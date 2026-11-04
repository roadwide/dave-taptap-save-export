#!/bin/sh
set -eu
cd -- "$(dirname -- "$0")"
umask 077
if ! command -v python3 >/dev/null 2>&1; then
    echo '请先安装 Python 3.9 或更高版本。' >&2
    exit 1
fi
if [ ! -x .venv/bin/python ]; then
    echo '正在准备本项目的 Python 环境……'
    python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import qrcode' >/dev/null 2>&1; then
    echo '首次运行，正在安装终端二维码依赖……'
    .venv/bin/python -m pip install --disable-pip-version-check --no-input -r requirements.txt
fi
exec .venv/bin/python taptap_save.py qr-export
