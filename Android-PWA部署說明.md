# Android App（PWA）與雲端部署說明

本次更新已將漁場預測系統改造為 **PWA（漸進式網頁應用）**：手機瀏覽器開啟網址後，可「安裝到主畫面」，之後以全螢幕、獨立圖示的 App 形式運作。

## 本次變更檔案

| 檔案 | 說明 |
|---|---|
| `webapp/static/manifest.webmanifest` | App 名稱、圖示、主題色、獨立視窗模式 |
| `webapp/static/sw.js` | Service worker：殼層快取、離線回退；`/api/` 一律走網路 |
| `webapp/static/icons/` | 由所徽產生的 192/512/maskable/Apple 圖示 |
| `webapp/templates/index.html` | PWA meta 標籤 + service worker 註冊 |
| `webapp/app.py` | 新增 `/sw.js`、`/manifest.webmanifest` 路由 |
| `webapp/Dockerfile` | 雲端部署容器（gunicorn） |

## Android 手機安裝步驟

1. 手機 Chrome 開啟系統網址（**必須是 HTTPS**，見下節）。
2. Chrome 會自動跳出「安裝應用程式」提示；或點右上「⋮」→「加入主畫面 / 安裝應用程式」。
3. 主畫面出現「漁場預測」圖示，點開即全螢幕 App 體驗。

> iPhone 亦支援：Safari →「分享」→「加入主畫面」。

## 雲端部署（後端）

PWA 安裝**必須透過 HTTPS**（`localhost` 測試除外）。建議流程：

### 方式一：Docker（任何雲主機）

```bash
# 於專案根目錄
docker build -f webapp/Dockerfile -t fri-hsi .
docker run -d -p 8765:8765 \
  -e NASA_EARTHDATA_TOKEN=你的token \
  --name fri-hsi fri-hsi
```

再以 Nginx / Caddy 反向代理加上 HTTPS（Caddy 最簡單，自動申請憑證）：

```
# Caddyfile
hsi.example.gov.tw {
    reverse_proxy localhost:8765
}
```

### 方式二：直接以 gunicorn 執行（Linux 主機）

```bash
cd webapp
pip install -r requirements.txt gunicorn
NASA_EARTHDATA_TOKEN=你的token \
gunicorn app:app --bind 0.0.0.0:8765 --workers 1 --threads 8 --timeout 600
```

### 注意事項

- **workers 必須為 1**：系統狀態（載入的 SST 場、下載進度）存於記憶體，多 worker 會導致狀態不同步；並行由 `--threads` 提供。
- **憑證**：NASA token 以環境變數 `NASA_EARTHDATA_TOKEN` 注入；Copernicus 帳密於網頁登入後存於容器內，容器重建須重新登入（可掛 volume 保存 `~/.copernicusmarine`）。
- **記憶體**：全球 MUR 檔案處理建議主機 ≥ 8 GB RAM。
- **對外服務前建議加上存取控制**（如 Caddy basic_auth 或機關 VPN），因系統可觸發大量衛星資料下載。

## 本機測試（不需 HTTPS）

```bash
cd webapp && python app.py
# 瀏覽器開 http://127.0.0.1:8765
# DevTools → Application → Manifest / Service Workers 可驗證 PWA 狀態
```

手機同區網測試：`python app.py --host 0.0.0.0`，手機連 `http://PC的IP:8765`（此情況瀏覽器不會出現安裝提示，僅能一般瀏覽；正式安裝需 HTTPS）。
