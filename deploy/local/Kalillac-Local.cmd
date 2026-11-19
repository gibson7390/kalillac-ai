@echo off
rem Kalillac local staging on this Windows machine (runs in WSL). Double-click to start and open the browser.
rem   Kalillac-Local.cmd            start, then open http://localhost:8010
rem   Kalillac-Local.cmd stop       stop the application, the front server and its PostgreSQL
rem   Kalillac-Local.cmd status     what runs, the demo accounts, which providers are simulated
rem   Kalillac-Local.cmd setup      first time only (see deploy\local\README.md)
rem   Kalillac-Local.cmd spaces-allow you@example.com
setlocal
set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=start"
pushd "%~dp0..\.."
set "REPO=%CD%"
popd
for /f "usebackq delims=" %%P in (`wsl wslpath -a "%REPO%"`) do set "WREPO=%%P"
for /f "usebackq delims=" %%H in (`wsl -e bash -lc "printf %%s \"$HOME\""`) do set "WHOME=%%H"
if not defined KALILLAC_LOCAL_PYTHON set "KALILLAC_LOCAL_PYTHON=%WHOME%/kf15/bin/python"
wsl -u root -e bash -lc "cd '%WREPO%/candidate' && '%KALILLAC_LOCAL_PYTHON%' -B tools/local_staging.py %ACTION% %2"
set "CODE=%ERRORLEVEL%"
if "%CODE%"=="0" if /i "%ACTION%"=="start" start "" "http://localhost:8010/"
if "%CODE%"=="0" if /i "%ACTION%"=="setup" start "" "http://localhost:8010/"
if not "%CODE%"=="0" pause
endlocal & exit /b %CODE%
