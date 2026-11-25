@echo off
rem Instalace pro tvare.bat: knihovny + modely YuNet (detekce) a SFace (embedding) z OpenCV Zoo do models\faces
cd /d "%~dp0.."
python -m pip install --upgrade "opencv-python>=4.8" numpy cryptography
if not exist models\faces mkdir models\faces
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$base='https://github.com/opencv/opencv_zoo/raw/main/models';" ^
  "$m=@{'face_detection_yunet_2023mar.onnx'='face_detection_yunet';'face_recognition_sface_2021dec.onnx'='face_recognition_sface'};" ^
  "foreach($f in $m.Keys){ $out='models\faces\'+$f; if((Test-Path $out) -and ((Get-Item $out).Length -gt 100000)){ Write-Host ('  mam '+$f); continue };" ^
  "  Write-Host ('  stahuji '+$f); Invoke-WebRequest -UseBasicParsing -Uri ($base+'/'+$m[$f]+'/'+$f) -OutFile $out;" ^
  "  if((Get-Item $out).Length -lt 100000){ throw ('stazeny soubor je moc maly: '+$f) } }"
if errorlevel 1 ( echo [X] stazeni modelu selhalo & pause & exit /b 1 )
python -m faces selftest
python -c "import cv2; from faces import config, FACES_DATA; from faces.engine import FaceEngine; FaceEngine(config.load(FACES_DATA)); print('  modely OK, OpenCV', cv2.__version__)"
pause
