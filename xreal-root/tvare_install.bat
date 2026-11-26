@echo off
rem Modul Tvare pro koren: knihovny (OpenCV, numpy, cryptography) a modely YuNet + SFace z OpenCV Zoo do models\faces.
rem Pokud uz modely stahl Ariel (ariel\tools\tvare_install.bat), koren je najde i tam.
cd /d "%~dp0"
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
python -c "from koren.faces import missing; m = missing(); print('  modul Tvare: ' + ('chybi ' + ', '.join(m) if m else 'vse pripraveno'))"
python -m unittest tests.test_faces
pause
