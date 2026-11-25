@echo off
rem Tvare: zapis, porovnani, kontrola a mazani. Uklada jen embeddingy (zadne fotky venku), sifrovane.
rem Prvni spusteni: tools\tvare_install.bat
cd /d "%~dp0"
if not "%~1"=="" ( python -m faces %* & goto :eof )
:menu
echo.
python -m faces status
echo.
echo  1  zapsat tvar z XREAL Eye (musi bezet ariel.bat)
echo  2  zapsat tvar z webkamery
echo  3  seznam zaznamu
echo  4  porovnat databazi samu se sebou
echo  5  kontrola podezrelych duplicit (rozhoduje clovek)
echo  6  smazat zaznam
echo  7  prepnout rezim lab / field
echo  8  smazat VSE (trezor i klic)
echo  9  selftest
echo  0  konec
set "c="
set /p c=Volba: 
if "%c%"=="1" python -m faces enroll --camera eye --preview
if "%c%"=="2" python -m faces enroll --camera 0 --preview
if "%c%"=="3" python -m faces list
if "%c%"=="4" python -m faces dedupe
if "%c%"=="5" python -m faces review
if "%c%"=="6" goto del
if "%c%"=="7" goto mode
if "%c%"=="8" python -m faces wipe
if "%c%"=="9" python -m faces selftest
if "%c%"=="0" goto :eof
goto menu
:del
set "ids="
set /p ids=ID (vic oddel mezerou): 
if not "%ids%"=="" python -m faces delete %ids%
goto menu
:mode
set "m="
set /p m=lab nebo field: 
if not "%m%"=="" python -m faces mode %m%
goto menu
