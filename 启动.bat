@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "LUBO_PYTHON=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%LUBO_PYTHON%" (
    echo 未找到录播助手的 Python 环境。
    echo 请参照 README.md 创建 .venv 并安装依赖。
    pause
    exit /b 1
)

start "" "%LUBO_PYTHON%" "%~dp0main.py"
