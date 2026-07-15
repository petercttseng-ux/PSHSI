"""
Copernicus Marine (CMEMS) fetcher — server-side subsetting for SST/Chl-a/SSHA.

The three environmental fields used by the fishing-ground prediction are
downloaded from the Copernicus Marine Data Store via the official
`copernicusmarine` Python toolbox (`copernicusmarine.subset`), which subsets
by variable / bounding box / date on the server and returns a small NetCDF.

Datasets
  mur (SST) : METOFFICE-GLO-SST-L4-NRT-OBS-SST-V2               analysed_sst (K)
  chl       : cmems_obs-oc_glo_bgc-plankton_nrt_l4-gapfree-multi-4km_P1D   CHL (mg/m³)
  ssh       : cmems_obs-sl_glo_phy-ssh_nrt_allsat-l4-duacs-0.25deg_P1D     sla (m→cm)

Authentication: run once on the machine —
    copernicusmarine login
(or set COPERNICUSMARINE_SERVICE_USERNAME / _PASSWORD). No credential is
stored in this repository.

The AOI (20°S–20°N, 130°E–150°W) crosses the 180° dateline; CMEMS grids are
−180…180, so the request is split into an east (130…180) and a west
(−180…−150) window and stitched into one ascending 0–360 grid.

Marine Environmental Research, Fisheries Research Institute, MOA.
"""
from __future__ import annotations

import pathlib
import re
import uuid
from typing import Callable

import numpy as np

try:
    import netCDF4 as nc
    HAS_NETCDF4 = True
except ImportError:
    HAS_NETCDF4 = False

from sst_processor import LAT_MIN, LAT_MAX, LON_MIN, LON_MAX, DATA_DIR

# Keyed by the same dataset names timeseries.py uses (mur/chl/ssh).
DATASETS = {
    # OSTIA analysed_sst is in Kelvin → the raw file is kept on disk and an
    # explicit converter (kelvin_to_celsius) produces the °C map values.
    # NRT 延遲：~1 天（fallback_days=2 即可覆蓋）
    # 網格上限：179.975°（不可請求 180.0）
    "mur": {"dataset_id": "METOFFICE-GLO-SST-L4-NRT-OBS-SST-V2",
            "var": "analysed_sst", "scale": 1.0, "kelvin": True,
            "keep_raw": True, "raw_prefix": "OSTIA_SST", "raw_units": "kelvin",
            "lon_max_east": 179.975, "fallback_days": 3},
    # GlobColour Chl-a NRT 延遲：~3 天
    "chl": {"dataset_id": "cmems_obs-oc_glo_bgc-plankton_nrt_l4-gapfree-multi-4km_P1D",
            "var": "CHL", "scale": 1.0,
            "lon_max_east": 179.98},
    "ssh": {
        # 0.125° NRT 為目前 CMEMS DUACS 主力（version 202506，資料從 2024-07-01 至今每日更新）
        "dataset_id": "cmems_obs-sl_glo_phy-ssh_nrt_allsat-l4-duacs-0.125deg_P1D",
        # 0.25° 備援（202311 版本，已於 2026-05-14 停止更新，保留供歷史資料回溯）
        "dataset_id_alt": "cmems_obs-sl_glo_phy-ssh_nrt_allsat-l4-duacs-0.25deg_P1D",
        "var": "sla", "scale": 100.0,
        # DUACS 網格上限為 179.875，不可請求 180.0
        "lon_max_east": 179.875},
}

TARGET_DEG = 0.1        # subsample the merged grid to ~0.1° for display/HSI
_CACHE = DATA_DIR / "copernicus"
_CACHE.mkdir(exist_ok=True)
RAW_DIR = DATA_DIR / "ostia_raw"     # 保留下載的原始 OSTIA 檔（凱氏）
RAW_DIR.mkdir(exist_ok=True)


def kelvin_to_celsius(arr):
    """自動將凱氏溫度（K）轉為攝氏溫度（°C）：°C = K − 273.15（NaN 保留）。"""
    a = np.asarray(arr, dtype=np.float64)
    return np.where(np.isfinite(a), a - 273.15, np.nan)


def _save_grid(path, lat, lon, arr, varname, units=""):
    """把一個 (lat, lon) 網格寫成 NetCDF（供原始資料保存）。"""
    ds = nc.Dataset(str(path), "w", format="NETCDF4")
    try:
        ds.createDimension("lat", lat.size)
        ds.createDimension("lon", lon.size)
        vla = ds.createVariable("latitude", "f8", ("lat",)); vla[:] = lat
        vla.units = "degrees_north"
        vlo = ds.createVariable("longitude", "f8", ("lon",)); vlo[:] = lon
        vlo.units = "degrees_east"
        vv = ds.createVariable(varname, "f4", ("lat", "lon"),
                               zlib=True, fill_value=np.float32(np.nan))
        vv[:] = np.asarray(arr, dtype=np.float32)
        if units:
            vv.units = units
        ds.institution = "Copernicus Marine (CMEMS) via FRI MOA"
    finally:
        ds.close()


def _lon_windows():
    """AOI longitude windows in the −180…180 convention used by CMEMS."""
    if LON_MAX <= 180:
        return [(LON_MIN, LON_MAX)]
    # east 130…180  +  west (210−360)…-… → −180…−150
    return [(LON_MIN, 180.0), (-180.0, LON_MAX - 360.0)]


def _safe_log(log: Callable[[str], None], msg: str) -> None:
    """Call `log(msg)` and silently absorb any UnicodeEncodeError.
    This prevents CP950 / other narrow-codec consoles from breaking the
    download pipeline when the message contains emoji."""
    try:
        log(msg)
    except (UnicodeEncodeError, UnicodeDecodeError):
        try:
            log(msg.encode("ascii", "replace").decode("ascii"))
        except Exception:
            pass   # never let a log call crash the pipeline
    except Exception:
        pass


def _subset(**kw):
    """Call copernicusmarine.subset tolerantly across toolbox versions."""
    import copernicusmarine as cm
    last = None
    for extra in ({"overwrite": True}, {"force_download": True}, {}):
        try:
            return cm.subset(**kw, **extra)
        except TypeError as e:      # unknown kwarg for this version
            last = e
            continue
    if last:
        raise last
    return cm.subset(**kw)


def _read_nc(path: pathlib.Path, var: str):
    """Return (lat, lon, arr2d) from a CMEMS subset file (NaN-masked)."""
    ds = nc.Dataset(str(path))
    try:
        latv = ds.variables.get("latitude") or ds.variables.get("lat")
        lonv = ds.variables.get("longitude") or ds.variables.get("lon")
        lat = np.array(latv[:], dtype=np.float64)
        lon = np.array(lonv[:], dtype=np.float64)
        raw = ds.variables[var][:]   # netCDF4 MaskedArray — keep mask intact
        raw = np.squeeze(raw)
        while raw.ndim > 2:
            raw = raw[0]
        # Fill masked/invalid cells with NaN before converting to plain float64
        a = np.ma.filled(raw.astype(np.float64), np.nan)
    finally:
        ds.close()
    a = np.where(np.isfinite(a), a, np.nan)   # extra safety pass
    return lat, lon, a


def fetch_region_day(dataset: str, date: str, log: Callable[[str], None],
                     stride: int = 1, convert: bool = True):
    """Fetch the AOI subset of `dataset` for `date` from Copernicus Marine.
    `convert=False` returns OSTIA SST in the raw unit (Kelvin) without the
    °C conversion (the raw file is still saved to disk).
    Returns (lat, lon0360, arr2d, actual_date) or (None, None, None, None).

    Automatically falls back to `dataset_id_alt` when the primary fails.
    Honours `lon_max_east` to stay within dataset grid bounds (e.g. 179.875
    for DUACS products whose grid does not reach exactly 180°).
    """
    if not HAS_NETCDF4:
        raise RuntimeError("缺少 netCDF4 套件")
    cfg = DATASETS[dataset]

    # East-lon ceiling (DUACS grid stops at 179.875, not 180.0)
    lon_max_east = cfg.get("lon_max_east", 180.0)

    def _lon_windows_for(lon_max_e):
        """Same logic as module-level _lon_windows() but with a custom ceiling."""
        if LON_MAX <= 180:
            return [(LON_MIN, min(LON_MAX, lon_max_e))]
        return [(LON_MIN, lon_max_e), (-180.0, LON_MAX - 360.0)]

    def _try_dataset_id(did):
        """Try downloading with a specific dataset_id. Returns merged arrays or
        raises an exception."""
        tag = uuid.uuid4().hex[:8]
        lon_parts, arr_parts, lat_ref = [], [], None
        for k, (lo0, lo1) in enumerate(_lon_windows_for(lon_max_east)):
            out = f"cop_{dataset}_{date.replace('-', '')}_{k}_{tag}.nc"
            _subset(
                dataset_id=did,
                variables=[cfg["var"]],
                minimum_longitude=lo0, maximum_longitude=lo1,
                minimum_latitude=LAT_MIN, maximum_latitude=LAT_MAX,
                start_datetime=f"{date}T00:00:00", end_datetime=f"{date}T12:00:00",
                output_filename=out, output_directory=str(_CACHE),
            )
            p = _CACHE / out
            if not p.exists() or p.stat().st_size < 100:
                # clean up and signal failure
                try:
                    p.unlink()
                except OSError:
                    pass
                raise RuntimeError(f"Copernicus {dataset} 視窗 {k} 無輸出檔")
            lat, lon, a = _read_nc(p, cfg["var"])
            try:
                p.unlink()
            except OSError:
                pass
            lat_ref = lat
            lon_parts.append(np.where(lon < 0, lon + 360.0, lon))
            arr_parts.append(a)

        lat = lat_ref
        lon_all = np.concatenate(lon_parts)
        arr = np.concatenate(arr_parts, axis=1)
        order = np.argsort(lon_all, kind="stable")
        return lat, lon_all[order], arr[:, order]

    # ── Try primary dataset_id, then alt ─────────────────────────────────
    primary_id = cfg["dataset_id"]
    alt_id     = cfg.get("dataset_id_alt")
    lat = lon_all = arr = None
    last_err = None

    for did in ([primary_id, alt_id] if alt_id else [primary_id]):
        if did is None:
            continue
        try:
            lat, lon_all, arr = _try_dataset_id(did)
            if did != primary_id:
                _safe_log(log, f"  [INFO] Using alt dataset: {did}")
            break
        except Exception as e:
            last_err = e
            _safe_log(log, f"  [WARN] {did} failed: {e}")

    if arr is None:
        _safe_log(log, f"  [ERR] Copernicus {dataset} {date}: all sources failed")
        return None, None, None, None

    if lat[0] > lat[-1]:
        lat, arr = lat[::-1], arr[::-1, :]

    # ── 保存下載的原始資料（OSTIA 為凱氏，未換算）到目錄 ──────────────
    if cfg.get("keep_raw"):
        raw = np.where(np.isfinite(arr), arr, np.nan) * cfg["scale"]
        raw_path = RAW_DIR / f"{cfg.get('raw_prefix', 'RAW')}_{date.replace('-', '')}.nc"
        _save_grid(raw_path, lat, lon_all, raw, cfg["var"], units=cfg.get("raw_units", ""))
        _safe_log(log, f"  [SAVE] Raw {cfg.get('raw_prefix')} saved ({cfg.get('raw_units')}): {raw_path}")

    # subsample to ~TARGET_DEG（供地圖展示與 HSI）
    dlat = abs(float(np.median(np.diff(lat)))) if lat.size > 1 else TARGET_DEG
    step = max(1, int(round(TARGET_DEG / max(dlat, 1e-6))), int(stride) if stride else 1)
    lat, lon_all, arr = lat[::step], lon_all[::step], arr[::step, ::step]

    arr = np.where(np.isfinite(arr), arr, np.nan) * cfg["scale"]
    # ── 自動換算成攝氏溫度（OSTIA 凱氏 → °C）供右側地圖框展示 ───────────
    unit = ""
    if cfg.get("kelvin"):
        unit = "°C"
        if convert and np.isfinite(arr).any() and np.nanmean(arr) > 100:
            arr = kelvin_to_celsius(arr)
        elif not convert:
            unit = "K"      # 保留原始凱氏（供「下載原始」顯示）
    rng = ""
    if np.isfinite(arr).any():
        rng = f"，值域 {np.nanmin(arr):.2f}~{np.nanmax(arr):.2f}{unit}"
    _safe_log(log, f"  [OK] Copernicus {dataset.upper()} {date}: "
              f"{arr.shape[0]}x{arr.shape[1]} grid ({cfg['dataset_id']}){rng}")
    return lat, lon_all, arr.astype(np.float32), date


def load_raw_celsius(date: str, log: Callable[[str], None], stride: int = 1):
    """讀取已保存的 OSTIA 原始檔（凱氏），換算成攝氏並回傳供地圖展示。
    找不到指定日期時改用最新一個原始檔。
    Returns (lat, lon0360, arr_celsius, actual_date) or (None, None, None, None)."""
    if not HAS_NETCDF4:
        raise RuntimeError("缺少 netCDF4 套件")
    cfg = DATASETS["mur"]
    prefix, var = cfg["raw_prefix"], cfg["var"]
    path = RAW_DIR / f"{prefix}_{date.replace('-', '')}.nc"
    if not path.exists():
        files = sorted(RAW_DIR.glob(f"{prefix}_*.nc"))
        if not files:
            return None, None, None, None
        path = files[-1]
    m = re.search(r"(\d{8})", path.name)
    actual = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]}" if m else date

    ds = nc.Dataset(str(path))
    try:
        latv = ds.variables.get("latitude") or ds.variables.get("lat")
        lonv = ds.variables.get("longitude") or ds.variables.get("lon")
        lat = np.array(latv[:], dtype=np.float64)
        lon = np.array(lonv[:], dtype=np.float64)
        arr = np.ma.filled(np.ma.masked_invalid(np.squeeze(ds.variables[var][:])), np.nan)
    finally:
        ds.close()

    cel = kelvin_to_celsius(arr)     # ← 攝氏換算
    dlat = abs(float(np.median(np.diff(lat)))) if lat.size > 1 else TARGET_DEG
    step = max(1, int(round(TARGET_DEG / max(dlat, 1e-6))), int(stride) if stride else 1)
    lat, lon, cel = lat[::step], lon[::step], cel[::step, ::step]
    fin = cel[np.isfinite(cel)]
    rng = f"，值域 {fin.min():.2f}~{fin.max():.2f}°C" if fin.size else ""
    _safe_log(log, f"  [SST] OSTIA raw->Celsius: {path.name}{rng}")
    return lat, lon, cel.astype(np.float32), actual
