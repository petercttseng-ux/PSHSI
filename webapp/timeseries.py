"""
Time-series / anomaly / station-monitoring backend.

Data source: NOAA CoastWatch ERDDAP griddap dataset `jplMURSST41`
(identical MUR v4.1 analysis, no Earthdata auth required, supports
server-side subsetting so a West-Pacific day at stride 8 is only a
few MB instead of a 700 MB global granule).

Marine Environmental Research, Fisheries Research Institute, MOA
"""
from __future__ import annotations

import datetime
import json
import pathlib
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

try:
    import netCDF4 as nc
    HAS_NETCDF4 = True
except ImportError:
    HAS_NETCDF4 = False

import sst_processor as sp

# ── Config ─────────────────────────────────────────────────────────────────
ERDDAP_HOSTS = [
    "https://coastwatch.pfeg.noaa.gov/erddap/griddap",
    "https://upwell.pfeg.noaa.gov/erddap/griddap",
]

# ── Dataset registry ───────────────────────────────────────────────────────
# kind: "sst" (°C, ocean thermal colormap) | "chl" (mg/m³, log scale)
DATASETS = {
    "mur": {
        "id": "jplMURSST41", "var": "analysed_sst", "kind": "sst",
        "name": "MUR SST v4.1（1km）", "default_stride": 8,
    },
    "oisst": {
        "id": "ncdcOisst21Agg_LonPM180", "var": "sst", "kind": "sst",
        "name": "NOAA OISST v2.1（25km，1981–今）", "default_stride": 1,
    },
    "blended": {
        "id": "nesdisBLENDEDsstDNDaily", "var": "analysed_sst", "kind": "sst",
        "name": "NOAA Geo-Polar Blended（5km）", "default_stride": 2,
    },
    "chl": {
        "id": "erdMH1chla1day_R2022SQ", "var": "chlor_a",
        "kind": "chl", "name": "MODIS Aqua 葉綠素-a 海洋水色（4km）",
        "default_stride": 6,   # 4km × 6 ≈ 0.25°
        "fallback_days": 60,   # MODIS NRT 延遲較大，放寬回退天數
    },
    "ssh": {
        "id": "noaacwBLENDEDsshDaily", "var": "sla", "kind": "ssh",
        "name": "SSH 海面高度距平 SLA（測高，25km）", "default_stride": 1,
        "hosts": ["https://coastwatch.noaa.gov/erddap/griddap"],
    },
    "currents": {
        "id": "noaacwBLENDEDNRTcurrentsDaily", "var": "u_current,v_current",
        "kind": "speed", "name": "表面地轉流速（測高，25km）", "default_stride": 1,
        "hosts": ["https://coastwatch.noaa.gov/erddap/griddap"],
    },
    "muranom": {
        "id": "jplMURSST41anom1day", "var": "sstAnom", "kind": "anom",
        "name": "MUR SST 距平（官方氣候基準，1km）", "default_stride": 8,
    },
    "dhw": {
        "id": "NOAA_DHW", "var": "CRW_DHW", "kind": "dhw",
        "name": "累積熱壓力 DHW（珊瑚熱壓力，5km）", "default_stride": 1,
    },
}

ERDDAP_BASES = [f"{h}/jplMURSST41" for h in ERDDAP_HOSTS]  # 向後相容（測站點位查詢）
LON_MIN, LON_MAX = sp.LON_MIN, sp.LON_MAX
LAT_MIN, LAT_MAX = sp.LAT_MIN, sp.LAT_MAX

TS_DIR = sp.DATA_DIR / "timeseries"
TS_DIR.mkdir(exist_ok=True)
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIONS_FILE = PROJECT_ROOT / "stations.json"
_stations_lock = threading.Lock()

MAX_DAYS = 92  # safety cap for one animation request


# ── ERDDAP fetching ────────────────────────────────────────────────────────
def _session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": "GHRSST-MUR-Viewer/2.0 (FRI MOA)"})
    return s


def _day_url(host: str, ds: dict, date: str, stride: int,
             lon0: float, lon1: float) -> str:
    # 以全日範圍取值，不假設各資料集的每日時間戳（MUR 09Z、OISST 12Z…）
    subset = (
        f"[({date}T00:00:00Z):1:({date}T23:59:59Z)]"
        f"[({LAT_MIN}):{stride}:({LAT_MAX})]"
        f"[({lon0}):{stride}:({lon1})]"
    )
    query = ",".join(v + subset for v in ds["var"].split(","))
    return f"{host}/{ds['id']}.nc?{query}"


def _lon_windows() -> list:
    """
    Return the ERDDAP longitude request window(s) covering the AOI, in the
    −180…180 convention that CoastWatch griddap datasets use.  When the AOI
    crosses the dateline (LON_MAX > 180) the request is split into an east
    window (LON_MIN…180) and a west window (−180…LON_MAX−360); the two
    subsets are stitched back together in 0–360 order after download.
    """
    if LON_MAX <= 180:
        return [(LON_MIN, LON_MAX)]
    return [(LON_MIN, 180.0), (-180.0, LON_MAX - 360.0)]


def _download_one(host: str, ds: dict, date: str, stride: int,
                  lon0: float, lon1: float, dest: pathlib.Path,
                  s) -> Optional[str]:
    """Download a single lon-window subset to `dest`. Returns error str or None."""
    try:
        r = s.get(_day_url(host, ds, date, stride, lon0, lon1),
                  timeout=180, stream=True)
        if r.status_code == 404:
            return "404 (該日資料尚未發布)"
        r.raise_for_status()
        tmp = dest.with_suffix(".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
        tmp.rename(dest)
        return None
    except Exception as e:
        return str(e)


def _merge_lon_parts(parts: list, dest: pathlib.Path) -> None:
    """
    Stitch several single-day ERDDAP subset files (each covering a different
    longitude window) into one cached file whose longitude axis is expressed
    in ascending 0–360.  All data variables are reduced to 2-D (lat, lon).
    """
    if not HAS_NETCDF4:
        raise RuntimeError("缺少 netCDF4 套件")
    lat = None
    lon_chunks, var_chunks = [], {}
    var_names = []
    for p in parts:
        d = nc.Dataset(str(p))
        try:
            latv = d.variables.get("latitude") or d.variables.get("lat")
            lonv = d.variables.get("longitude") or d.variables.get("lon")
            la = np.array(latv[:], dtype=np.float64)
            lo = np.array(lonv[:], dtype=np.float64)
            lo = np.where(lo < 0, lo + 360.0, lo)          # → 0–360
            if lat is None:
                lat = la
                var_names = [n for n, v in d.variables.items()
                             if v.ndim >= 2 and not n.endswith("_mask")
                             and n not in ("latitude", "longitude", "lat", "lon", "time")]
            lon_chunks.append(lo)
            for n in var_names:
                arr = np.squeeze(np.array(d.variables[n][...], dtype=np.float32))
                while arr.ndim > 2:
                    arr = arr[0]
                var_chunks.setdefault(n, []).append(arr)
        finally:
            d.close()
    lon_all = np.concatenate(lon_chunks)
    order = np.argsort(lon_all, kind="stable")
    lon_sorted = lon_all[order]
    out = nc.Dataset(str(dest), "w", format="NETCDF4")
    try:
        out.createDimension("lat", lat.size)
        out.createDimension("lon", lon_sorted.size)
        vla = out.createVariable("latitude", "f8", ("lat",)); vla[:] = lat
        vlo = out.createVariable("longitude", "f8", ("lon",)); vlo[:] = lon_sorted
        for n in var_names:
            merged = np.concatenate(var_chunks[n], axis=1)[:, order]
            vv = out.createVariable(n, "f4", ("lat", "lon"),
                                    zlib=True, fill_value=np.float32(np.nan))
            vv[:] = merged.astype(np.float32)
    finally:
        out.close()


def fetch_day(date: str, stride: int, log: Callable[[str], None],
              dataset: str = "mur") -> Optional[pathlib.Path]:
    """Download one day's tropical-Pacific subset (cached). Returns path or None.

    Handles the 130°E–150°W AOI that crosses the 180° dateline by issuing an
    east and a west request and stitching them into a single 0–360 file.
    """
    ds = DATASETS[dataset]
    dest = TS_DIR / f"{dataset}_s{stride}_{date.replace('-', '')}.nc"
    if dest.exists() and dest.stat().st_size > 1000:
        return dest
    windows = _lon_windows()
    last_err = None
    for host in ds.get("hosts", ERDDAP_HOSTS):
        s = _session()
        part_paths, ok = [], True
        for k, (lo0, lo1) in enumerate(windows):
            part = (dest if len(windows) == 1
                    else dest.with_suffix(f".part{k}.nc"))
            err = _download_one(host, ds, date, stride, lo0, lo1, part, s)
            if err:
                last_err = err
                ok = False
                break
            part_paths.append(part)
        if not ok:
            for p in part_paths:
                try: p.unlink()
                except OSError: pass
            continue
        if len(windows) > 1:
            try:
                _merge_lon_parts(part_paths, dest)
            except Exception as e:
                last_err = f"合併經度分段失敗：{e}"
                continue
            finally:
                for p in part_paths:
                    try: p.unlink()
                    except OSError: pass
        return dest
    log(f"  ⚠️ {date} 下載失敗：{last_err}")
    return None


def load_day(path: pathlib.Path, var: Optional[str] = None):
    """
    Return (lat, lon, arr2d[NaN-masked]) from an ERDDAP subset file.
    `var` (from the dataset registry) selects the variable explicitly;
    without it the loader falls back to a candidate-name guess. Passing
    `var` is required for multi-variable files (e.g. DHW) to avoid
    picking the wrong column.
    """
    if not HAS_NETCDF4:
        raise RuntimeError("缺少 netCDF4 套件")
    ds = nc.Dataset(str(path))
    try:
        latv = ds.variables.get("latitude") or ds.variables.get("lat")
        lonv = ds.variables.get("longitude") or ds.variables.get("lon")
        lat = np.array(latv[:], dtype=np.float64)
        lon = np.array(lonv[:], dtype=np.float64)
        if "u_current" in ds.variables and "v_current" in ds.variables:
            u = np.squeeze(ds.variables["u_current"][...])
            w = np.squeeze(ds.variables["v_current"][...])
            while u.ndim > 2:   # 全日範圍可能含相鄰日兩個時間點 → 取請求日
                u = u[0]
            while w.ndim > 2:
                w = w[0]
            u = np.ma.filled(np.ma.masked_invalid(u), np.nan).astype(np.float32)
            w = np.ma.filled(np.ma.masked_invalid(w), np.nan).astype(np.float32)
            arr = np.hypot(u, w)
            latv = ds.variables.get("latitude") or ds.variables.get("lat")
            lonv = ds.variables.get("longitude") or ds.variables.get("lon")
            lat = np.array(latv[:], dtype=np.float64)
            lon = np.array(lonv[:], dtype=np.float64)
            if lat[0] > lat[-1]:
                lat = lat[::-1]; arr = arr[::-1, :]
            return lat, lon, arr
        v = None
        # 1) explicit variable from the dataset registry (most reliable)
        if var and "," not in var and var in ds.variables:
            v = ds.variables[var]
        # 2) known single-variable names
        if v is None:
            for cand in ("analysed_sst", "sst", "chlor_a", "sla", "sstAnom", "CRW_DHW"):
                if cand in ds.variables:
                    v = ds.variables[cand]
                    break
        if v is None:  # 3) fallback: first ≥2-D non-coordinate, non-mask variable
            for name, vv in ds.variables.items():
                if (vv.ndim >= 2 and not name.endswith("_mask")
                        and name not in ("latitude", "longitude", "lat", "lon", "time")):
                    v = vv
                    break
        arr = np.squeeze(v[...])          # (time, [zlev/alt,] lat, lon) → (lat, lon)
        while arr.ndim > 2:               # 全日範圍可能含兩個時間點 → 取第一（請求日）
            arr = arr[0]
        if arr.ndim != 2:
            raise RuntimeError(f"非預期維度：{arr.shape}")
        arr = np.ma.filled(np.ma.masked_invalid(arr), np.nan).astype(np.float32)
    finally:
        ds.close()
    valid = arr[np.isfinite(arr)]
    if valid.size and valid.mean() > 200:  # kelvin safety net
        arr = arr - 273.15
    if lat[0] > lat[-1]:  # ensure ascending latitude
        lat = lat[::-1]
        arr = arr[::-1, :]
    return lat, lon, arr


# ── Stack ──────────────────────────────────────────────────────────────────
@dataclass
class SeriesStack:
    dates: list          # ["YYYY-MM-DD", ...]
    lat: np.ndarray
    lon: np.ndarray
    data: np.ndarray     # (T, Y, X) float32, NaN = land/missing
    dataset: str = "mur"

    @property
    def nframes(self) -> int:
        return len(self.dates)

    def frame_stats(self, i: int) -> dict:
        a = self.data[i]
        v = a[np.isfinite(a)]
        if v.size == 0:
            return {"min": None, "max": None, "mean": None}
        return {"min": round(float(v.min()), 2),
                "max": round(float(v.max()), 2),
                "mean": round(float(v.mean()), 2)}

    def anomaly(self, i: int, baseline: int = 30) -> np.ndarray:
        """frame i minus mean of up-to-`baseline` preceding frames
        (or all other frames when i has no history)."""
        if self.nframes < 2:
            raise ValueError("至少需要 2 天資料才能計算距平")
        import warnings
        j0 = max(0, i - baseline)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            if i == 0:
                base = np.nanmean(self.data[1:], axis=0)
            else:
                base = np.nanmean(self.data[j0:i], axis=0)
        return self.data[i] - base


def date_range(start: str, end: str) -> list:
    d0 = datetime.date.fromisoformat(start)
    d1 = datetime.date.fromisoformat(end)
    if d1 < d0:
        d0, d1 = d1, d0
    n = (d1 - d0).days + 1
    if n > MAX_DAYS:
        raise ValueError(f"日期區間過長（{n} 天），上限 {MAX_DAYS} 天")
    return [(d0 + datetime.timedelta(days=k)).isoformat() for k in range(n)]


def build_stack(start: str, end: str, stride: int,
                log: Callable[[str], None],
                progress_cb: Optional[Callable[[float, str], None]] = None,
                fetch=None, dataset: str = "mur") -> SeriesStack:
    if fetch is None:
        def fetch(d, st, lg):
            return fetch_day(d, st, lg, dataset=dataset)
    dates = date_range(start, end)
    frames, kept, lat = [], [], None
    lon = None
    for k, d in enumerate(dates):
        if progress_cb:
            progress_cb(k / len(dates) * 100, f"下載 {d}")
        p = fetch(d, stride, log)
        if p is None:
            continue
        try:
            la, lo, arr = load_day(p, var=DATASETS.get(dataset, {}).get("var"))
        except Exception as e:
            log(f"  ⚠️ {d} 讀取失敗：{e}")
            continue
        if lat is None:
            lat, lon = la, lo
        elif arr.shape != (lat.size, lon.size):
            log(f"  ⚠️ {d} 網格不一致，略過")
            continue
        frames.append(arr)
        kept.append(d)
    if not frames:
        raise RuntimeError("區間內無任何可用資料")
    if progress_cb:
        progress_cb(100.0, "完成")
    log(f"✅ 時間序列就緒：{kept[0]} ~ {kept[-1]}（{len(kept)} 天，網格 {frames[0].shape}）")
    return SeriesStack(dates=kept, lat=lat, lon=lon,
                       data=np.stack(frames).astype(np.float32), dataset=dataset)


def frame_payload(stack: SeriesStack, i: int, anomaly: bool = False,
                  baseline: int = 30) -> dict:
    arr = stack.anomaly(i, baseline) if anomaly else stack.data[i]
    values = [
        [None if not np.isfinite(v) else round(float(v), 2) for v in row]
        for row in arr
    ]
    v = arr[np.isfinite(arr)]
    stats = ({"min": round(float(v.min()), 2), "max": round(float(v.max()), 2),
              "mean": round(float(v.mean()), 2)} if v.size else
             {"min": None, "max": None, "mean": None})
    return {
        "lon": [round(float(x), 4) for x in stack.lon],
        "lat": [round(float(y), 4) for y in stack.lat],
        "values": values,
        "date": stack.dates[i],
        "index": i,
        "nframes": stack.nframes,
        "anomaly": anomaly,
        "dataset": stack.dataset,
        "kind": DATASETS.get(stack.dataset, {}).get("kind", "sst"),
        "dataset_name": DATASETS.get(stack.dataset, {}).get("name", stack.dataset),
        "stats": stats,
    }


# ── GIF export ─────────────────────────────────────────────────────────────
OCEAN_COLORS = [
    (0.00, "#030085"), (0.10, "#0028c8"), (0.22, "#0066ff"), (0.32, "#00b4e6"),
    (0.42, "#00e6b4"), (0.52, "#80ff80"), (0.62, "#ffff00"), (0.72, "#ffaa00"),
    (0.82, "#ff4400"), (0.92, "#cc0000"), (1.00, "#660000"),
]


def export_gif(stack: SeriesStack, out_path: pathlib.Path,
               anomaly: bool = False, baseline: int = 30,
               fps: int = 4, log: Callable[[str], None] = print) -> pathlib.Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = [
        "Microsoft JhengHei", "Noto Sans TC", "PingFang TC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    from matplotlib.colors import LinearSegmentedColormap
    from PIL import Image
    import io as _io

    kind = DATASETS.get(stack.dataset, {}).get("kind", "sst")
    if anomaly:
        cmap, label = "RdBu_r", "anomaly"
        amax = max(0.1, float(np.nanpercentile(
            np.abs([stack.anomaly(i, baseline) for i in range(stack.nframes)]), 98)))
        vmin, vmax = -amax, amax
    elif kind == "ssh":
        cmap, label = "RdBu_r", "SLA (m)"
        amax = max(0.1, float(np.nanpercentile(np.abs(stack.data), 98)))
        vmin, vmax = -amax, amax
    elif kind == "speed":
        cmap, label = "plasma", "Speed (m/s)"
        vmin, vmax = 0.0, max(0.5, float(np.nanpercentile(stack.data, 99)))
    elif kind == "chl":
        cmap, label = "viridis", "Chl (mg/m³)"
        vmin = 0.05
        vmax = max(1.0, float(np.nanpercentile(stack.data, 99)))
    elif kind == "anom":
        cmap, label = "RdBu_r", "SST anomaly (°C)"
        amax = max(0.5, float(np.nanpercentile(np.abs(stack.data), 98)))
        vmin, vmax = -amax, amax
    elif kind == "dhw":
        # Coral Reef Watch 熱壓力配色：0→白，愈高愈紅紫
        cmap = LinearSegmentedColormap.from_list(
            "crw_dhw",
            ["#ffffff", "#fce94f", "#f57900", "#cc0000", "#5c0000", "#2e0854"],
            N=512)
        label = "DHW (°C-weeks)"
        vmin, vmax = 0.0, max(8.0, float(np.nanpercentile(stack.data, 99)))
    else:
        cmap = LinearSegmentedColormap.from_list("ocean_thermal", OCEAN_COLORS, N=512)
        label = "SST (°C)"
        vmin = float(np.nanpercentile(stack.data, 1))
        vmax = float(np.nanpercentile(stack.data, 99))

    images = []
    for i in range(stack.nframes):
        arr = stack.anomaly(i, baseline) if anomaly else stack.data[i]
        fig, ax = plt.subplots(figsize=(7.2, 6.0), dpi=110)
        im = ax.imshow(arr, origin="lower", cmap=cmap, vmin=vmin, vmax=vmax,
                       extent=[stack.lon[0], stack.lon[-1], stack.lat[0], stack.lat[-1]],
                       aspect="auto", interpolation="nearest")
        ax.set_title(f"GHRSST MUR {'距平' if anomaly else 'SST'}  {stack.dates[i]}",
                     fontsize=11)
        ax.set_xlabel("Longitude (°E)")
        ax.set_ylabel("Latitude (°N)")
        fig.colorbar(im, ax=ax, label=label, shrink=0.85)
        fig.text(0.99, 0.01, "FRI MOA · NASA JPL MUR v4.1 via NOAA ERDDAP",
                 ha="right", fontsize=6, color="#666")
        buf = _io.BytesIO()
        fig.tight_layout()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0)
        images.append(Image.open(buf).convert("P", palette=Image.ADAPTIVE))
        if i % 5 == 0:
            log(f"  ↳ GIF frame {i + 1}/{stack.nframes}")

    images[0].save(out_path, save_all=True, append_images=images[1:],
                   duration=int(1000 / max(1, fps)), loop=0, optimize=True)
    log(f"💾 GIF 匯出完成：{out_path.name}")
    return out_path


# ── Stations ───────────────────────────────────────────────────────────────
def load_stations() -> list:
    with _stations_lock:
        if not STATIONS_FILE.exists():
            return []
        try:
            return json.loads(STATIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []


def save_stations(stations: list) -> None:
    with _stations_lock:
        STATIONS_FILE.write_text(
            json.dumps(stations, ensure_ascii=False, indent=2), encoding="utf-8")


def add_station(name: str, lat: float, lon: float,
                t_high: Optional[float] = None,
                t_low: Optional[float] = None) -> dict:
    lon = lon + 360.0 if lon < 0 else lon   # accept −180…180 input too
    if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
        raise ValueError("點位超出系統範圍（130°E–150°W, 20°S–20°N）")
    st = {
        "id": uuid.uuid4().hex[:8],
        "name": (name or "").strip()[:40] or f"站點 {lat:.2f}N {lon:.2f}E",
        "lat": round(float(lat), 4),
        "lon": round(float(lon), 4),
        "t_high": None if t_high is None else float(t_high),
        "t_low": None if t_low is None else float(t_low),
        "created": datetime.date.today().isoformat(),
    }
    stations = load_stations()
    stations.append(st)
    save_stations(stations)
    return st


def remove_station(sid: str) -> bool:
    stations = load_stations()
    kept = [s for s in stations if s["id"] != sid]
    if len(kept) == len(stations):
        return False
    save_stations(kept)
    return True


def fetch_point_series(lat: float, lon: float, days: int = 60,
                       session=None) -> dict:
    """Fetch a single-point SST time series from ERDDAP → {dates, values}."""
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days + 2)
    q = (
        ".json?analysed_sst"
        f"[({start.isoformat()}T09:00:00Z):1:({end.isoformat()}T09:00:00Z)]"
        f"[({lat}):1:({lat})][({lon}):1:({lon})]"
    )
    last_err = None
    s = session or _session()
    for base in ERDDAP_BASES:
        try:
            r = s.get(base + q, timeout=90)
            r.raise_for_status()
            rows = r.json()["table"]["rows"]
            dates, values = [], []
            for row in rows:
                t, _la, _lo, v = row
                dates.append(t[:10])
                if v is None:
                    values.append(None)
                else:
                    v = float(v)
                    values.append(round(v - 273.15, 2) if v > 200 else round(v, 2))
            return {"dates": dates, "values": values}
        except Exception as e:
            last_err = str(e)
            continue
    raise RuntimeError(f"ERDDAP 查詢失敗：{last_err}")


def evaluate_alerts(station: dict, series: dict) -> dict:
    """Latest valid value vs thresholds → alert dict."""
    latest_date, latest_val = None, None
    for d, v in zip(reversed(series["dates"]), reversed(series["values"])):
        if v is not None:
            latest_date, latest_val = d, v
            break
    alerts = []
    if latest_val is not None:
        if station.get("t_high") is not None and latest_val >= station["t_high"]:
            alerts.append(f"高溫警報：{latest_val}°C ≥ {station['t_high']}°C")
        if station.get("t_low") is not None and latest_val <= station["t_low"]:
            alerts.append(f"低溫警報：{latest_val}°C ≤ {station['t_low']}°C")
    return {"latest_date": latest_date, "latest_value": latest_val, "alerts": alerts}


# ── Transect profile (剖面工具) ─────────────────────────────────────────────
EARTH_R = 6371.0088  # km


def _haversine(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_R * np.arcsin(np.sqrt(a))


def _bilinear(lat_arr, lon_arr, grid, lats, lons):
    """Bilinear sampling of a regular grid at (lats, lons); NaN-aware."""
    fi = np.interp(lats, lat_arr, np.arange(lat_arr.size))
    fj = np.interp(lons, lon_arr, np.arange(lon_arr.size))
    i0 = np.clip(np.floor(fi).astype(int), 0, lat_arr.size - 2)
    j0 = np.clip(np.floor(fj).astype(int), 0, lon_arr.size - 2)
    di = fi - i0
    dj = fj - j0
    q00 = grid[i0, j0]
    q01 = grid[i0, j0 + 1]
    q10 = grid[i0 + 1, j0]
    q11 = grid[i0 + 1, j0 + 1]
    val = (q00 * (1 - di) * (1 - dj) + q01 * (1 - di) * dj
           + q10 * di * (1 - dj) + q11 * di * dj)
    # 任一角為 NaN → 改用最近鄰（保留海陸邊界附近的值）
    bad = ~np.isfinite(val)
    if bad.any():
        ni = np.clip(np.round(fi).astype(int), 0, lat_arr.size - 1)
        nj = np.clip(np.round(fj).astype(int), 0, lon_arr.size - 1)
        val[bad] = grid[ni[bad], nj[bad]]
    return val


def transect(lat_arr: np.ndarray, lon_arr: np.ndarray, grid: np.ndarray,
             lat1: float, lon1: float, lat2: float, lon2: float,
             n: int = 300) -> dict:
    """
    Sample SST along the line (lat1,lon1)→(lat2,lon2) and compute the
    along-track gradient (°C/km). Front strength = max |gradient|.
    """
    n = max(10, min(2000, int(n)))
    lats = np.linspace(lat1, lat2, n)
    lons = np.linspace(lon1, lon2, n)
    vals = _bilinear(lat_arr, lon_arr, grid, lats, lons).astype(float)

    seg = _haversine(lats[:-1], lons[:-1], lats[1:], lons[1:])
    dist = np.concatenate([[0.0], np.cumsum(seg)])

    grad = np.full(n, np.nan)
    ok = np.isfinite(vals)
    if ok.sum() >= 3:
        v = np.where(ok, vals, np.nan)
        with np.errstate(invalid="ignore"):
            grad = np.gradient(v, dist)

    agrad = np.abs(grad)
    imax = int(np.nanargmax(agrad)) if np.isfinite(agrad).any() else 0
    return {
        "dist_km": [round(float(d), 2) for d in dist],
        "lats": [round(float(x), 4) for x in lats],
        "lons": [round(float(x), 4) for x in lons],
        "values": [None if not np.isfinite(v) else round(float(v), 3) for v in vals],
        "grad": [None if not np.isfinite(g) else round(float(g), 4) for g in grad],
        "total_km": round(float(dist[-1]), 1),
        "max_grad": None if not np.isfinite(agrad[imax]) else round(float(agrad[imax]), 4),
        "max_grad_at": {
            "km": round(float(dist[imax]), 1),
            "lat": round(float(lats[imax]), 4),
            "lon": round(float(lons[imax]), 4),
        },
    }


# ── Region export (區域匯出) ────────────────────────────────────────────────
EXPORT_DIR = sp.DATA_DIR / "exports"
EXPORT_DIR.mkdir(exist_ok=True)


def _crop(lat_arr, lon_arr, grid, lat0, lat1, lon0, lon1):
    if lat0 > lat1:
        lat0, lat1 = lat1, lat0
    if lon0 > lon1:
        lon0, lon1 = lon1, lon0
    ii = np.where((lat_arr >= lat0) & (lat_arr <= lat1))[0]
    jj = np.where((lon_arr >= lon0) & (lon_arr <= lon1))[0]
    if ii.size < 2 or jj.size < 2:
        raise ValueError("裁切範圍內無資料格點")
    return (lat_arr[ii[0]:ii[-1] + 1], lon_arr[jj[0]:jj[-1] + 1],
            grid[ii[0]:ii[-1] + 1, jj[0]:jj[-1] + 1])


def export_csv(lat_arr, lon_arr, grid, out_path, var="sst_c", max_rows=400_000):
    npts = grid.size
    stride = 1
    while npts / (stride * stride) > max_rows:
        stride += 1
    la, lo, g = lat_arr[::stride], lon_arr[::stride], grid[::stride, ::stride]
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write(f"lon,lat,{var}\n")
        for i, y in enumerate(la):
            row = g[i]
            fin = np.isfinite(row)
            for j in np.where(fin)[0]:
                f.write(f"{lo[j]:.4f},{y:.4f},{row[j]:.3f}\n")
    return out_path, stride


def export_netcdf(lat_arr, lon_arr, grid, out_path, var="sst",
                  units="degree_C", title="SST subset"):
    ds = nc.Dataset(str(out_path), "w", format="NETCDF4")
    try:
        ds.createDimension("lat", lat_arr.size)
        ds.createDimension("lon", lon_arr.size)
        vla = ds.createVariable("lat", "f8", ("lat",))
        vlo = ds.createVariable("lon", "f8", ("lon",))
        vv = ds.createVariable(var, "f4", ("lat", "lon"), zlib=True, fill_value=np.float32(np.nan))
        vla[:] = lat_arr; vla.units = "degrees_north"; vla.standard_name = "latitude"
        vlo[:] = lon_arr; vlo.units = "degrees_east";  vlo.standard_name = "longitude"
        vv[:] = grid.astype(np.float32); vv.units = units
        ds.title = title
        ds.institution = "Fisheries Research Institute, MOA (via NASA JPL / NOAA)"
        ds.Conventions = "CF-1.6"
    finally:
        ds.close()
    return out_path


def export_geotiff(lat_arr, lon_arr, grid, out_path):
    try:
        import rasterio
        from rasterio.transform import from_bounds
    except ImportError:
        raise RuntimeError("GeoTIFF 匯出需安裝 rasterio：pip install rasterio")
    # north-up：列由北到南
    if lat_arr[0] < lat_arr[-1]:
        lat_arr = lat_arr[::-1]
        grid = grid[::-1, :]
    transform = from_bounds(
        float(lon_arr[0]), float(lat_arr[-1]),
        float(lon_arr[-1]), float(lat_arr[0]),
        lon_arr.size, lat_arr.size,
    )
    with rasterio.open(
        str(out_path), "w", driver="GTiff",
        height=lat_arr.size, width=lon_arr.size, count=1,
        dtype="float32", crs="EPSG:4326", transform=transform,
        nodata=np.nan, compress="deflate",
    ) as dst:
        dst.write(grid.astype(np.float32), 1)
    return out_path


def fetch_day_fallback(date: str, stride: int, log: Callable[[str], None],
                       dataset: str, max_back: Optional[int] = None):
    """
    Fetch `date`; if that day isn't published yet, walk back day by day
    (up to max_back) and return the newest available.
    Returns (path, actual_date) or (None, None).
    """
    if max_back is None:
        max_back = DATASETS.get(dataset, {}).get("fallback_days", 7)
    d0 = datetime.date.fromisoformat(date)
    for k in range(max_back + 1):
        d = (d0 - datetime.timedelta(days=k)).isoformat()
        p = fetch_day(d, stride, log, dataset=dataset)
        if p is not None:
            if k > 0:
                log(f"ℹ️ {dataset} {date} 尚未發布，改用最新可用日期 {d}")
            return p, d
    return None, None


CHL_MIN = 0.5   # 只顯示 ≥ 0.5 mg/m³ 的水色點（高生產力/潛在漁場）


def chl_payload(lat, lon, arr, max_points: int = 4000,
                threshold: float = CHL_MIN) -> dict:
    """
    水色濃度稀疏點列表（僅含 ≥ threshold 的值），供前端以綠色空心圓
    大小分級呈現。子取樣以控制傳輸量；回傳 lons/lats/chl 與 min/max。
    """
    ny, nx = arr.shape
    finite = np.isfinite(arr) & (arr >= threshold)
    step = 1
    while (finite[::step, ::step]).sum() > max_points and step < 20:
        step += 1
    la = lat[::step]; lo = lon[::step]; a = arr[::step, ::step]
    m = np.isfinite(a) & (a >= threshold)
    ii, jj = np.where(m)
    lons = [round(float(lo[j]), 3) for j in jj]
    lats = [round(float(la[i]), 3) for i in ii]
    chl = [round(float(a[i, j]), 3) for i, j in zip(ii, jj)]
    return {
        "lons": lons, "lats": lats, "chl": chl, "step": step,
        "threshold": threshold,
        "min": round(float(min(chl)), 3) if chl else None,
        "max": round(float(max(chl)), 3) if chl else None,
        "count": len(chl),
    }


# ── Surface currents（表面地轉流向量）────────────────────────────────────────


def load_currents(path: pathlib.Path):
    """Read a currents subset file → (lat, lon, u, v) with NaN masking."""
    if not HAS_NETCDF4:
        raise RuntimeError("缺少 netCDF4 套件")
    ds = nc.Dataset(str(path))
    try:
        latv = ds.variables.get("latitude") or ds.variables.get("lat")
        lonv = ds.variables.get("longitude") or ds.variables.get("lon")
        lat = np.array(latv[:], dtype=np.float64)
        lon = np.array(lonv[:], dtype=np.float64)
        u = np.squeeze(ds.variables["u_current"][...])
        v = np.squeeze(ds.variables["v_current"][...])
        while u.ndim > 2:
            u = u[0]
        while v.ndim > 2:
            v = v[0]
    finally:
        ds.close()
    u = np.ma.filled(np.ma.masked_invalid(u), np.nan).astype(np.float32)
    v = np.ma.filled(np.ma.masked_invalid(v), np.nan).astype(np.float32)
    if lat[0] > lat[-1]:
        lat = lat[::-1]; u = u[::-1, :]; v = v[::-1, :]
    return lat, lon, u, v


def current_direction(u, v):
    """海洋慣例「流向」（去向）：0°=向北，順時針。"""
    return (np.degrees(np.arctan2(u, v)) + 360.0) % 360.0


def currents_payload(lat, lon, u, v, max_arrows: int = 900) -> dict:
    """Subsample the vector field to <=max_arrows arrows for the browser."""
    ny, nx = u.shape
    step = 1
    while (ny // step) * (nx // step) > max_arrows:
        step += 1
    la = lat[::step]; lo = lon[::step]
    us = u[::step, ::step]; vs = v[::step, ::step]
    spd = np.hypot(us, vs)
    di = current_direction(us, vs)
    lons, lats, uu, vv, ss, dd = [], [], [], [], [], []
    for i in range(la.size):
        for j in range(lo.size):
            if np.isfinite(spd[i, j]):
                lats.append(round(float(la[i]), 3))
                lons.append(round(float(lo[j]), 3))
                uu.append(round(float(us[i, j]), 3))
                vv.append(round(float(vs[i, j]), 3))
                ss.append(round(float(spd[i, j]), 3))
                dd.append(round(float(di[i, j]), 1))
    return {"lons": lons, "lats": lats, "u": uu, "v": vv,
            "speed": ss, "dir": dd, "step": step,
            "max_speed": round(float(np.nanmax(spd)), 3) if np.isfinite(spd).any() else None}
