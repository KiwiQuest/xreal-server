# Kontrola zraku: otevre static/oko.html pres celou plochu bryli (monitor 3840x1080 nebo jiny nez hlavni).
Add-Type -AssemblyName System.Windows.Forms
$screens = [System.Windows.Forms.Screen]::AllScreens
$s = $screens | Where-Object { $_.Bounds.Width -ge 3000 -and $_.Bounds.Height -le 1200 } | Select-Object -First 1
if (-not $s) { $s = $screens | Where-Object { -not $_.Primary } | Select-Object -First 1 }
if (-not $s) { $s = [System.Windows.Forms.Screen]::PrimaryScreen }
$b = $s.Bounds
Write-Host "Displej: $($s.DeviceName) $($b.Width)x$($b.Height) na $($b.X),$($b.Y)"
$page = (Resolve-Path (Join-Path $PSScriptRoot '..\static\oko.html')).Path
$url = 'file:///' + $page.Replace([string][char]92, '/')
$browsers = @(
  "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "${env:LOCALAPPDATA}\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
  "${env:ProgramFiles}\Microsoft\Edge\Application\msedge.exe")
$exe = $browsers | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $exe) { Start-Process $url; exit }
$prof = Join-Path $env:LOCALAPPDATA 'ariel-oko-profile'
Start-Process $exe -ArgumentList @("--user-data-dir=`"$prof`"", "--no-first-run", "--app=$url",
  "--window-position=$($b.X),$($b.Y)", "--window-size=$($b.Width),$($b.Height)", "--start-fullscreen",
  "--autoplay-policy=no-user-gesture-required")

