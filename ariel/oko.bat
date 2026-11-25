@echo off
rem Kontrola zraku v bryli (Full SBS): kazde oko zvlast. Vysledky zustavaji v profilu prohlizece. Zavrit: Alt+F4.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\oko.ps1"
