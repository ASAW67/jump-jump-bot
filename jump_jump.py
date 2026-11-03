#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 本脚本由 DeepSeek 辅助编写（AI 辅助生成，仅供学习和技术研究使用）。
"""
微信小游戏《跳一跳》自动脚本（Windows + ADB + OpenCV）

整体流程：
    1. 用 `adb exec-out screencap -p` 获取手机实时画面；
    2. 用颜色阈值 + 轮廓分析找到棋子的底部中心；
    3. 用 Canny 边缘检测 + 轮廓分析找到目标方块，并取顶面中心；
    4. 计算两点欧氏距离，按压时长 = 距离 × 系数；
    5. 用 `adb shell input swipe x y x y duration` 模拟长按；
    6. 循环执行，每次跳跃后等待动画结束。

依赖安装：
    pip install opencv-python numpy adbutils

运行示例：
    python jump_jump.py --serial 你的设备序列号 --dry-run
    python jump_jump.py --serial 你的设备序列号 --coefficient 1.35

注意：
    - 运行前请手动打开微信《跳一跳》，并保持游戏在前台、竖屏；
    - 默认系数 1.35 适用于 1080 宽竖屏（如 1080x1920、1080x2344）；跳短了调大，跳过了调小；
    - 本脚本仅用于学习图像识别与自动化，请遵守游戏规则，风险自负。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np
from adbutils import AdbClient

# ------------------------------ 可调参数 ------------------------------

# 默认按压系数：按压毫秒数 = 像素距离 × 系数。
# 1080 宽屏（1080x1920、1080x2344 等）通常 1.30 ~ 1.40，1.35 是常用初值。
DEFAULT_COEFFICIENT = 1.35

# 按压时长的安全范围，避免识别异常时误按太久。
MIN_PRESS_MS = 100
MAX_PRESS_MS = 1300

# 找不到顶面中心白点时，彩色方块“顶面中心”在外接矩形中的高度比例。
# 白色方块没有可见白点，实测发现用 0.20 会瞄准偏上、导致跳过头；
# 单独用 0.30 更接近白色方块的落点中心。
TARGET_TOP_BIAS = 0.25
WHITE_TOP_BIAS = 0.30

# 自动校正后的按压补偿会持久化，下一次运行继续使用。
CALIBRATION_PATH = Path("jump_calibration.json")


def load_saved_offset() -> float:
    try:
        data = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
        return float(data.get("offset_ms", 0.0))
    except (OSError, ValueError, TypeError):
        return 0.0


def save_offset(offset_ms: float) -> None:
    try:
        CALIBRATION_PATH.write_text(
            json.dumps({"offset_ms": round(offset_ms, 1)}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


# ------------------------------ 数据结构 ------------------------------


@dataclass
class Box:
    """一个矩形检测框，坐标单位为手机截图像素。"""

    x: int
    y: int
    w: int
    h: int
    # 某些目标（例如带圆盘的方块）可以直接给出精确落点中心，
    # 避免再用外接框比例去估算。
    center_hint: Optional[Tuple[float, float]] = None

    @property
    def cx(self) -> float:
        """矩形中心 x 坐标。"""
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        """矩形中心 y 坐标。"""
        return self.y + self.h / 2.0

    @property
    def bottom(self) -> int:
        """矩形底边 y 坐标。"""
        return self.y + self.h


# ------------------------------ 图片写出 ------------------------------


def imwrite_unicode(path: Path, image: np.ndarray) -> bool:
    """保存图片，兼容 Windows 中文路径。"""
    suffix = path.suffix or ".png"
    ok, buffer = cv2.imencode(suffix, image)
    if not ok:
        return False
    try:
        buffer.tofile(str(path))
        return True
    except OSError:
        return False


# ------------------------------ 手机控制 ------------------------------


class Phone:
    """
    封装 adb 设备操作。

    - 截图：直接调用 adb exec-out，保证拿到的是原始 PNG 二进制；
    - 长按：通过 adbutils 的 device.shell 执行 input swipe。
    """

    def __init__(self, serial: Optional[str] = None, adb_path: Optional[str] = None):
        # 优先使用用户传入的 adb 路径，其次从 PATH 中查找。
        self.adb = adb_path or shutil.which("adb") or "adb"

        # adbutils 通过本机 5037 端口的 adb server 管理设备。
        client = AdbClient(host="127.0.0.1", port=5037)
        devices = client.device_list()

        if not devices:
            raise RuntimeError(
                "没有检测到安卓设备。请确认 USB 调试已开启、数据线已连接，"
                "并执行 adb devices 查看授权状态。"
            )

        if serial:
            matched = [d for d in devices if d.serial == serial]
            if not matched:
                serials = ", ".join(d.serial for d in devices)
                raise RuntimeError(f"找不到设备 {serial}，当前在线设备：{serials}")
            self.device = matched[0]
        else:
            if len(devices) > 1:
                serials = ", ".join(d.serial for d in devices)
                raise RuntimeError(
                    f"检测到多台设备，请用 --serial 指定一台。当前设备：{serials}"
                )
            self.device = devices[0]

        self.serial = self.device.serial

    def screencap(self) -> np.ndarray:
        """
        执行：adb -s <serial> exec-out screencap -p

        使用 exec-out 而不是 shell，是因为 exec-out 不会把二进制中的
        LF 转换成 CRLF，能避免截图 PNG 被破坏。
        """
        cmd = [self.adb]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += ["exec-out", "screencap", "-p"]

        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"找不到 adb 可执行文件：{self.adb}。请把 platform-tools 加入 PATH。"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("adb 截图超时，请检查数据线或无线连接。") from exc

        if result.returncode != 0:
            error = result.stderr.decode("utf-8", "ignore").strip()
            raise RuntimeError(f"adb 截图失败：{error or '未知错误'}")

        data = np.frombuffer(result.stdout, dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError(
                "截图解码失败。请确认 adb exec-out screencap -p 能正常输出 PNG。"
            )
        return image

    def long_press(self, x: float, y: float, duration_ms: int) -> None:
        """
        模拟长按。

        adb shell input swipe x y x y duration 的起点和终点相同，
        效果就是按住屏幕 duration 毫秒后松开。
        """
        x = int(round(x))
        y = int(round(y))
        duration_ms = int(duration_ms)

        # adbutils 的 shell 支持字符串命令；
        # 这里只拼接整数，不存在命令注入问题。
        self.device.shell(
            f"input swipe {x} {y} {x} {y} {duration_ms}"
        )

    def tap(self, x: float, y: float) -> None:
        """普通点击，用于点“再玩一局”。"""
        self.device.shell(f"input tap {int(round(x))} {int(round(y))}")

    def game_in_foreground(self) -> bool:
        """
        检查前台是不是微信小游戏，避免在微信聊天/文件传输界面乱点。

        微信聊天主页的 Activity 是 com.tencent.mm/.ui.LauncherUI；
        小游戏通常在 AppBrand 相关 Activity 中。这里只拦截已知的
        微信聊天/文件传输界面，其他非微信应用也直接判为不在游戏。
        """
        try:
            output = self.device.shell("dumpsys activity activities")
        except Exception:
            return False

        current = ""
        for line in output.splitlines():
            if "topResumedActivity" in line or "mResumedActivity" in line:
                current = line.strip()
                break

        if not current:
            return False

        # 微信聊天、文件传输助手、公众号文章等都在 LauncherUI 下。
        if "LauncherUI" in current or "ChattingUI" in current:
            return False

        # 不是微信的界面，也不要按。
        if "com.tencent.mm" not in current:
            return False

        return True


# ------------------------------ 棋子识别 ------------------------------


def _best_player_from_mask(
    mask: np.ndarray,
    roi_y0: int,
    width: int,
    height: int,
) -> Optional[Box]:
    """在二值掩码中筛选最像棋子的轮廓。"""
    # 形态学处理：先闭运算连接断裂区域，再开运算去掉小噪点。
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)

    contours = _find_contours(mask)

    # 面积、宽高的相对阈值，按 1080x2344 换算后大致为：
    # 面积 430~30000 像素，宽 13~170 像素，高 58~580 像素。
    min_area = max(150, int(width * height * 0.00017))
    max_area = int(width * height * 0.012)

    best_box: Optional[Box] = None
    best_score = -1e9

    for contour in contours:
        x, y, bw, bh = cv2.boundingRect(contour)
        area = cv2.contourArea(contour)

        if area < min_area or area > max_area:
            continue
        if bw < width * 0.012 or bh < height * 0.025:
            continue
        if bw > width * 0.16 or bh > height * 0.25:
            continue

        aspect = bh / max(bw, 1)
        if aspect < 1.10:
            continue

        # 轮廓在 ROI 中的坐标要加回 roi_y0，才是整张截图的坐标。
        box = Box(x, roi_y0 + y, bw, bh)

        # 排除画面最左/最右的底部 UI 图标，避免把“上传”等图标当成棋子。
        if box.cx < width * 0.10 or box.cx > width * 0.90:
            continue
        if box.bottom > height * 0.78:
            continue

        # 棋子是“头 + 身体”两段；瓶子/方块阴影通常是单独一块。
        # 如果身体上方有一个尺寸合适的小圆头，大幅加分。
        has_head = False
        for head_contour in contours:
            hx, hy, hw, hh = cv2.boundingRect(head_contour)
            head_area = cv2.contourArea(head_contour)
            if head_area < 300 or head_area > 4000:
                continue
            if hw < 20 or hh < 15:
                continue
            if not 0.5 * bw <= hw <= 1.3 * bw:
                continue
            if hh > 0.60 * bh:
                continue
            head_ratio = hh / max(hw, 1)
            if not 0.60 <= head_ratio <= 1.40:
                continue

            head_box = Box(hx, roi_y0 + hy, hw, hh)
            if (
                head_box.cy < box.y
                and box.y - head_box.bottom < 0.50 * bh
                and abs(head_box.cx - box.cx) < 0.70 * bw
            ):
                has_head = True
                break

        # 越靠下越可能是棋子；细长和面积适中适当加分。
        bottom_score = box.bottom / height
        aspect_score = min(aspect, 3.0) / 3.0
        area_score = min(area / (width * height * 0.004), 1.0)
        score = bottom_score * 0.65 + aspect_score * 0.20 + area_score * 0.15
        if has_head:
            score += 0.70

        if score > best_score:
            best_score = score
            best_box = box

    return best_box


def find_player_by_color(image: np.ndarray) -> Optional[Box]:
    """
    方法二：颜色阈值 + 轮廓分析。

    当前游戏的棋子是深蓝/紫色，阴影是黄绿色，二者 HSV 色相差别很大。
    因此先按蓝紫色相找棋子，可以避免阴影把棋子外接框拉宽；
    如果找不到，再用“深色/高饱和”作为通用兜底。
    """
    h, w = image.shape[:2]

    # 只处理画面下半部分。y0 大约在屏幕 42% 处。
    roi_y0 = int(h * 0.42)
    roi = image[roi_y0 : int(h * 0.985), :]

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

    # 第一优先：深蓝/紫色棋子（当前皮肤的实测色相约 118~132）。
    # 注意两点：
    #   1. 限制 V 上限：紫色方块本身很亮（V 约 225~240），
    #      只看色相会和方块连成一个大轮廓；
    #   2. 限制 S 下限：紫色主题背景的阴影饱和度较低，
    #      提高 S 下限可以把棋子和背景阴影分开；
    #   3. 蓝色药罐、蓝色标签的饱和度很低，S>=80 也不会把它们并进棋子。
    purple_mask = cv2.inRange(hsv, (100, 80, 35), (170, 255, 160))
    purple_box = _best_player_from_mask(purple_mask, roi_y0, w, h)
    if purple_box is not None:
        return purple_box

    # 兜底：深色/高饱和棋子（覆盖黑色、深蓝、红色等皮肤）。
    dark_mask = cv2.inRange(hsv, (0, 0, 0), (180, 255, 120))
    saturated_mask = cv2.inRange(hsv, (0, 100, 30), (180, 255, 255))
    generic_mask = cv2.bitwise_or(dark_mask, saturated_mask)
    return _best_player_from_mask(generic_mask, roi_y0, w, h)


# ------------------------------ 目标方块识别 ------------------------------


def _collect_target_candidates(
    binary: np.ndarray,
    player: Box,
    width: int,
    height: int,
    min_area_ratio: float,
    direction: Optional[Tuple[float, float]] = None,
) -> list:
    """
    从一个二值图中收集“可能是目标方块”的候选框。

    关键点：棋子脚下的当前方块，其外接框通常会包含棋子底部中心；
    而目标方块在画面另一侧，不会包含这个点。用这个条件排除当前方块，
    比单纯要求“目标必须在棋子上方”更可靠，因为侧向跳跃时目标方块
    在屏幕上的高度可能和棋子差不多，甚至更低。
    """
    contours = _find_contours(binary)
    min_area = width * height * min_area_ratio
    max_area = width * height * 0.14

    candidates = []

    for contour in contours:
        x, y, bw, bh = cv2.boundingRect(contour)
        area = cv2.contourArea(contour)

        if area < min_area or area > max_area:
            continue
        if bw < width * 0.04 or bh < height * 0.012:
            continue
        if bw > width * 0.70 or bh > height * 0.45:
            continue

        box = Box(x, y, bw, bh)

        # 候选框紧贴画面左/右边缘时，通常把投影阴影也算进来了。
        # 向内侧裁掉一小段，避免目标中心被阴影拉偏。
        if box.w > width * 0.25:
            trim = int(box.w * 0.08)
            if box.x <= 1:
                box.x += trim
                box.w -= trim
            if box.x + box.w >= width - 1:
                box.w -= trim

        # 画面顶部是状态栏、游戏图标和分数，不是方块区域。
        # 防止把顶部 UI 图标误认成目标，导致按压时间突然变得很大。
        if box.cy < height * 0.25:
            continue

        # 当前方块：外接框包含棋子底部，而且棋子基本站在它的中间。
        # 近距离时目标方块的框也可能包含棋子底部，但棋子会偏向框的一侧，
        # 用“棋子相对框中心的横向偏移”区分，避免把目标误排除。
        contains_base = _contains_point(box, player.cx, player.bottom)
        center_offset = abs(box.cx - player.cx) / max(box.w, 1)

        # 超宽的框又包含棋子底部，通常是把当前方块和下一块连在一起了。
        if contains_base and box.w > player.w * 3.0:
            continue

        if contains_base and center_offset < 0.28:
            continue

        # 棋子站在方块边缘时，外接框可能不包含棋子底部中心，
        # 但方块的边缘离棋子底部很近；这种情况也判为当前方块。
        vertical_overlap = box.y <= player.bottom <= box.y + box.h
        edge_distance = min(
            abs(player.cx - box.x),
            abs(box.x + box.w - player.cx),
        )
        if vertical_overlap and edge_distance < player.w * 0.8:
            continue

        # 正下方、横向和棋子几乎重合的目标，通常是圆桌/台子的桌脚，
        # 不是下一块方块。下一块可以有高低差，但不会在棋子正下方。
        if (
            box.cy > player.bottom
            and abs(box.cx - player.cx) < player.w * 1.5
        ):
            continue

        # 目标方块的中心不应明显低于棋子中心。
        if box.cy >= player.cy + height * 0.06:
            continue

        # 与棋子框重叠太多，通常也是脚下方块或棋子本身。
        # 但近距离目标框可能包含棋子，这种情况上面已经用偏移量放行。
        overlap = _intersection_area(box, player)
        if not contains_base and overlap > 0.40 * player.w * player.h:
            continue

        # 面积越大、离棋子越近，越可能是下一块。
        distance = math.hypot(box.cx - player.cx, box.cy - player.cy)

        # 距离过近的候选通常是棋子脚下的当前方块或误检。
        # 真正的下一块中心不会和棋子底部中心几乎重合。
        # 用 1.1 倍棋子宽度：既能排除 60~70px 的原地跳，又不会把很近的药罐误杀。
        if distance < max(110.0, player.w * 1.1):
            continue

        # 距离超过屏幕宽度 92% 的“目标”基本是顶部 UI/远处装饰，
        # 正常跳跃不会需要这么长的按压。
        if distance > width * 0.92:
            continue

        # 下一块通常是离棋子最近的候选；用 1/距离 打分，
        # 避免远处的大方块压过眼前的药罐/小目标。
        score = 1.0 / (distance + 1.0)

        candidates.append((score, box))

    return candidates


def _find_target_by_ellipse(image: np.ndarray, player: Box) -> Optional[Box]:
    """
    兜底：检测椭圆形顶面。

    圆桌、椭圆台面在等距视角下会变成椭圆，霍夫圆容易把圆心估偏。
    这里对边缘轮廓做 fitEllipse，要求明显“长宽不等”，取椭圆中心。
    """
    h, w = image.shape[:2]
    gray = cv2.GaussianBlur(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    edges = cv2.Canny(gray, 30, 80)
    found = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    contours = found[0] if len(found) == 2 else found[1]

    candidates = []
    for contour in contours:
        if len(contour) < 50:
            continue

        (cx, cy), (a, b), _ = cv2.fitEllipse(contour)
        major, minor = max(a, b), min(a, b)

        # 只要明显椭圆的顶面，避免和普通圆/方块混淆。
        if major / max(minor, 1) < 1.25:
            continue
        if major < 180 or major > 600 or minor < 80 or minor > 400:
            continue
        if cy < h * 0.35 or cy >= player.cy:
            continue

        center_distance = math.hypot(cx - player.cx, cy - player.cy)
        if center_distance < player.w * 1.5 or center_distance > w * 0.92:
            continue

        box = Box(
            int(cx - major / 2),
            int(cy - minor / 2),
            int(major),
            int(minor),
            center_hint=(float(cx), float(cy)),
        )
        overlap = _intersection_area(box, player)
        if overlap > 0.40 * player.w * player.h:
            continue

        candidates.append((len(contour), center_distance, box))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][2]


def _find_target_by_circle(image: np.ndarray, player: Box) -> Optional[Box]:
    """
    兜底：直接检测目标顶部的大圆盘/圆形顶面。

    有些方块顶部是唱片、圆盘或圆形台面（例如唱片机方块），
    颜色和背景非常接近，颜色/轮廓策略都不稳；但圆形边缘很明显。
    这里用霍夫圆找“棋子斜上方的圆形顶面”，圆心直接作为落点。
    """
    h, w = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.medianBlur(gray, 7)

    circles = cv2.HoughCircles(
        gray,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=80,
        param1=80,
        param2=20,
        minRadius=60,
        maxRadius=180,
    )
    if circles is None:
        return None

    candidates = []
    for cx, cy, r in np.round(circles[0]).astype(int):
        # 只保留画面中下部、棋子上方的圆。
        if cy < h * 0.35 or cy >= player.cy:
            continue

        # 离棋子太近的圆通常是脚下的当前方块。
        center_distance = math.hypot(cx - player.cx, cy - player.cy)
        if center_distance < max(110.0, player.w * 1.1):
            continue
        if center_distance > w * 0.92:
            continue

        box = Box(
            int(cx - r),
            int(cy - r),
            int(2 * r),
            int(2 * r),
            center_hint=(float(cx), float(cy)),
        )

        # 和棋子框重叠太多的圆也排除。
        overlap = _intersection_area(box, player)
        if overlap > 0.40 * player.w * player.h:
            continue

        # 越靠上越像目标圆盘；同一高度时取离棋子近的。
        candidates.append((cy, center_distance, box))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def find_target(
    image: np.ndarray,
    player: Box,
    direction: Optional[Tuple[float, float]] = None,
) -> Optional[Box]:
    """
    用多种互补的方式寻找目标方块：

    方式 A：饱和度通道 Canny。
        适合彩色方块，或者目标方块和当前方块在画面中已经分开的情况。

    方式 B：白色/灰色低饱和掩码。
        适合白色方块。白色方块和橙色/绿色当前方块相邻时，饱和度边缘
        可能把两块连成一个大轮廓；但按低饱和度分割可以把白色目标单独
        拿出来，不会被当前方块带偏。

    方式 C：按色相分家族的彩色掩码，并挖掉棋子所在区域。
        适合“白色当前方块 + 绿色目标方块”等目标为彩色的情况。
        按红橙、黄、绿青、蓝紫分别提取，不依赖固定高饱和阈值。

    方式 D：通用前景掩码 + 挖掉脚下圆形区域。
        适合同色方块相邻、前三种方式都找不到候选的兜底情况，
        对灰色+灰色、绿色+绿色等组合都适用。

    方式 E：椭圆/圆形顶面兜底。
        适合顶部是唱片、圆盘、圆桌或椭圆台面的方块；椭圆先做拟合，
        圆形再用霍夫圆，直接取中心作为落点。

    方式 F：深灰色方块兜底。
        适合浅灰背景上的深灰色方块，用“比背景暗”来分割。

    最后比较所有候选，取“面积大、离棋子近”的那个。
    """
    h, w = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    # ---------- 方式 A：饱和度通道 Canny ----------
    saturation = cv2.GaussianBlur(hsv[:, :, 1], (5, 5), 0)
    edges = cv2.Canny(saturation, 30, 80)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    edges = cv2.morphologyEx(
        edges,
        cv2.MORPH_CLOSE,
        np.ones((5, 5), np.uint8),
        iterations=1,
    )
    candidates = _collect_target_candidates(edges, player, w, h, 0.002, direction)

    # ---------- 方式 B：白色/灰色低饱和掩码 ----------
    # 先估计当前主题的背景颜色。
    # 如果背景本身是低饱和的灰色/白色，白色方块通常比背景更亮，
    # 这时用“比背景更亮”来提取；否则用低饱和 + 较高亮度的通用掩码。
    bg_roi = hsv[int(h * 0.06) : int(h * 0.14), :, :]
    if bg_roi.size == 0:
        bg_s, bg_v = 45, 120
    else:
        bg_s = float(np.median(bg_roi[:, :, 1]))
        bg_v = float(np.median(bg_roi[:, :, 2]))

    if bg_s < 70:
        lower_v = min(250, int(bg_v + 6))
        bright_mask = cv2.inRange(hsv, (0, 0, lower_v), (180, 60, 255))
        # 白色方块也可能不是更亮，而是明显比背景更低饱和。
        desat_s = max(10, int(bg_s - 10))
        desat_mask = cv2.inRange(hsv, (0, 0, 120), (180, desat_s, 255))
        white_mask = cv2.bitwise_or(bright_mask, desat_mask)
    else:
        white_mask = cv2.inRange(hsv, (0, 0, 90), (180, 45, 255))

    white_mask = cv2.morphologyEx(
        white_mask,
        cv2.MORPH_CLOSE,
        np.ones((5, 5), np.uint8),
        iterations=2,
    )
    white_mask = cv2.morphologyEx(
        white_mask,
        cv2.MORPH_OPEN,
        np.ones((3, 3), np.uint8),
        iterations=1,
    )
    candidates += _collect_target_candidates(white_mask, player, w, h, 0.003, direction)

    # 白色目标和白色当前方块贴在一起时，白色掩码会连成一个大轮廓。
    # 再从白色掩码里挖掉棋子脚下的圆，单独收集一份候选。
    white_cut = white_mask.copy()
    cut_radius = max(12, int(player.w * 2.5))
    cut_center = (int(player.cx), int(player.bottom - h * 0.03))
    cv2.circle(white_cut, cut_center, cut_radius, 0, -1)
    candidates += _collect_target_candidates(white_cut, player, w, h, 0.003, direction)

    # 白色桌脚和下一块方块的白色条纹也可能连成一个大框，
    # 同样沿棋子 x 坐标切成左右两半分别找。
    for base_mask in (white_mask, white_cut):
        cut_x = max(1, min(w - 1, int(player.cx)))
        right_half = base_mask.copy()
        right_half[:, :cut_x] = 0
        candidates += _collect_target_candidates(
            right_half, player, w, h, 0.003, direction
        )

        left_half = base_mask.copy()
        left_half[:, cut_x:] = 0
        candidates += _collect_target_candidates(
            left_half, player, w, h, 0.003, direction
        )

    # ---------- 方式 C：彩色方块（按色相分家族） ----------
    # 背景是浅黄色，色相约 27。下面按色相覆盖红橙、黄、绿青、蓝紫，
    # 并把棋子所在矩形区域挖掉，避免棋子和目标连成一个包含棋子底部
    # 的大轮廓。这样不依赖固定的饱和度阈值，绿色方块 S 只有 82 时
    # 也能识别，不会像只用 S>=90 那样漏掉。
    margin = max(8, int(w * 0.008))
    x1 = max(0, player.x - margin)
    y1 = max(0, player.y - margin)
    x2 = min(w, player.x + player.w + margin)
    y2 = min(h, player.y + player.h + margin)
    player_region = np.zeros((h, w), np.uint8)
    player_region[y1:y2, x1:x2] = 255
    player_region_inv = cv2.bitwise_not(player_region)

    color_ranges = [
        # 红/橙：S 下限提高到 110，避免把米色/浅橙背景一起选进来。
        ((0, 110, 40), (20, 255, 255)),
        ((20, 100, 40), (35, 255, 255)),   # 黄（只取高饱和，避开浅黄背景）
        ((35, 45, 40), (100, 255, 255)),   # 绿/青
        ((100, 45, 40), (170, 255, 255)),  # 蓝/紫
        ((170, 110, 40), (180, 255, 255)), # 红色在 HSV 中的回绕段
    ]

    for lower, upper in color_ranges:
        color_mask = cv2.inRange(hsv, lower, upper)
        color_mask = cv2.bitwise_and(color_mask, player_region_inv)
        color_mask = cv2.morphologyEx(
            color_mask,
            cv2.MORPH_CLOSE,
            np.ones((5, 5), np.uint8),
            iterations=1,
        )
        color_mask = cv2.morphologyEx(
            color_mask,
            cv2.MORPH_OPEN,
            np.ones((3, 3), np.uint8),
            iterations=1,
        )
        candidates += _collect_target_candidates(color_mask, player, w, h, 0.003, direction)

        # 同色方块左右贴在一起时，色相掩码会把它们连成一个大框，
        # 框中心可能正好落在棋子上。沿棋子 x 坐标切成左右两半，
        # 再各自找候选，可以把“当前方块 + 下一块”拆开。
        cut_x = max(1, min(w - 1, int(player.cx)))
        right_half = color_mask.copy()
        right_half[:, :cut_x] = 0
        candidates += _collect_target_candidates(
            right_half, player, w, h, 0.003, direction
        )

        left_half = color_mask.copy()
        left_half[:, cut_x:] = 0
        candidates += _collect_target_candidates(
            left_half, player, w, h, 0.003, direction
        )

    # ---------- 方式 D：通用前景掩码 + 挖掉脚下当前方块 ----------
    # 同色方块相邻时（灰色+灰色、绿色+绿色等），前面的掩码会把两块
    # 连成一个大轮廓，并被“包含棋子底部”条件排除。这里退一步：
    # 合并灰色区域和所有彩色家族，以棋子底部为中心挖一个圆，
    # 把脚下的当前方块切断，再在剩余区域中找目标。
    if not candidates:
        gray_like = cv2.inRange(hsv, (0, 0, 45), (180, 55, 235))
        foreground = gray_like.copy()
        for lower, upper in color_ranges:
            foreground = cv2.bitwise_or(
                foreground,
                cv2.inRange(hsv, lower, upper),
            )

        # 先去掉棋子自身，避免棋子参与轮廓。
        foreground = cv2.bitwise_and(foreground, player_region_inv)
        foreground = cv2.morphologyEx(
            foreground,
            cv2.MORPH_CLOSE,
            np.ones((5, 5), np.uint8),
            iterations=2,
        )
        foreground = cv2.morphologyEx(
            foreground,
            cv2.MORPH_OPEN,
            np.ones((3, 3), np.uint8),
            iterations=1,
        )

        # 半径按棋子宽度自适应；2.5 倍棋子宽度可以覆盖大多数当前方块。
        radius = max(12, int(player.w * 2.5))
        cut_center = (int(player.cx), int(player.bottom - h * 0.03))
        cv2.circle(foreground, cut_center, radius, 0, -1)

        candidates += _collect_target_candidates(foreground, player, w, h, 0.004, direction)

    if not candidates:
        # 先试椭圆顶面（圆桌、椭圆台面）。
        ellipse_box = _find_target_by_ellipse(image, player)
        if ellipse_box is not None:
            return ellipse_box

        # 再试唱片机、圆盘等圆形顶面，用霍夫圆直接定位圆心。
        circle_box = _find_target_by_circle(image, player)
        if circle_box is not None:
            return circle_box

        # 再兜底：浅灰背景上的深灰色方块。
        # 背景亮度和目标接近，但目标通常更暗；用背景亮度减去余量作为上限。
        if bg_s < 70 and bg_v > 200:
            vmax = max(60, int(bg_v - 20))
            dark_mask = cv2.inRange(hsv, (0, 0, 40), (180, 50, vmax))
            dark_mask = cv2.morphologyEx(
                dark_mask,
                cv2.MORPH_CLOSE,
                np.ones((5, 5), np.uint8),
                iterations=2,
            )
            dark_mask = cv2.morphologyEx(
                dark_mask,
                cv2.MORPH_OPEN,
                np.ones((3, 3), np.uint8),
                iterations=1,
            )
            candidates += _collect_target_candidates(dark_mask, player, w, h, 0.004, direction)

        if not candidates:
            return None

    # 同一个目标可能被两种方式都检测到，取分数最高的即可。
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _find_center_dot(image: np.ndarray, target: Box) -> Optional[Tuple[float, float]]:
    """
    找目标方块顶面中心的小白点。

    游戏里很多方块顶面中心会有一个小白点（或浅色圆点），它比
    按外接框 25% 高度估算更准确，尤其是方块厚度不同的时候。
    找不到就返回 None，由调用方回退到几何估算。
    """
    roi = image[
        target.y : target.y + target.h,
        target.x : target.x + target.w,
    ]
    if roi.size == 0:
        return None

    # 很小的目标（药罐、瓶子等）没有“中心白点”，
    # 强行检测容易把瓶盖纹路误当成白点，直接用外接框中心/比例更稳。
    if target.w < 180 or target.h < 120:
        return None

    h, w = roi.shape[:2]

    # 方法一：霍夫圆。灰色/彩色方块上的小白点通常是一个小圆。
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    gray = cv2.medianBlur(gray, 5)
    circles = cv2.HoughCircles(
        gray,
        cv2.HOUGH_GRADIENT,
        dp=1,
        minDist=15,
        param1=60,
        param2=12,
        minRadius=6,
        maxRadius=30,
    )
    if circles is not None:
        for cx, cy, _ in np.round(circles[0]).astype(int):
            # 只接受顶面上半部分、横向靠近方块中心的圆点。
            # 方块侧面可能有笑脸/圆形装饰，y 限制在 45% 以内可以排除。
            if cy > h * 0.45:
                continue
            if abs(cx - w / 2.0) > w * 0.35:
                continue
            return target.x + float(cx), target.y + float(cy)

    # 方法二：高亮低饱和小色块（白色方块上白点对比度低，通常找不到）。
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 0, 200), (180, 40, 255))
    found = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = found[0] if len(found) == 2 else found[1]

    candidates = []
    for contour in contours:
        x, y, bw, bh = cv2.boundingRect(contour)
        area = cv2.contourArea(contour)

        if area < 50 or area > 1800:
            continue
        if bw < 8 or bh < 8 or bw > 55 or bh > 55:
            continue
        if not 0.5 <= bw / max(bh, 1) <= 2.0:
            continue
        if y + bh / 2.0 > h * 0.45:
            continue
        if abs((x + bw / 2.0) - w / 2.0) > w * 0.35:
            continue

        # 面积接近 400 像素、且靠近方块中心的更像中心白点。
        score = abs(area - 400) + abs((x + bw / 2.0) - w / 2.0) * 2
        candidates.append((score, x + bw / 2.0, y + bh / 2.0))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    _, cx, cy = candidates[0]
    return target.x + float(cx), target.y + float(cy)


def get_target_point(
    image: np.ndarray,
    target: Box,
    white_bias: float = WHITE_TOP_BIAS,
) -> Tuple[float, float]:
    """
    取目标方块的“顶面中心”。

    优先使用顶面中心的小白点；如果找不到：
      - 白色方块没有可见白点，用 white_bias（默认 0.30）；
      - 彩色方块用 TARGET_TOP_BIAS（0.25）。
    """
    if target.center_hint is not None:
        return target.center_hint

    dot = _find_center_dot(image, target)
    if dot is not None:
        return dot

    top_h = max(1, int(target.h * 0.55))
    top_roi = image[
        target.y : target.y + top_h,
        target.x : target.x + target.w,
    ]
    mean_saturation = 0.0
    if top_roi.size > 0:
        hsv = cv2.cvtColor(top_roi, cv2.COLOR_BGR2HSV)
        mean_saturation = float(hsv[:, :, 1].mean())

    bias = white_bias if mean_saturation < 45 else TARGET_TOP_BIAS

    # 圆桌/桌子类目标：外接框通常“高大于宽”，因为把桌脚也算进来了。
    # 这时顶面中心更靠上，用 10% 而不是 30%，避免落点被桌脚拉低。
    if target.h > target.w * 1.15:
        bias = min(bias, 0.10)

    return target.cx, target.y + target.h * bias


# ------------------------------ 工具函数 ------------------------------


def _find_contours(mask: np.ndarray):
    """
    兼容 OpenCV 3 和 OpenCV 4 的 findContours 返回值。

    OpenCV 3 返回 (image, contours, hierarchy)；
    OpenCV 4 返回 (contours, hierarchy)。
    """
    found = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    return found[0] if len(found) == 2 else found[1]


def _contains_point(box: Box, x: float, y: float) -> bool:
    """判断点 (x, y) 是否落在矩形框内。"""
    return (
        box.x <= x <= box.x + box.w
        and box.y <= y <= box.y + box.h
    )


def _intersection_area(a: Box, b: Box) -> int:
    """计算两个矩形框的交集面积。"""
    x1 = max(a.x, b.x)
    y1 = max(a.y, b.y)
    x2 = min(a.x + a.w, b.x + b.w)
    y2 = min(a.y + a.h, b.y + b.h)
    return max(0, x2 - x1) * max(0, y2 - y1)


def calc_press_ms(
    distance_px: float,
    coefficient: float,
    offset_ms: float = 0.0,
) -> int:
    """
    按压时长 = 欧氏距离 × 系数 + 固定补偿。

    例如 1080p 下距离 300 像素：
        300 × 1.35 + 0 = 405 毫秒。
    coefficient 调比例误差，offset_ms 调“整体偏短/偏长”的固定误差。
    """
    ms = distance_px * coefficient + offset_ms
    return int(round(max(MIN_PRESS_MS, min(MAX_PRESS_MS, ms))))


def try_restart_game(phone: "Phone") -> bool:
    """
    游戏结束页自动点“再玩一局”。

    游戏是 WebView 画布，uiautomator 拿不到按钮文字，因此用视觉找
    底部中央的白色圆角大按钮：宽度约屏宽 30%~65%，高度 4%~10%，
    长宽比 3~7，中心在画面 60%~85% 高度。
    """
    try:
        image = phone.screencap()
    except Exception:
        return False

    h, w = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 0, 200), (180, 40, 255))
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        np.ones((7, 7), np.uint8),
        iterations=2,
    )
    found = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = found[0] if len(found) == 2 else found[1]

    candidates = []
    for contour in contours:
        x, y, bw, bh = cv2.boundingRect(contour)
        if bw < w * 0.30 or bw > w * 0.65:
            continue
        if bh < h * 0.04 or bh > h * 0.10:
            continue
        if not 3.0 <= bw / max(bh, 1) <= 7.0:
            continue
        if y < h * 0.60 or y + bh > h * 0.90:
            continue
        candidates.append((bw * bh, x, y, bw, bh))

    if not candidates:
        return False

    candidates.sort(reverse=True)
    _, x, y, bw, bh = candidates[0]
    cx = x + bw / 2
    cy = y + bh / 2
    print(f"[修复] 检测到“再玩一局”按钮，点击 ({cx:.0f}, {cy:.0f})")
    try:
        phone.tap(cx, cy)
    except Exception:
        return False

    time.sleep(2.0)
    return True


def _block_top_center(box: Box) -> Tuple[float, float]:
    """按方块比例估算当前方块的顶面中心。"""
    bias = 0.10 if box.h > box.w * 1.15 else 0.25
    return box.cx, box.y + box.h * bias


def find_block_under_player(image: np.ndarray, player: Box) -> Optional[Box]:
    """
    找棋子脚下的当前方块，用于落地偏差检测。

    做法：分别用白色、彩色家族、灰色掩码找轮廓，只保留“包含棋子
    底部或边缘离棋子很近”的候选，选中心离棋子最近的那个。
    """
    h, w = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    masks = []

    # 白色/灰色：兼顾亮背景和低饱和背景。
    bg_roi = hsv[int(h * 0.06) : int(h * 0.14), :, :]
    bg_s = float(np.median(bg_roi[:, :, 1])) if bg_roi.size else 45.0
    bg_v = float(np.median(bg_roi[:, :, 2])) if bg_roi.size else 120.0
    if bg_s < 70:
        lower_v = min(250, int(bg_v + 6))
        bright = cv2.inRange(hsv, (0, 0, lower_v), (180, 60, 255))
        desat_s = max(10, int(bg_s - 10))
        desat = cv2.inRange(hsv, (0, 0, 120), (180, desat_s, 255))
        masks.append(cv2.bitwise_or(bright, desat))
    else:
        masks.append(cv2.inRange(hsv, (0, 0, 90), (180, 45, 255)))

    for lower, upper in [
        ((0, 110, 40), (20, 255, 255)),
        ((20, 100, 40), (35, 255, 255)),
        ((35, 45, 40), (100, 255, 255)),
        ((100, 45, 40), (170, 255, 255)),
        ((170, 110, 40), (180, 255, 255)),
    ]:
        masks.append(cv2.inRange(hsv, lower, upper))

    masks.append(cv2.inRange(hsv, (0, 0, 45), (180, 55, 235)))

    candidates = []
    for mask in masks:
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8), iterations=2
        )
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8), iterations=1
        )
        for contour in _find_contours(mask):
            x, y, bw, bh = cv2.boundingRect(contour)
            area = cv2.contourArea(contour)
            if area < 4000 or bw < 40 or bh < 20:
                continue

            box = Box(x, y, bw, bh)

            # 跳过棋子自身；大块方块即使包含棋子也要保留。
            if (
                box.w * box.h < 3.0 * player.w * player.h
                and _intersection_area(box, player) > 0.50 * player.w * player.h
            ):
                continue

            contains = _contains_point(box, player.cx, player.bottom)
            vertical = box.y <= player.bottom <= box.y + box.h
            edge_distance = min(
                abs(player.cx - box.x),
                abs(box.x + box.w - player.cx),
            )
            if not contains and not (vertical and edge_distance < player.w * 1.2):
                continue

            center_distance = math.hypot(box.cx - player.cx, box.cy - player.cy)
            candidates.append((center_distance, -area, box))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def attempt_recovery(phone: "Phone", reason: str) -> bool:
    """
    自动修复入口：游戏结束就点“再玩一局”；
    如果前台不是游戏，则等待游戏回到前台，不执行任何点击。
    """
    print(f"[自动修复] {reason}")

    if not phone.game_in_foreground():
        print("[自动修复] 当前前台不是小游戏，等待 5 秒后重试。")
        time.sleep(5.0)
        return False

    if try_restart_game(phone):
        print("[自动修复] 已重开一局。")
        return True

    print("[自动修复] 未找到重开按钮，等待 3 秒后重试。")
    time.sleep(3.0)
    return False


def wait_for_landing(
    phone: "Phone",
    timeout: float = 3.0,
    interval: float = 0.22,
) -> Tuple[Optional[Box], Optional[np.ndarray]]:
    """
    跳跃后实时监控画面，直到棋子重新站稳或判定游戏结束。

    返回 (落点棋子框, 最后一帧)：
      - 棋子连续 3 帧找不到，或变成横向倒下，返回 (None, 最后一帧)；
      - 棋子位置连续稳定，返回 (棋子框, 当前帧)；
      - 超时仍未稳定，返回 (None, 最后一帧)。
    """
    start = time.monotonic()
    last_player: Optional[Box] = None
    last_image: Optional[np.ndarray] = None
    stable_frames = 0
    missing_frames = 0

    while time.monotonic() - start < timeout:
        time.sleep(interval)
        try:
            image = phone.screencap()
        except Exception:
            return None, last_image

        last_image = image
        player = find_player_by_color(image)

        if player is None:
            missing_frames += 1
            if missing_frames >= 3:
                return None, image
            continue

        missing_frames = 0

        # 棋子横向倒下：游戏结束。
        if player.h < player.w * 0.90:
            return None, image

        if last_player is not None:
            moved = math.hypot(
                player.cx - last_player.cx,
                player.bottom - last_player.bottom,
            )
            if moved < 4.0:
                stable_frames += 1
            else:
                stable_frames = 0

        last_player = player
        if stable_frames >= 2 and time.monotonic() - start > 0.6:
            return player, image

    # 超时但还看得到棋子：返回最后位置，交给下一轮继续判断，
    # 不要误判为游戏结束。
    return last_player, last_image


def manual_click_target(image: np.ndarray) -> Optional[Box]:
    """
    在电脑上显示当前截图，让用户点击目标中心。
    点击坐标会换算回手机截图坐标，并作为精确落点。
    """
    h, w = image.shape[:2]
    scale = min(1.0, 700.0 / max(h, 1))
    display = cv2.resize(
        image,
        (int(w * scale), int(h * scale)),
        interpolation=cv2.INTER_AREA,
    )
    clicked = []

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            clicked.append((x / scale, y / scale))

    window = "Click target center  (ESC to cancel)"
    print("[手动] 请在弹出的窗口中点击目标方块顶面中心；按 ESC 取消。")
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, display.shape[1], display.shape[0])
    cv2.moveWindow(window, 40, 40)
    try:
        cv2.setWindowProperty(window, cv2.WND_PROP_TOPMOST, 1)
    except Exception:
        pass
    cv2.setMouseCallback(window, on_mouse)
    cv2.imshow(window, display)
    cv2.waitKey(30)

    while not clicked:
        if cv2.waitKey(50) & 0xFF == 27:
            break

    cv2.destroyWindow(window)
    if not clicked:
        print("[手动] 已取消。")
        return None

    px, py = clicked[0]
    print(f"[手动] 目标中心=({px:.0f}, {py:.0f})")
    return Box(
        int(px) - 5,
        int(py) - 5,
        10,
        10,
        center_hint=(float(px), float(py)),
    )


def annotate(
    image: np.ndarray,
    player: Optional[Box] = None,
    target: Optional[Box] = None,
    point: Optional[Tuple[float, float]] = None,
    text: str = "",
) -> np.ndarray:
    """把识别结果画到截图上，方便调试。"""
    canvas = image.copy()

    if player is not None:
        cv2.rectangle(
            canvas,
            (player.x, player.y),
            (player.x + player.w, player.y + player.h),
            (0, 255, 0),
            2,
        )
        cv2.circle(
            canvas,
            (int(player.cx), player.bottom),
            5,
            (0, 255, 0),
            -1,
        )

    if target is not None:
        cv2.rectangle(
            canvas,
            (target.x, target.y),
            (target.x + target.w, target.y + target.h),
            (0, 0, 255),
            2,
        )

    if player is not None and point is not None:
        cv2.circle(canvas, (int(point[0]), int(point[1])), 6, (0, 0, 255), -1)
        cv2.line(
            canvas,
            (int(player.cx), player.bottom),
            (int(point[0]), int(point[1])),
            (255, 0, 255),
            2,
        )

    if text:
        cv2.putText(
            canvas,
            text,
            (20, 45),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 0, 255),
            2,
            cv2.LINE_AA,
        )

    return canvas


# ------------------------------ 主流程 ------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="微信小游戏《跳一跳》ADB 自动脚本",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--serial", default=None, help="设备序列号，单设备可省略")
    parser.add_argument("--adb", default=None, help="adb 可执行文件路径，默认从 PATH 查找")
    parser.add_argument(
        "--coefficient",
        type=float,
        default=DEFAULT_COEFFICIENT,
        help="按压系数：按压毫秒数 = 像素距离 × 系数 + 固定补偿",
    )
    parser.add_argument(
        "--distance-slope",
        type=float,
        default=0.00020,
        help="距离越远系数越大的斜率；设为 0 则固定系数",
    )
    parser.add_argument(
        "--offset-ms",
        type=float,
        default=0.0,
        help="固定补偿毫秒数；整体偏短用正数，整体偏长用负数",
    )
    parser.add_argument(
        "--white-bias",
        type=float,
        default=WHITE_TOP_BIAS,
        help="白色方块没有白点时，顶面中心在外接框中的高度比例",
    )
    parser.add_argument("--delay", type=float, default=1.5, help="每次跳跃后的等待秒数")
    parser.add_argument(
        "--no-monitor",
        action="store_true",
        help="跳跃后不做实时落地监控，只用固定 delay 等待",
    )
    parser.add_argument(
        "--no-auto-restart",
        action="store_true",
        help="游戏结束后不自动点“再玩一局”",
    )
    parser.add_argument(
        "--no-auto-tune",
        action="store_true",
        help="失败后不自动微调按压补偿",
    )
    parser.add_argument(
        "--reset-calibration",
        action="store_true",
        help="清除已保存的自动补偿，从 --offset-ms 重新开始",
    )
    parser.add_argument("--max-fail", type=int, default=5, help="连续识别失败多少次后退出")
    parser.add_argument("--once", action="store_true", help="只跳一次")
    parser.add_argument(
        "--manual",
        action="store_true",
        help="每跳在电脑上点击目标中心（最稳，适合复杂方块）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只识别并保存 debug.png，不点击")
    parser.add_argument("--debug", action="store_true", help="每次识别都保存 debug.png")
    parser.add_argument("--shot", action="store_true", help="只截一张图保存为 screen.png")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        phone = Phone(serial=args.serial, adb_path=args.adb)
    except Exception as exc:
        print(f"[错误] 连接手机失败：{exc}")
        return 1

    print(f"已连接设备：{phone.serial}")

    try:
        image = phone.screencap()
    except Exception as exc:
        print(f"[错误] 截图失败：{exc}")
        return 1

    if args.shot:
        if imwrite_unicode(Path("screen.png"), image):
            print("已保存 screen.png，可用来制作棋子模板。")
            return 0
        print("[错误] 保存 screen.png 失败。")
        return 1

    h, w = image.shape[:2]
    if h <= w:
        print(f"[错误] 当前截图尺寸为 {w}x{h}，不是竖屏。请保持游戏竖屏后重试。")
        return 1

    print(f"截图尺寸：{w}x{h}")
    print(f"按压系数：{args.coefficient}，按 Ctrl+C 可随时停止。")

    fail_count = 0
    anomaly_count = 0
    jump_count = 0
    last_direction = None
    last_landing_error = 0.0

    if args.reset_calibration:
        save_offset(0.0)
        auto_offset = args.offset_ms
    elif abs(args.offset_ms) > 1e-9:
        # 命令行显式给了 offset，以命令行为准。
        auto_offset = args.offset_ms
    else:
        auto_offset = load_saved_offset()

    try:
        while True:
            # 0. 前台检查：聊天/文件传输界面绝不执行点击。
            if not phone.game_in_foreground():
                print(
                    "[自动修复] 当前前台不是《跳一跳》小游戏"
                    "（可能在微信聊天/文件传输/其他应用），等待 3 秒后重试。"
                )
                if args.once:
                    break
                time.sleep(3.0)
                continue

            # 1. 获取实时画面
            try:
                image = phone.screencap()
            except Exception as exc:
                print(f"[错误] 截图失败：{exc}")
                return 1

            # 2. 识别棋子
            player = find_player_by_color(image)

            # 3. 识别目标方块（必须先找到棋子，才能判断“上方”）
            if player is None:
                target = None
            elif args.manual:
                target = manual_click_target(image)
            else:
                target = find_target(image, player, last_direction)

            # 4. 识别失败处理
            if player is None or target is None:
                fail_count += 1
                missing = "棋子" if player is None else "目标方块"
                debug_image = annotate(image, player, target, text=f"miss: {missing}")
                imwrite_unicode(Path("debug.png"), debug_image)

                print(
                    f"[识别失败 {fail_count}/{args.max_fail}] 未找到{missing}，"
                    "已保存 debug.png"
                )

                if fail_count >= args.max_fail:
                    print(
                        "连续识别失败，自动尝试重开并继续。"
                        "若前台不是游戏，会等待游戏回到前台。"
                    )
                    if args.once:
                        break
                    if attempt_recovery(phone, "连续识别失败"):
                        fail_count = 0
                        anomaly_count = 0
                    else:
                        fail_count = 0
                    continue

                time.sleep(1.0)
                continue

            fail_count = 0

            # 5. 计算两点距离和按压时长
            target_x, target_y = get_target_point(image, target, args.white_bias)
            last_direction = (target_x - player.cx, target_y - player.bottom)
            distance = math.hypot(
                target_x - player.cx,
                target_y - player.bottom,
            )
            # 距离越远，需要的系数略大；距离越近，系数略小。
            # 这样近方块不会因为固定 1.35 过冲，远方块也不会偏短。
            effective_coefficient = args.coefficient + args.distance_slope * (
                distance - 450.0
            )
            effective_coefficient = max(
                1.22, min(1.50, effective_coefficient)
            )
            press_ms = calc_press_ms(
                distance, effective_coefficient, auto_offset
            )

            # 安全闸：距离过近/过远，或棋子已经是横向倒下的姿态，
            # 都不要再按，直接按识别失败处理，避免游戏结束后乱跳。
            if (
                distance < 110.0
                or distance > w * 0.92
                or player.h < player.w * 0.90
            ):
                anomaly_count += 1
                debug_image = annotate(
                    image,
                    player,
                    target,
                    (target_x, target_y),
                    text="unstable jump",
                )
                imwrite_unicode(Path("debug.png"), debug_image)
                print(
                    f"[异常目标 {anomaly_count}/{args.max_fail}] "
                    f"棋子=({player.cx:.0f},{player.bottom}) "
                    f"距离={distance:.1f}px，疑似已结束或误检，不执行点击。"
                )
                if anomaly_count >= args.max_fail:
                    print(
                        "连续异常目标，自动等待并重新识别。"
                        "如果棋子已经倒下，会尝试重开。"
                    )
                    if args.once:
                        break
                    if player.h < player.w * 0.90:
                        if attempt_recovery(phone, "棋子倒下"):
                            fail_count = 0
                            anomaly_count = 0
                            continue
                    anomaly_count = 0
                    time.sleep(2.0)
                    continue
                time.sleep(1.0)
                continue

            print(
                f"[第 {jump_count + 1} 跳] "
                f"棋子=({player.cx:.0f}, {player.bottom}) "
                f"目标=({target_x:.0f}, {target_y:.0f}) "
                f"距离={distance:.1f}px "
                f"按压={press_ms}ms"
            )

            # 保存“起跳前”的画面和日志。失败后 debug.png 往往已经是
            # 游戏结束/排行榜，last_jump.png 才能用于复盘这一跳。
            pre_jump = annotate(
                image,
                player,
                target,
                (target_x, target_y),
                text=f"jump {jump_count + 1}  {distance:.0f}px  {press_ms}ms",
            )
            imwrite_unicode(Path("last_jump.png"), pre_jump)

            log_path = Path("jumps.csv")
            log_is_new = not log_path.exists()
            try:
                with log_path.open("a", newline="", encoding="utf-8-sig") as fp:
                    writer = csv.writer(fp)
                    if log_is_new:
                        writer.writerow(
                            [
                                "time",
                                "jump",
                                "player_x",
                                "player_bottom",
                                "target_x",
                                "target_y",
                                "distance",
                                "press_ms",
                                "coefficient",
                                "offset_ms",
                            ]
                        )
                    writer.writerow(
                        [
                            datetime.now().isoformat(timespec="seconds"),
                            jump_count + 1,
                            f"{player.cx:.1f}",
                            player.bottom,
                            f"{target_x:.1f}",
                            f"{target_y:.1f}",
                            f"{distance:.1f}",
                            press_ms,
                            effective_coefficient,
                            auto_offset,
                        ]
                    )
            except OSError:
                pass

            # 6. 调试模式下保存标注图
            if args.debug or args.dry_run:
                debug_image = annotate(
                    image,
                    player,
                    target,
                    (target_x, target_y),
                    text=f"dist={distance:.0f}px  press={press_ms}ms",
                )
                imwrite_unicode(Path("debug.png"), debug_image)

            # 7. dry-run 只识别，不点击
            if args.dry_run:
                print("dry-run：识别完成，未点击。请查看 debug.png 中的绿框和红框。")
                break

            # 8. 模拟长按
            # 按在棋子中心而不是棋子底部，避免误触 Android 底部手势区。
            try:
                phone.long_press(player.cx, player.cy, press_ms)
            except Exception as exc:
                print(f"[错误] 模拟长按失败：{exc}")
                return 1

            jump_count += 1
            anomaly_count = 0

            if args.once:
                print("已完成一次跳跃，退出。")
                break

            # 9. 实时监控落地：看到棋子重新站稳才进入下一跳；
            #    如果棋子消失/横向倒下，立即停止，不再乱按。
            if not args.no_monitor:
                landed, monitor_image = wait_for_landing(
                    phone,
                    timeout=max(3.0, args.delay + 1.5),
                )
                if landed is None:
                    print("[监控] 棋子消失或倒下，判定这一跳失败。")
                    if monitor_image is not None:
                        imwrite_unicode(
                            Path("debug_monitor.png"),
                            annotate(monitor_image, text="game over"),
                        )

                    # 失败自动修正：按这一跳按压时长增加少量补偿。
                    # 多次“跳短”会逐步增大 offset；上限 30ms，避免失控。
                    if not args.no_auto_tune:
                        step = max(3, min(8, int(round(press_ms * 0.02))))
                        old_offset = auto_offset
                        auto_offset = min(30.0, auto_offset + step)
                        print(
                            f"[修复] 自动补偿 +{step}ms："
                            f"offset {old_offset:.0f} -> {auto_offset:.0f}ms"
                        )
                        save_offset(auto_offset)

                    # 游戏结束自动点“再玩一局”，然后继续。
                    if not args.no_auto_restart and try_restart_game(phone):
                        fail_count = 0
                        anomaly_count = 0
                        print("[修复] 已自动重开一局，继续。")
                        continue

                    print("[自动修复] 未找到重开按钮，等待 3 秒后重试。")
                    if args.once:
                        break
                    time.sleep(3.0)
                    continue
                print(
                    f"[监控] 已落地 棋子=({landed.cx:.0f}, {landed.bottom})"
                )

                # 落地偏差自动校正：找脚下方块中心，沿刚才的跳跃方向
                # 判断偏短还是偏长，下一次跳跃自动调整 offset。
                if (
                    not args.no_auto_tune
                    and last_direction is not None
                    and monitor_image is not None
                ):
                    block = find_block_under_player(monitor_image, landed)
                    if block is not None:
                        bx, by = _block_top_center(block)
                        vx = bx - landed.cx
                        vy = by - landed.bottom
                        dir_len = math.hypot(last_direction[0], last_direction[1])
                        if dir_len > 1e-6 and math.hypot(vx, vy) > 1e-6:
                            along = (
                                last_direction[0] * vx + last_direction[1] * vy
                            ) / dir_len
                            print(
                                f"[校正] 落地沿跳跃方向偏移 {along:+.1f}px"
                                f"（正=偏短，负=偏长）"
                            )
                            # 比例控制：落地偏差多少像素，就按
                            # “毫秒/像素”系数换算成下一跳要补的毫秒数。
                            # 用上一跳误差做低通，减少单帧误检导致的抖动。
                            filtered_error = (
                                along * 0.65 + last_landing_error * 0.35
                            )
                            last_landing_error = along

                            if abs(filtered_error) >= 8.0:
                                correction = (
                                    filtered_error * effective_coefficient * 0.80
                                )
                                correction = max(-40.0, min(40.0, correction))
                                old_offset = auto_offset
                                auto_offset = max(
                                    -80.0, min(80.0, auto_offset + correction)
                                )
                                print(
                                    f"[计算] 下一跳力度修正 "
                                    f"{correction:+.1f}ms（滤波偏差 "
                                    f"{filtered_error:+.1f}px）：offset "
                                    f"{old_offset:.1f} -> {auto_offset:.1f}ms"
                                )
                                save_offset(auto_offset)
                            else:
                                print("[计算] 偏差在 ±8px 内，保持当前力度。")
                        imwrite_unicode(
                            Path("last_landing.png"),
                            annotate(
                                monitor_image,
                                landed,
                                block,
                                (bx, by),
                                text=f"landing {_block_top_center(block)[0]:.0f}",
                            ),
                        )
            else:
                time.sleep(max(0.5, args.delay))

    except KeyboardInterrupt:
        print("\n已手动停止。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
