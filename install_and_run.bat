@echo off
chcp 65001 >nul
echo ============================================================
echo   GHRSST MUR SST 海面水溫系統  ^|  農業部水產試驗所
echo ============================================================
echo.

:: 檢查 conda
where conda >nul 2>&1
if %ERRORLEVEL%==0 (
    echo [1/3] 偵測到 Conda，使用 conda 安裝科學套件 ...
    conda install -c conda-forge -y netCDF4 cartopy scipy requests matplotlib numpy 2>nul
    echo [2/3] 安裝完成.
    echo [3/3] 啟動程式 ...
    python ghrsst_sst_gui.py
    goto :end
)

:: fallback: pip
echo [1/3] 使用 pip 安裝套件 ...
pip install numpy matplotlib netCDF4 requests scipy 2>nul
pip install cartopy 2>nul || echo [!] cartopy pip 安裝失敗，建議改用 conda
echo [2/3] 安裝完成.
echo [3/3] 啟動程式 ...
python ghrsst_sst_gui.py

:end
pause
