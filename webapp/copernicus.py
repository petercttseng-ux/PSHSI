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
    "mur": {"dataset_id": "METOFFICE-GLO-SST-L4-NRT-OBS-SST-V2",
            "var": "analysed_sst", "scale": 1.0, "kelvin": True,
            "keep_raw": True, "raw_prefix": "OSTIA_SST", "raw_units": "kelvin"},
    "chl": {"dataset_id": "cmems_obs-oc_glo_bgc-plankton_nrt_l4-gapfree-multi-4km_P1D",
            "var": "CHL", "scale": 1.0},
    "ssh": {"dataset_id": "cmems_obs-sl_glo_phy-ssh_nrt_allsat-l4-duacs-0.25deg_P1D",
            "var": "sla", "scale": 100.0},
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
        a = np.squeeze(np.array(ds.variables[var][:], dtype=np.float64))
        while a.ndim > 2:
            a = a[0]
    finally:
        ds.close()
    a = np.ma.filled(np.ma.masked_invalid(a), np.nan)
    return lat, lon, a


def fetch_region_day(dataset: str, date: str, log: Callable[[str], None],
                     stride: int = 1):
    """Fetch the AOI subset of `dataset` for `date` from Copernicus Marine.
    Returns (lat, lon0360, arr2d, actual_date) or (None, None, None, None)."""
    if not HAS_NETCDF4:
        raise RuntimeError("缺少 netCDF4 套件")
    cfg = DATASETS[dataset]
    tag = uuid.uuid4().hex[:8]
    lon_parts, arr_parts, lat_ref = [], [], None
    for k, (lo0, lo1) in enumerate(_lon_windows()):
        out = f"cop_{dataset}_{date.replace('-', '')}_{k}_{tag}.nc"
        _subset(
            dataset_id=cfg["dataset_id"],
            variables=[cfg["var"]],
            minimum_longitude=lo0, maximum_longitude=lo1,
            minimum_latitude=LAT_MIN, maximum_latitude=LAT_MAX,
            start_datetime=f"{date}T00:00:00", end_datetime=f"{date}T23:59:59",
            output_filename=out, output_directory=str(_CACHE),
        )
        p = _CACHE / out
        if not p.exists():
            log(f"  ⚠️ Copernicus {dataset} 視窗 {k} 無輸出檔")
            return None, None, None, None
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
    lon_all, arr = lon_all[order], arr[:, order]
    if lat[0] > lat[-1]:
        lat, arr = lat[::-1], arr[::-1, :]

    # ── 保存下載的原始資料（OSTIA 為凱氏，未換算）到目錄 ──────────────
    if cfg.get("keep_raw"):
        raw = np.where(np.isfinite(arr), arr, np.nan) * cfg["scale"]
        raw_path = RAW_DIR / f"{cfg.get('raw_prefix', 'RAW')}_{date.replace('-', '')}.nc"
        _save_grid(raw_path, lat, lon_all, raw, cfg["var"], units=cfg.get("raw_units", ""))
        log(f"  💾 原始 {cfg.get('raw_prefix')} 已存檔（{cfg.get('raw_units')}）：{raw_path}")

    # subsample to ~TARGET_DEG（供地圖展示與 HSI）
    dlat = abs(float(np.median(np.diff(lat)))) if lat.size > 1 else TARGET_DEG
    step = max(1, int(round(TARGET_DEG / max(dlat, 1e-6))), int(stride) if stride else 1)
    lat, lon_all, arr = lat[::step], lon_all[::step], arr[::step, ::step]

    arr = np.where(np.isfinite(arr), arr, np.nan) * cfg["scale"]
    # ── 自動換算成攝氏溫度（OSTIA 凱氏 → °C）供右側地圖框展示 ───────────
    unit = ""
    if cfg.get("kelvin"):
        # 只有明顯為凱氏（均值 > 100）時才換算，避免來源已是 °C 時重複扣減。
        if np.isfinite(arr).any() and np.nanmean(arr) > 100:
            arr = kelvin_to_celsius(arr)
        unit = "°C"
    rng = ""
    if np.isfinite(arr).any():
        rng = f"，值域 {np.nanmin(arr):.2f}~{np.nanmax(arr):.2f}{unit}"
    log(f"  ✅ Copernicus {dataset.upper()} {date}："
        f"{arr.shape[0]}×{arr.shape[1]} 格（{cfg['dataset_id']}）{rng}")
    return lat, lon_all, arr.astype(np.float32), date
