@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Instaluji/kontroluji hidapi, opencv-python a pygrabber...
python -m pip install --quiet hidapi opencv-python pygrabber
python eye_rgb_enable.py %* > rgb_out_enable.txt 2>&1
type rgb_out_enable.txt
echo.
echo HOTOVO - vystup je v rgb_out_enable.txt
pause
