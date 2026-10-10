@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
echo ============================================
echo  Codex 会话监控 - 按 Ctrl+C 停止
echo ============================================
python codex_monitor.py
pause
