#!/bin/bash
# SZU 网络守护 · macOS 一键启动脚本
# 双击运行：自动检查 Python → 安装依赖 → 在菜单栏启动守护
# 若提示“来自身份不明的开发者”，请右键本文件 → 打开

cd "$(dirname "$0")" || exit 1

echo "== SZU 网络守护（macOS）=="
echo ""

# 1) 找到可用的 Python 3
PY=""
for candidate in python3 /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
            PY="$candidate"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "[错误] 未找到 Python 3.10+。"
    echo "请从 https://www.python.org/downloads/ 安装官方 Python（默认包含 tkinter），再重新双击本脚本。"
    read -r -p "按回车键关闭…" _
    exit 1
fi
echo "使用 Python：$($PY --version 2>&1)"

# 2) 检查 tkinter（设置窗口需要；系统自带的 /usr/bin/python3 通常没有）
if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
    echo "[错误] 当前 Python 缺少 tkinter 组件。"
    echo "请安装 python.org 官方 Python 3（安装时保持默认勾选即可），或使用 Anaconda。"
    read -r -p "按回车键关闭…" _
    exit 1
fi

# 3) 安装依赖（先常规安装，失败再退回用户目录）
echo "正在检查 / 安装依赖（首次需要联网）…"
if ! "$PY" -m pip install -q -r requirements.txt 2>/dev/null; then
    if ! "$PY" -m pip install -q --user -r requirements.txt; then
        echo "[错误] 依赖安装失败，请检查网络后重试。"
        read -r -p "按回车键关闭…" _
        exit 1
    fi
fi

# 4) 后台启动守护（关闭本终端窗口不影响运行）
echo "正在启动守护…"
nohup "$PY" main.py >> "$HOME/Library/Application Support/SZUNetworkGuardian/launch.out" 2>&1 &
APP_PID=$!
sleep 4

if kill -0 "$APP_PID" 2>/dev/null; then
    echo ""
    echo "[完成] 守护已在菜单栏运行，屏幕右上角出现图标即表示成功。"
    echo "       首次使用请点击图标 → 设置… 填写校园网账号。"
    echo "       之后可在设置中勾选「开机自动启动」，无需再手动运行本脚本。"
    echo "       退出守护：点击菜单栏图标 → 退出守护。"
else
    echo ""
    echo "[失败] 守护未能启动，错误信息："
    tail -5 "$HOME/Library/Application Support/SZUNetworkGuardian/launch.out" 2>/dev/null
fi
echo ""
read -r -p "按回车键关闭本窗口…" _
