@echo off
REM ================================================================
REM  Tropical Pacific Tuna Fishing Ground Prediction System
REM  Fisheries Research Institute, MOA  /  Marine Env. Research Team
REM  Build: 2026-07-14
REM ================================================================

setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0webapp"

set "PORT=8765"
set "URL=http://127.0.0.1:%PORT%"

echo.
echo  ================================================================
echo    Tropical Pacific Tuna Fishing Ground Prediction System
echo    Fisheries Research Institute, MOA
echo  ================================================================
echo.
echo    Coverage : 20S - 20N  /  130E - 150W (crosses 180 dateline)
echo    Data src : Copernicus Marine Service (CMEMS)
echo    Species  : Skipjack tuna / Yellowfin tuna  (ECDF-HSI)
echo  ================================================================
echo.

REM ------------------------------------------------------------------
REM  1. Locate Python
REM ------------------------------------------------------------------
echo  [1/4] Checking Python ...
where python >nul 2>nul
if errorlevel 1 (
    echo  [ERROR] Python not found. Install Python 3.10+
    echo          https://www.python.org/downloads/
    pause & exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo         %%v found.

REM ------------------------------------------------------------------
REM  2. Verify packages  (all output suppressed to avoid encoding noise)
REM ------------------------------------------------------------------
echo.
echo  [2/4] Verifying Python packages ...
python -c "import flask,netCDF4,numpy,requests,scipy,matplotlib,PIL" >nul 2>nul
if errorlevel 1 (
    echo         Missing packages detected. Installing now, please wait...
    python -m pip install -r requirements.txt >nul 2>nul
    if errorlevel 1 (
        echo  ERROR: pip install failed.
        echo        Please run manually: python -m pip install -r requirements.txt
        pause
        exit /b 1
    )
    echo         Packages installed OK.
) else (
    echo         All packages present.
)

REM ------------------------------------------------------------------
REM  3. Check copernicusmarine toolbox
REM ------------------------------------------------------------------
echo.
echo  [3/4] Checking copernicusmarine toolbox ...
python -c "import copernicusmarine" >nul 2>nul
if errorlevel 1 (
    echo         Not found -- installing ...
    python -m pip install copernicusmarine >nul 2>nul
    python -c "import copernicusmarine" >nul 2>nul
    if errorlevel 1 (
        echo  [WARN]  Install failed. Copernicus download may not work.
        echo          Manual fix: pip install copernicusmarine
    ) else (
        echo         copernicusmarine installed OK.
    )
) else (
    echo         copernicusmarine OK.
)
echo         Reminder: run  copernicusmarine login  once if not yet done.

REM ------------------------------------------------------------------
REM  4. Release port, open browser, start server
REM ------------------------------------------------------------------
echo.
echo  [4/4] Starting server on port %PORT% ...

REM Release port if occupied, then open browser (both via PowerShell)
powershell -NoProfile -WindowStyle Hidden -Command "$p=%PORT%; $ids=(netstat -ano|Select-String \":$p \"|Select-String 'LISTENING'|ForEach-Object{($_ -split '\s+')[5]}|Sort -Unique); foreach($id in $ids){if($id){Stop-Process -Id $id -Force -ErrorAction SilentlyContinue}}; Start-Sleep 1; Start-Sleep 3; Start-Process 'http://127.0.0.1:'+$p" >nul 2>nul

REM Open browser after delay
start /B "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 3; Start-Process 'http://127.0.0.1:%PORT%'"

echo.
echo  ================================================================
echo    Server  : %URL%
echo    Browser will open automatically in ~3 seconds.
echo.
echo    Features:
echo      - Download SST / Chl-a / SSHA  (Copernicus Marine)
echo      - Skipjack  ECDF chart + one-click HSI prediction
echo      - Yellowfin ECDF chart + one-click HSI prediction
echo      - Ocean front detection / Time-series animation
echo.
echo    Keep this window open. Press Ctrl+C to stop.
echo  ================================================================
echo.

python app.py --port %PORT% --no-browser

echo.
echo  Server stopped.
pause
endlocal
