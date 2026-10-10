@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
echo ============================================
echo  会话监控控制台（Codex + Pi）
echo ============================================
echo.
echo  提示：Pi 会话需要先安装插件，执行：
echo        python install_pi_plugin.py
echo.
start "" pythonw codex_monitor_gui.py 2>nul
if errorlevel 1 start "" python codex_monitor_gui.py
