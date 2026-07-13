# GHRSST MUR SST 海面水溫展示系統 — Web 版

> 農業部水產試驗所 漁海況研究小組
> Marine Environmental Research, Fisheries Research Institute, MOA

從原本 Tkinter 桌面應用優化重構為現代化網頁版本，整合水產試驗所 logo，
並改善視覺、互動體驗與部署便利性。

## 功能

- **互動地圖** — Plotly.js 提供滑鼠懸停顯示經緯度與水溫，滾輪縮放、拖曳平移、框選放大
- **NASA 自動下載** — 一鍵下載最新 MUR L4 v4.1 granule（西太平洋 114–162°E, 17–56°N）
- **本地檔案管理** — 列出 `mur_data/` 內所有 `.nc`，點選即載入；支援拖放上傳
- **圖層控制** — 等溫線（可調間距）、海岸線（cartopy 50m Natural Earth）、海洋前緣
- **Cayula-Cornillon 偵測** — 雙峰直方圖前緣偵測，後台執行不阻塞 UI
- **PNG 匯出** — 高解析度 (2× scale) 出版級圖片
- **即時統計** — 最高/平均/最低 SST、資料形狀、降採樣比
- **訊息記錄** — 後台日誌即時顯示於 sidebar

## 啟動

雙擊專案根目錄的 `start_web.bat`，或：

```bash
cd webapp
pip install -r requirements.txt
python app.py
```

預設於 `http://127.0.0.1:8765` 提供服務，並自動開啟瀏覽器。

## 架構

```
webapp/
├── app.py              Flask 後端 (REST API)
├── sst_processor.py    SST 處理核心 (loaded NetCDF, 前緣偵測, NASA CMR)
├── templates/
│   └── index.html      單頁應用主頁
└── static/
    ├── css/style.css   深海主題樣式
    ├── js/app.js       前端互動邏輯 (Plotly.js)
    └── img/fri_logo.jpg
```

## API 端點

| 路徑 | 方法 | 功能 |
|---|---|---|
| `/api/status` | GET | 目前載入狀態 |
| `/api/files`  | GET | 本機 .nc 列表 |
| `/api/load`   | POST | 載入指定本機檔 |
| `/api/upload` | POST | 上傳 .nc |
| `/api/download` | POST | 啟動 NASA 下載（async） |
| `/api/download/status` | GET | 下載進度 |
| `/api/sst` | GET | 取得 SST 網格 |
| `/api/fronts` | GET/POST | 取得/啟動前緣偵測 |
| `/api/coastline` | GET | 海岸線 polygon |
| `/api/logs` | GET | 訊息日誌 |

## 憑證設定（2026-07 安全更新）

原始碼已不含任何帳號、密碼或 token。NASA Earthdata token 依下列順序讀取：

1. 環境變數 `NASA_EARTHDATA_TOKEN`
2. 專案根目錄 `earthdata_token.txt`（建議，已附範本）
3. `~/.earthdata_token`
4. 皆未設定時退回 `~/.netrc`（machine urs.earthdata.nasa.gov）

Token 產生位置：https://urs.earthdata.nasa.gov/users/<帳號>/user_tokens
系統啟動與下載前會自動檢查 token 是否過期，狀態可由 `GET /api/token_status` 查詢。

TLS 憑證驗證已全面啟用。若機關網路有 SSL 攔截，請設定
`REQUESTS_CA_BUNDLE=<機關CA憑證路徑>`，勿再關閉驗證。

## 新功能（2026-07）

**🎞 時間序列動畫** — 選擇起迄日期（上限 92 天）與解析度，經 NOAA ERDDAP
（jplMURSST41，同為 MUR v4.1）下載西太平洋子集（每天僅數 MB，免 NASA 認證），
滑桿瀏覽或自動播放 SST 逐日演變，可匯出 GIF。每日子集快取於
`mur_data/timeseries/`，重複請求不重新下載。

**🌡 距平模式** — 勾選「距平模式」後，顯示當日 SST 減去前 N 日（預設 30 日，
可調）均值的異常圖，紅藍發散色階置中於 0°C，用於海洋熱浪與寒害判讀，
GIF 匯出亦支援距平。

**📍 定點監測** — 勾選「點擊地圖新增測站」後點圖（或直接輸入座標），
設定高/低溫閾值。測站以 ⭐ 顯示於地圖，列表顯示最新水溫並於超標時
顯示紅色警報，「📈 序列」開啟近 90 日走勢圖（含閾值線）。
設定存於專案根目錄 `stations.json`。

**每日通報** — `python check_stations.py --html report.html` 產生通報
（文字＋HTML），有警報時退出碼為 2，可搭配排程器每日執行並寄送 Gmail。

### 新增 API

| 路徑 | 方法 | 功能 |
|---|---|---|
| `/api/series/load` | POST | 下載日期區間序列（背景執行） |
| `/api/series/status` | GET | 序列下載進度 |
| `/api/series/meta` | GET | 序列日期清單與逐日統計 |
| `/api/series/frame` | GET | 單日影格（`anomaly=1` 為距平） |
| `/api/series/export.gif` | GET | 匯出 GIF 動畫 |
| `/api/stations` | GET/POST | 測站清單／新增 |
| `/api/stations/<id>` | DELETE | 刪除測站 |
| `/api/stations/<id>/series` | GET | 點位時間序列＋警報判定 |
| `/api/token_status` | GET | NASA token 狀態 |

## 分析工具（2026-07 第二批）

**📏 剖面工具** — 勾選「剖面模式」後在地圖點兩點，顯示沿線 SST 剖面與
|梯度|（雙軸圖），自動標出最大梯度值與位置以量化鋒面強度
（≥0.05 °C/km 標示為顯著鋒面）。支援主資料與時間序列影格。

**🔍 前緣偵測 v2** — Cayula-Cornillon 加入鄰域連貫性檢驗（cohesion，
兩水團空間連貫度需 ≥0.90，剔除雲遮/雜訊窗）、front 像素改為水團邊界、
小物件過濾（<12 px 連通元件剔除，無 scipy 時有純 numpy 後備）。
「🗺 前緣 GeoJSON 匯出」將前緣向量化為 LineString（EPSG:4326），
可直接匯入 QGIS / ArcGIS。

**📦 區域匯出** — 依目前地圖縮放範圍裁切匯出 CSV（過大自動降採樣）、
NetCDF（CF-1.6）或 GeoTIFF（需 `pip install rasterio`），存於
`mur_data/exports/` 並自動下載。

**🛰 多資料集** — 時間序列動畫可切換：MUR SST 1km、NOAA OISST v2.1
25km（1981 至今，適合長期距平）、NOAA Geo-Polar Blended 5km（OSTIA
同級）、VIIRS 葉綠素-a gap-filled 9km（log 色階）。另可在 SST 圖上
勾選「葉綠素等值線疊圖」（0.3–3 mg/m³ 綠線），SST 鋒面×高葉綠素
交會處即為潛在漁場。

### 新增 API（第二批）

| 路徑 | 方法 | 功能 |
|---|---|---|
| `/api/datasets` | GET | 可用資料集清單 |
| `/api/transect` | POST | 沿線剖面與梯度 |
| `/api/fronts/geojson` | GET | 前緣向量 GeoJSON 下載 |
| `/api/export` | GET | 區域裁切匯出 csv/nc/tif |
| `/api/overlay/chl` | GET | 單日葉綠素子集（疊圖用） |

## SSH 與表面海流（2026-07 第三批）

時間序列資料集新增兩項（來源 NOAA CoastWatch 測高融合產品
S-3A/B、CryoSat-2、Jason-2/3、SARAL，0.25°，近即時每日更新）：

- **SSH 海面高度距平（SLA）** — 發散色階置中 0 m，等值線固定 0.1 m
  間距（SSH 等高線≈地轉流線，暖渦/冷渦一目瞭然），支援動畫與 GIF。
- **表面地轉流速** — Plasma 色階流速圖，適合觀察黑潮主軸強度變化。

**🌀 海流向量疊圖** — 分析工具卡勾選後，在任何底圖（SST、SLA、葉綠素…）
疊上白色流向箭頭（≤900 支自動子取樣，箭長∝流速），滑鼠懸停顯示
流速 (m/s) 與流向（海洋慣例：去向，北=0° 順時針）。動畫換日時
箭頭自動跟隨影格日期更新。

新增 API：`GET /api/overlay/currents?date=YYYY-MM-DD` 回傳子取樣
向量場（lons/lats/u/v/speed/dir）。

## 官方距平與累積熱壓力（2026-07 第四批）

時間序列動畫新增兩項官方 GHRSST 加值產品（來源 coastwatch.pfeg ERDDAP，近即時每日更新）：

- **MUR SST 距平（官方氣候基準）— `jplMURSST41anom1day`（`sstAnom`，1km）**
  以 NASA 20 餘年氣候平均為基準的正式距平場，取代原本「前 N 日均值」近似。
  紅藍發散色階置中 0，海洋熱浪（正距平）與寒害（負距平，養殖寒害預警）
  判讀的正確基準。

- **累積熱壓力 DHW — `NOAA_DHW`（`CRW_DHW`，5km）**
  NOAA Coral Reef Watch 的 Degree Heating Weeks（過去 12 週累積熱壓力，
  單位 °C·週）。專屬熱壓力色階（白→黃→橙→紅→深紅紫），colorbar 標示
  CRW 門檻（4=白化警戒、8=嚴重白化）。與「當日超標警報」互補——單日不
  超標但持續偏暖同樣致災，DHW 捕捉的正是這種累積效應。

兩者皆在時間序列資料集下拉選單中選取，滑桿播放、GIF 匯出、剖面、區域
匯出全部沿用。

**內部修正**：`load_day` 改為依資料集宣告的變數名讀取（DHW 這類多變數
檔含 `CRW_BAA`、`CRW_DHW` 等多欄，舊版的猜測邏輯可能選錯欄位）。
