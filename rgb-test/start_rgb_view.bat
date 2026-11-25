@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Kontroluji knihovny...
python -m pip install --quiet hidapi opencv-python pygrabber imageio-ffmpeg
echo.
echo === 1/2 Zapinam RGB kameru (pro jistotu znovu) ===
python eye_rgb_enable.py --no-probe
echo.
echo === 2/2 Zachytavam a dekoduji obraz ===
python eye_rgb_view.py
echo.
echo HOTOVO - report je v rgb_out\view_report.txt
pause
