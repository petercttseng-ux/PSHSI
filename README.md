# 中西太平洋鰹鮪圍網漁場預測系統（網頁版）

> 農業部水產試驗所 漁海況研究小組
> Fisheries Research Institute, MOA — Skipjack & Yellowfin Tuna ECDF-HSI Fishing Ground Prediction System

本系統已由原 Tkinter 桌面應用（`ghrsst_sst_gui.py`）完整重構為 **Flask + Plotly.js 網頁系統**（`webapp/`），可於瀏覽器操作全部功能。

## 系統功能

| 面板 | 功能 |
|---|---|
| 📡 資料管理 | NASA MUR SST 下載、OSTIA SST（°C 轉換）、GlobColour 海洋水色（Chl-a）、SSHA 海面高度異常、Copernicus Marine 登入、本地 `.nc` 載入與拖放上傳 |
| 🎯 漁場預測 (ECDF-HSI) | 鰹魚（SKJ）／黃鰭鮪（YFT）一鍵預測；以 1998–2007 漁獲–環境資料（SST、Chl-a、SSHA）建立 ECDF 適合度曲線，計算棲地適合度指數 HSI；面板顯示當日環境資料來源日期 |
| 🎨 圖層控制 | 等溫線（可調間距）、海岸線（Natural Earth 50m）、海洋前緣（Cayula-Cornillon）、水色疊圖（≥0.1 mg/m³） |
| 🔭 視圖操作 | 滾輪縮放、拖曳平移、框選放大、視圖重設 |
| 🧰 分析工具 | 測站時間序列、SST 剖面、前緣 GeoJSON 匯出、高解析 PNG 匯出 |
| 📋 訊息記錄 | 後台日誌即時顯示 |

## 快速啟動

```bash
cd webapp
pip install -r requirements.txt
python app.py
```

或於 Windows 直接雙擊根目錄的 `啟動漁場預測系統.bat`。
預設服務位址：`http://127.0.0.1:8765`（自動開啟瀏覽器）。

## 專案結構

```
├── webapp/                  # 網頁系統（主系統）
│   ├── app.py               # Flask 後端 REST API
│   ├── sst_processor.py     # SST 載入、前緣偵測、NASA CMR
│   ├── habitat.py           # ECDF-HSI 棲地模式
│   ├── copernicus.py        # Copernicus Marine（OSTIA / GlobColour / SSHA）
│   ├── earthdata.py         # NASA Earthdata 認證
│   ├── timeseries.py        # 測站時間序列（ERDDAP）
│   ├── templates/index.html # 單頁應用前端
│   └── static/              # css / js / logo
├── habitat_params.json      # 物種 ECDF 參數
├── skipjack-*.csv / yellowfin-*.csv  # 1998–2007 漁獲–環境訓練資料
├── 漁場預測原理及系統操作手冊.pdf
├── 漁場預測-ECDF-HSI-方法與更新說明.md
└── ghrsst_sst_gui.py        # 舊版桌面程式（保留參考）
```

## 憑證設定（不得提交至版本庫）

- **NASA Earthdata token**：依序讀取環境變數 `NASA_EARTHDATA_TOKEN` → `earthdata_token.txt` → `~/.earthdata_token` → `~/.netrc`。
- **Copernicus Marine**：於網頁「Copernicus 登入」按鈕輸入帳密，憑證存於本機使用者目錄。
- `.gitignore` 已排除 token、`.netrc` 與 `.nc` 大檔，請勿手動加入。

## 資料來源

GHRSST MUR L4 v4.1（NASA PO.DAAC）、OSTIA（Copernicus Marine）、GlobColour L4 Chl-a、SSHA（Copernicus Marine）、NOAA ERDDAP（jplMURSST41）。

## 授權與引用

本系統供研究用途。使用請引用：農業部水產試驗所 鰹鮪 ECDF-HSI 漁場預測系統（2026）。
