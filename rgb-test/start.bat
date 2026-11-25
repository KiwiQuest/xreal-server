@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Instaluji/kontroluji opencv-python a pygrabber...
python -m pip install --quiet opencv-python pygrabber
python rgb_probe.py %*
echo.
pause
