@echo off
chcp 65001 >nul
rem SZU 网络守护 · Windows 一键启动脚本
rem 双击运行：自动检查 Python → 安装依赖 → 在系统托盘启动守护
rem 若 SmartScreen 提示，点「更多信息 → 仍要运行」

cd /d "%~dp0"

echo == SZU 网络守护（Windows）==
echo.

rem 1) 找到可用的 Python 3
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python 3.10+。
    echo 请从 https://www.python.org/downloads/ 安装官方 Python，
    echo 安装时勾选 "Add python.exe to PATH"，再重新双击本脚本。
    pause
    exit /b 1
)

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [错误] Python 版本过低，需要 3.10 及以上。
    pause
    exit /b 1
)

rem 2) 检查 tkinter（设置窗口需要，官方安装包默认包含）
python -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo [错误] 当前 Python 缺少 tkinter 组件。
    echo 请安装 python.org 官方 Python 3，安装时保持默认勾选即可。
    pause
    exit /b 1
)

rem 3) 安装依赖（先常规安装，失败再退回用户目录）
echo 正在检查 / 安装依赖（首次需要联网）…
python -m pip install -q -r requirements.txt >nul 2>nul
if errorlevel 1 (
    python -m pip install -q --user -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络后重试。
        pause
        exit /b 1
    )
)

rem 4) 托盘方式启动守护（无控制台黑窗）
set LAUNCHER=pythonw
where pythonw >nul 2>nul || set LAUNCHER=python
start "" %LAUNCHER% main.py

echo.
echo [完成] 守护已在系统托盘运行，屏幕右下角（可能收进 ^^^ 箭头里）出现图标即表示成功。
echo        首次使用请右键图标 → 设置… 填写校园网账号。
echo        之后可在设置中勾选「开机自动启动」，无需再手动运行本脚本。
echo        退出守护：右键托盘图标 → 退出守护。
echo.
pause
