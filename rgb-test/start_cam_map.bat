@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install --quiet hidapi pygrabber
python eye_cam_map.py
echo.
echo HOTOVO
pause
