# 微信跳一跳 ADB 自动脚本

> Windows + ADB + Python/OpenCV 实现的微信小游戏《跳一跳》自动脚本。  
> 支持自动识别、实时落地监控、失败自动重开、落点比例校正，以及复杂方块手动点选模式。

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)
![License](https://img.shields.io/badge/License-MIT-green)

---

## 功能

- 通过 `adb exec-out screencap -p` 实时抓取手机画面
- 自动识别棋子底部与目标方块顶面中心
- 按像素距离计算长按时间，通过 `input swipe` 模拟按压
- 实时监控落地：棋子站稳才继续，消失/倒下立即停止
- 游戏结束后自动寻找“再玩一局”按钮并重开
- 比例控制器根据落地偏差实时修正下一跳力度
- 校准值持久化到 `jump_calibration.json`
- 复杂方块支持 `--manual` 手动点选目标中心
- 保存起跳前画面、落地画面和 CSV 日志，便于复盘

## 工作原理

```text
adb 截图
   ↓
HSV / 轮廓 / 白点检测
   ↓
棋子底部 + 目标顶面中心
   ↓
距离 × 距离相关系数 + 落地补偿
   ↓
adb shell input swipe x y x y duration
   ↓
实时截图监控落地
   ↓
偏差 → 修正下一跳 offset
```

默认按压模型：

```python
coef = 1.35 + 0.00020 * (distance - 450)
coef = clamp(coef, 1.22, 1.50)
press_ms = distance * coef + offset_ms
```

- 近距离系数略低，减少过冲
- 远距离系数略高，减少偏短
- 落地偏差通过比例控制器修正

## 环境要求

| 项目 | 要求 |
|---|---|
| 系统 | Windows 10/11 |
| Python | 3.9+ |
| 手机 | Android，已开启 USB 调试 |
| ADB | Android Platform-Tools |
| 其他 | 微信已登录，能打开《跳一跳》 |

Python 依赖：

```text
opencv-python>=4.8
numpy>=1.24
adbutils>=2.0
```

## 安装

### 1. 安装 Python 依赖

```powershell
python -m pip install -r requirements.txt
```

### 2. 安装 ADB

下载 Android Platform-Tools，解压后把目录加入 PATH，确认：

```powershell
adb version
adb devices
```

看到设备状态为 `device` 才能运行；`unauthorized` 需要在手机上点“允许 USB 调试”。

### 3. 手机设置

- 打开开发者选项 → USB 调试
- USB 连接电脑，选择“传输文件”
- 手机保持亮屏、解锁
- 打开微信《跳一跳》并停留在游戏画面

## 快速开始

```powershell
Set-Location -LiteralPath "你的项目目录"
python .\jump_jump.py
```

只识别、不点击：

```powershell
python .\jump_jump.py --dry-run
```

只跳一次：

```powershell
python .\jump_jump.py --once
```

多台设备时指定序列号：

```powershell
python .\jump_jump.py --serial 你的设备序列号
```

## 使用模式

### 自动模式

```powershell
python .\jump_jump.py
```

脚本会持续：

```text
识别 → 跳跃 → 监控落地 → 修正力度 → 下一跳
```

游戏结束、识别失败、棋子倒下时自动尝试重开并继续。

### 手动点选模式（复杂方块推荐）

```powershell
python .\jump_jump.py --manual
```

每一跳会弹出截图窗口：

1. 用鼠标点击目标方块**顶面中心**
2. 脚本自动计算距离和按压时间
3. 自动长按，继续下一跳

适合魔方、礼物盒、圆桌、药罐等自动识别不稳定的方块。

> 如果窗口没有出现，检查任务栏中的 `Click target center` 窗口；新版会尝试置顶。按 `ESC` 取消当前跳。

### 调试模式

```powershell
python .\jump_jump.py --dry-run --debug
```

只保存 `debug.png`，不操作手机。

## 参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--serial` | 自动 | 设备序列号 |
| `--adb` | PATH | adb 可执行文件路径 |
| `--coefficient` | 1.35 | 基础按压系数 |
| `--distance-slope` | 0.0002 | 距离越远系数越大的斜率 |
| `--offset-ms` | 0 | 固定补偿毫秒数 |
| `--white-bias` | 0.30 | 白色方块顶面中心高度比例 |
| `--delay` | 1.5 | 跳跃后等待秒数 |
| `--no-monitor` | 关闭 | 关闭落地实时监控 |
| `--no-auto-restart` | 关闭 | 关闭失败自动重开 |
| `--no-auto-tune` | 关闭 | 关闭落地/失败自动补偿 |
| `--reset-calibration` | 关闭 | 清除已保存的自动补偿 |
| `--max-fail` | 5 | 连续失败多少次后进入自动修复 |
| `--once` | 关闭 | 只跳一次 |
| `--manual` | 关闭 | 每跳手动点选目标中心 |
| `--dry-run` | 关闭 | 只识别，不点击 |
| `--debug` | 关闭 | 每跳保存 debug.png |
| `--shot` | 关闭 | 只截图保存 screen.png |

## 自动修正

### 落地比例控制

落地后找到脚下方块，计算方块中心相对棋子底部的偏移，并投影到刚才的跳跃方向：

```python
filtered = 0.65 * error + 0.35 * last_error
correction = filtered * coefficient * 0.80
offset += clamp(correction, -40, 40)
offset = clamp(offset, -80, 80)
```

- 正值：偏短，增加下一次按压
- 负值：偏长，减少下一次按压
- ±8px 内不调整
- 校准写入 `jump_calibration.json`，下次启动继续使用

### 失败修复

- 落地监控发现棋子消失/倒下：增加按压补偿，自动点“再玩一局”
- 连续识别失败：尝试自动重开；前台不是游戏时等待，不执行点击
- 找不到重开按钮：等待后重试，不退出

## 输出文件

| 文件 | 说明 |
|---|---|
| `debug.png` | 识别调试图 |
| `last_jump.png` | 最后一跳起跳前的标注图 |
| `last_landing.png` | 最后一跳落地标注图 |
| `debug_monitor.png` | 游戏结束时的监控画面 |
| `jumps.csv` | 每跳距离、按压、系数、offset |
| `jump_calibration.json` | 持久化校准值 |

这些文件默认被 `.gitignore` 忽略。

## 常见问题

### 识别不到棋子

- 确认游戏在前台、屏幕未锁定
- 用 `--shot` 截图检查画面
- 换非常规皮肤后颜色识别可能失效，需要调整 `find_player_by_color` 中的 HSV 阈值

### 识别不到目标

- 用 `--dry-run --debug` 查看 `debug.png`
- 复杂方块改用 `--manual`
- 调整 `--white-bias` 或 `--distance-slope`

### 总是偏短

```powershell
python .\jump_jump.py --offset-ms 20
```

或增大系数：

```powershell
python .\jump_jump.py --coefficient 1.40
```

### 总是偏长

```powershell
python .\jump_jump.py --offset-ms -20
```

或减小系数：

```powershell
python .\jump_jump.py --coefficient 1.30
```

### 近距离过大、远距离偏短

增大距离斜率：

```powershell
python .\jump_jump.py --distance-slope 0.0003
```

### 自动补偿越跑越大

说明落地方块中心检测被误检带偏，先关闭自动补偿：

```powershell
python .\jump_jump.py --no-auto-tune --reset-calibration
```

再用手动模式确认基准力度。

## 发布到 GitHub

```powershell
cd wechat-jump-jump-bot
git init -b main
git add .
git commit -m "Initial commit: WeChat Jump Jump ADB bot"
git remote add origin https://github.com/<你的用户名>/wechat-jump-jump-bot.git
git push -u origin main
```

运行时生成的 `debug.png`、`last_jump.png`、`jumps.csv`、`jump_calibration.json` 已被 `.gitignore` 忽略。

## 免责声明

- 本项目仅用于学习和自动化技术研究。
- 使用脚本可能违反游戏用户协议，存在账号风险。
- 请自行承担使用后果，作者不对任何封号、数据丢失或设备问题负责。
- 请勿用于商业代练、刷分等用途。

## License

[MIT](LICENSE)
