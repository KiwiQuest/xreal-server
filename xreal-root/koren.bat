@echo off
rem XREAL Koren - spusti korenovou aplikaci do systray (bez okna konzole)
cd /d "%~dp0"
start "" pythonw koren.py %*
