@echo off
chcp 65001 >nul
rem Doinstaluje do Windows ceske OCR (soucast jazyka cestina). Spust PRAVYM TLACITKEM -> "Spustit jako spravce".
rem Nic jineho nemeni. Odebrani: Nastaveni > Cas a jazyk > Jazyk a oblast > Cestina > Moznosti jazyka.
net session >nul 2>&1
if errorlevel 1 (
  echo Spust tento soubor jako spravce: pravym tlacitkem - Spustit jako spravce.
  pause
  exit /b 1
)
echo Stav OCR jazyku:
powershell -NoProfile -Command "Get-WindowsCapability -Online | Where-Object Name -like 'Language.OCR*' | Format-Table Name, State -AutoSize"
echo Instaluji Language.OCR cs-CZ ...
powershell -NoProfile -Command "Get-WindowsCapability -Online | Where-Object Name -like 'Language.OCR~~~cs-CZ*' | Add-WindowsCapability -Online"
echo.
echo Hotovo. Restartuj xreal-web server.
pause
