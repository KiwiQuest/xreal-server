@echo off
rem Pripravi prostredi pro YOLO (virtualni prostredi .venv v teto slozce).
cd /d "%~dp0"
where python >nul 2>nul || (echo Python nenalezen - nainstaluj Python 3.10+ a zaskrtni "Add to PATH" & pause & exit /b 1)
if not exist .venv (
    echo Vytvarim virtualni prostredi .venv ...
    python -m venv .venv || (echo Chyba pri vytvareni .venv & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements-yolo.txt || (echo Instalace selhala & pause & exit /b 1)
echo.
echo === Kontrola prostredi ===
python -m xreal.yolo
echo.
echo Hotovo. Spoustej program takto:   .venv\Scripts\python run.py
pause
