#!/bin/bash
cd "$(dirname "$0")"
echo "正在啟動 OET 訓練營..."
pip3 install flask anthropic requests -q 2>/dev/null || pip install flask anthropic requests -q
python3 main.py
