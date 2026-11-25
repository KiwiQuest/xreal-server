@echo off
rem Povoli prichozi spojeni z tabletu na porty xreal-web (HTTP 8080, WebSocket 8765).
rem Spust PRAVYM TLACITKEM -> "Spustit jako spravce".
net session >nul 2>&1
if errorlevel 1 (
  echo Spust tento soubor jako spravce: pravym tlacitkem - Spustit jako spravce.
  pause
  exit /b 1
)
netsh advfirewall firewall delete rule name="xreal-web" >nul 2>&1
netsh advfirewall firewall add rule name="xreal-web" dir=in action=allow protocol=TCP localport=8080,8765 profile=private,public
echo.
echo Hotovo: porty 8080 a 8765 jsou povolene (pravidlo "xreal-web").
echo Odebrani: netsh advfirewall firewall delete rule name="xreal-web"
pause
