@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo TEST ROZLISENI KAMERY EYE
echo  1) Zavri server (okno se serverem), kamera smi mit jen jednoho uzivatele.
echo  2) Bryle poloz na stul proti vytistenemu textu / obrazovce (cca 50-100 cm), ve svetle.
echo.
pause
python eye_res_test.py
pause
