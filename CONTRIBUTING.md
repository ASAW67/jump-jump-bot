# Contributing

欢迎提交 Issue 和 Pull Request。

## 开发环境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 提交前检查

```powershell
python -m py_compile jump_jump.py
python .\jump_jump.py --help
```

请勿提交以下运行时文件：

- `debug*.png`
- `last_jump.png`
- `last_landing.png`
- `jumps.csv`
- `jump_calibration.json`

## 提交内容

- 描述复现步骤、手机分辨率、Android 版本
- 附上脱敏后的 `last_jump.png` 或 `debug.png`
- 说明是识别问题还是按压力度问题
