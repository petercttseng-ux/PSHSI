# 鰹鮪 ECDF-HSI 漁場預測系統 — 雲端部署映像
# 建置（context 必須是專案根目錄）：
#   docker build -f webapp/Dockerfile -t fri-hsi .
# 執行：
#   docker run -p 8765:8765 -e NASA_EARTHDATA_TOKEN=xxxx fri-hsi
FROM python:3.11-slim

# cartopy / netCDF4 需要的系統函式庫
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgeos-dev libproj-dev proj-data proj-bin build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/webapp

COPY webapp/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# 程式碼與訓練資料（habitat.py 於專案根目錄尋找參數與 CSV）
COPY webapp/ /app/webapp/
COPY habitat_params.json /app/
COPY skipjack-chla-ssha-sst-1998to2007.csv /app/
COPY yellowfin-chla-ssha-sst-1998to2007.csv /app/

ENV PORT=8765
EXPOSE 8765

# 衛星下載與 HSI 運算耗時，timeout 放寬；單 worker 保留記憶體內狀態
CMD gunicorn app:app --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 600
