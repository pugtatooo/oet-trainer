@echo off
cd /d "%~dp0"
echo 正在啟動 OET 訓練營...
pip install flask anthropic requests -q 2>nul
python main.py
pause
