"""
新功能自動測試（時間序列/距平/測站警報）— 不需網路，使用合成資料。
執行：python test_features.py
"""
from __future__ import annotations

import datetime
import pathlib
import sys
import tempfile

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "webapp"))
import timeseries as ts

PASS = 0


def check(name, cond):
    global PASS
    assert cond, f"FAIL: {name}"
    PASS += 1
    print(f"  ✔ {name}")


def synthetic_stack(n=35, ny=20, nx=24, warm_last=True):
    lat = np.linspace(17, 56, ny)
    lon = np.linspace(114, 162, nx)
    data = np.full((n, ny, nx), 20.0, dtype=np.float32)
    for i in range(n):
        data[i] += 0.01 * i                     # slow warming trend
    data[:, :3, :] = np.nan                     # "land"
    if warm_last:
        data[-1] += 3.0                         # heatwave on last day
    dates = [(datetime.date(2026, 6, 1) + datetime.timedelta(days=k)).isoformat()
             for k in range(n)]
    return ts.SeriesStack(dates=dates, lat=lat, lon=lon, data=data)


def main():
    print("── 距平 (anomaly) ──")
    stk = synthetic_stack()
    a = stk.anomaly(stk.nframes - 1, baseline=30)
    v = a[np.isfinite(a)]
    check("最後一日距平 ≈ +3°C（熱浪偵測）", abs(float(v.mean()) - 3.0) < 0.3)
    a0 = stk.anomaly(5, baseline=30)
    v0 = a0[np.isfinite(a0)]
    check("正常日距平 ≈ 0", abs(float(v0.mean())) < 0.2)
    check("陸地維持 NaN", np.isnan(a[0, 0]))

    print("── frame_payload ──")
    pl = ts.frame_payload(stk, 0)
    check("payload 含日期/格點", pl["date"] == "2026-06-01"
          and len(pl["lat"]) == 20 and len(pl["values"]) == 20)
    check("陸地輸出 None", pl["values"][0][0] is None)
    pa = ts.frame_payload(stk, stk.nframes - 1, anomaly=True, baseline=30)
    check("距平 payload stats 為正", pa["stats"]["mean"] > 2.5)

    print("── date_range 防呆 ──")
    check("正常區間", len(ts.date_range("2026-06-01", "2026-06-10")) == 10)
    check("反向自動修正", len(ts.date_range("2026-06-10", "2026-06-01")) == 10)
    try:
        ts.date_range("2026-01-01", "2026-12-31")
        check("超長區間應拒絕", False)
    except ValueError:
        check("超長區間應拒絕", True)

    print("── build_stack（mock 下載）──")
    tmp = pathlib.Path(tempfile.mkdtemp())

    def fake_fetch(date, stride, log):
        import netCDF4 as nc
        p = tmp / f"f{date}.nc"
        ds = nc.Dataset(str(p), "w")
        ds.createDimension("time", 1)
        ds.createDimension("latitude", 8)
        ds.createDimension("longitude", 10)
        la = ds.createVariable("latitude", "f8", ("latitude",))
        lo = ds.createVariable("longitude", "f8", ("longitude",))
        v = ds.createVariable("analysed_sst", "f4", ("time", "latitude", "longitude"))
        la[:] = np.linspace(17, 56, 8)
        lo[:] = np.linspace(114, 162, 10)
        v[0] = 298.15  # kelvin → should become 25°C
        ds.close()
        return p

    stk2 = ts.build_stack("2026-06-01", "2026-06-03", 8, print, fetch=fake_fetch)
    check("3 天堆疊", stk2.nframes == 3)
    check("Kelvin 自動轉 °C", abs(float(stk2.data[0][0, 0]) - 25.0) < 0.01)

    print("── GIF 匯出 ──")
    gif = tmp / "test.gif"
    ts.export_gif(synthetic_stack(n=4), gif, fps=2, log=lambda m: None)
    check("GIF 檔案產生", gif.exists() and gif.stat().st_size > 5000)
    gif2 = tmp / "test_anom.gif"
    ts.export_gif(synthetic_stack(n=4), gif2, anomaly=True, baseline=3, log=lambda m: None)
    check("距平 GIF 產生", gif2.exists() and gif2.stat().st_size > 5000)

    print("── 測站警報 ──")
    st = {"name": "測試站", "lat": 25.0, "lon": 122.0, "t_high": 30.0, "t_low": 15.0}
    series = {"dates": ["2026-07-01", "2026-07-02"], "values": [28.0, 31.2]}
    r = ts.evaluate_alerts(st, series)
    check("高溫警報觸發", len(r["alerts"]) == 1 and "高溫" in r["alerts"][0])
    series["values"] = [16.0, 14.5]
    r = ts.evaluate_alerts(st, series)
    check("低溫警報觸發", len(r["alerts"]) == 1 and "低溫" in r["alerts"][0])
    series["values"] = [20.0, None]           # 最新缺值 → 用前一日
    r = ts.evaluate_alerts(st, series)
    check("缺值回退前一日且無警報", r["latest_value"] == 20.0 and not r["alerts"])

    print("── 測站範圍驗證 ──")
    try:
        ts.add_station.__wrapped__ if False else None
        bad = False
        try:
            # do not actually persist: validate only via exception (lat out of AOI)
            ts.add_station("外海", 5.0, 122.0)
        except ValueError:
            bad = True
        check("超出範圍點位應拒絕", bad)
    finally:
        pass

    print(f"\n全部通過：{PASS} 項 ✅")




def test_v2():
    """新功能第二批：剖面 / cohesion / GeoJSON / 匯出"""
    import sst_processor as spp

    print("── 剖面工具 ──")
    ny, nx = 40, 60
    lat = np.linspace(20, 30, ny)
    lon = np.linspace(115, 130, nx)
    # 合成鋒面：125°E 以東 +4°C，過渡帶 1°
    grid = np.full((ny, nx), 22.0, dtype=np.float32)
    grid += 4.0 / (1.0 + np.exp(-(lon[None, :] - 125.0) * 6.0))
    r = ts.transect(lat, lon, grid, 25.0, 118.0, 25.0, 129.0, n=400)
    check("剖面長度合理（約 1100 km）", 1000 < r["total_km"] < 1200)
    check("剖面起點 ≈ 22°C", abs(r["values"][0] - 22.0) < 0.1)
    check("剖面終點 ≈ 26°C", abs(r["values"][-1] - 26.0) < 0.1)
    check("最大梯度位於 125°E 附近", abs(r["max_grad_at"]["lon"] - 125.0) < 0.6)
    check("鋒面強度顯著（>0.02 °C/km）", r["max_grad"] > 0.02)

    print("── Cayula-Cornillon cohesion ──")
    rng = np.random.default_rng(7)
    # (a) 連貫雙峰：左右兩水團 → 應偵測到前緣
    coh = np.full((96, 96), 18.0, dtype=float)
    coh[:, 48:] = 24.0
    coh += rng.normal(0, 0.25, coh.shape)
    m1 = spp.cayula_cornillon(coh, window=32, overlap=0.5, threshold=1.5)
    m1 = spp.remove_small_fronts(m1, min_pixels=12)   # 完整管線含小物件過濾
    check("連貫鋒面被偵測", m1.sum() > 20)
    colsum = m1.sum(axis=0)
    wmean = (np.arange(96) * colsum).sum() / colsum.sum()
    check("前緣位於邊界附近（col 48±3，像素加權）", abs(wmean - 48) < 3)
    # (b) 不連貫雙峰（鹽椒噪音）→ cohesion 應拒絕
    noise = np.where(rng.random((96, 96)) > 0.5, 18.0, 24.0)
    noise += rng.normal(0, 0.25, noise.shape)
    m2 = spp.cayula_cornillon(noise, window=32, overlap=0.5, threshold=1.5)
    check("鹽椒噪音被 cohesion 拒絕", m2.sum() < m1.sum() * 0.2)

    print("── 小物件過濾與 GeoJSON ──")
    mk = np.zeros((50, 50), dtype=bool)
    mk[10:40, 25] = True        # 30 px 線
    mk[5, 5] = True             # 1 px 雜訊
    mk2 = spp.remove_small_fronts(mk, min_pixels=12)
    check("保留 30px 前緣、移除 1px 雜訊",
          mk2[10:40, 25].all() and not mk2[5, 5])
    la2 = np.linspace(20, 25, 50); lo2 = np.linspace(120, 125, 50)
    gj = spp.fronts_to_geojson(mk2, la2, lo2)
    check("GeoJSON 有 LineString",
          gj["type"] == "FeatureCollection" and len(gj["features"]) >= 1
          and gj["features"][0]["geometry"]["type"] == "LineString")

    print("── 區域匯出 ──")
    import tempfile
    tmp = pathlib.Path(tempfile.mkdtemp())
    cla, clo, cg = ts._crop(lat, lon, grid, 22, 28, 118, 128)
    check("裁切維度正確", cg.shape == (cla.size, clo.size) and cla.size > 5)
    out_csv, stride = ts.export_csv(cla, clo, cg, tmp / "t.csv")
    head = (tmp / "t.csv").read_text().splitlines()
    check("CSV 匯出（含表頭與資料）",
          head[0] == "lon,lat,sst_c" and len(head) > 100)
    out_nc = ts.export_netcdf(cla, clo, cg, tmp / "t.nc")
    import netCDF4 as ncmod
    d = ncmod.Dataset(str(out_nc)); v = d.variables["sst"][:]; d.close()
    check("NetCDF 匯出往返一致",
          abs(float(np.ma.filled(v, np.nan)[0, 0]) - float(cg[0, 0])) < 1e-3)

    print("── 資料集註冊表 ──")
    check("八個資料集（含官方距平與 DHW）",
          set(ts.DATASETS) == {"mur", "oisst", "blended", "chl", "ssh",
                               "currents", "muranom", "dhw"})
    print("── SSH / 海流 ──")
    d = ts.current_direction(np.array([1.0, 0.0]), np.array([0.0, -1.0]))
    check("流向換算（東=90°、南=180°）",
          abs(d[0] - 90) < 0.01 and abs(d[1] - 180) < 0.01)
    cu = ts._day_url("https://coastwatch.noaa.gov/erddap/griddap",
                     ts.DATASETS["currents"], "2026-07-07", 1)
    check("海流 URL 含雙變數", "u_current[" in cu and ",v_current[" in cu)
    u = ts._day_url("https://host/erddap/griddap", ts.DATASETS["oisst"], "2026-07-01", 1)
    check("OISST URL 正確",
          "ncdcOisst21Agg_LonPM180" in u and "sst[" in u and "T00:00:00Z" in u)


if __name__ == "__main__":
    main()
    print()
    test_v2()
    print(f"\n=== 總計通過 {PASS} 項 ✅ ===")
