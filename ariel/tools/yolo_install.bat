@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Instaluji YOLO (ultralytics + PyTorch) a OpenVINO (Intel CPU/GPU/NPU) - stahuje se nekolik set MB...
python -m pip install --upgrade ultralytics openvino opencv-python numpy
echo.
echo Stahuji model yolo11n.pt a zkousim ho...
python -c "from ultralytics import YOLO; import numpy as np; m=YOLO('yolo11n.pt'); m.predict(np.zeros((540,960,3),'uint8'), verbose=False); print('YOLO OK')"
echo.
echo Zarizeni OpenVINO na tomto notebooku:
python -c "import openvino as ov; print(ov.Core().available_devices)"
echo.
pause
