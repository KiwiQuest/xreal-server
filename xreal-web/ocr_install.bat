@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Instaluji OCR a predcitani: balicky pywinrt pro vestavene OCR a hlasy Windows (male, par MB)...
python -m pip install --upgrade winrt-runtime winrt-Windows.Foundation winrt-Windows.Foundation.Collections ^
  winrt-Windows.Media.Ocr winrt-Windows.Globalization winrt-Windows.Graphics.Imaging ^
  winrt-Windows.Storage.Streams winrt-Windows.Media.SpeechSynthesis
echo.
echo Jazyky OCR v tomto Windows:
python -c "import ocr_engine as o; s=o.status(); print('  engine:', s['backend'], '| jazyky:', s['langs'], '|', s['error'] or 'OK')"
echo.
echo Test cteni (vygenerovany obrazek s textem):
python -c "import cv2,numpy as np,ocr_engine as o; i=np.full((200,1400,3),255,np.uint8); cv2.putText(i,'Hello XREAL 2026 OCR test',(30,120),cv2.FONT_HERSHEY_SIMPLEX,2.2,(0,0,0),4); r=o.recognize(i); print('  precteno:', repr(r['text']), '|', r['ms'], 'ms |', r['lang'])"
echo.
echo Hlasy pro predcitani:
python -c "from winrt.windows.media.speechsynthesis import SpeechSynthesizer as S; [print('  ', v.display_name, v.language) for v in S.all_voices]"
echo.
echo Kdyz v jazycich OCR chybi cs-CZ, spust ocr_cestina.bat jako spravce.
pause
