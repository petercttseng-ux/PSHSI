"""
NASA Earthdata OPeNDAP fetcher — server-side subsetting for SST / Chl-a / SSHA.

Why this module
───────────────
The institute network cannot reach the NOAA CoastWatch ERDDAP servers, but it
can reach NASA Earthdata. This module fetches the three environmental fields
used by the fishing-ground prediction (SST, ocean-colour chlorophyll, and sea
surface height anomaly) entirely from NASA Earthdata, subsetting on the server
via OPeNDAP so only the tropical-Pacific study area (20°S–20°N, 130°E–150°W)
is downloaded.

Flow (per variable, per date)
  1. CMR granule search (short_name + temporal window, newest first) → the
     latest granule at/just before the requested date, and its OPeNDAP URL.
  2. Read the granule's DAP4 metadata (.dmr.xml) to discover the data
     variable's dimensions and the lat/lon coordinate names/order.
  3. Fetch the 1-D lat/lon coordinate vectors, compute the index window(s)
     covering the AOI (0–360°, dateline-aware → up to two longitude runs).
  4. Fetch only that index hyperslab (.dap.nc4?dap4.ce=…) and stitch the
     longitude runs into one ascending 0–360 grid.

Authentication uses the Earthdata bearer token via nasa_auth.auth_session().

Datasets
  sst : MUR-JPL-L4-GLOB-v4.1                                   var analysed_sst (K)
  chl : VIIRS S-NPP L3m daily chlorophyll                      var chlor_a (mg/m³)
  ssh : MEaSUREs Gridded SSHA v2205 (5-day, ~3-month latency)  var SLA (m→cm)

Marine Environmental Research, Fisheries Research Institute, MOA.
"""
from __future__ import annotations

import datetime
import xml.etree.ElementTree as ET
from typing import Callable, Optional

import numpy as np

try:
    import netCDF4 as nc
    HAS_NETCDF4 = True
except ImportError:
    HAS_NETCDF4 = False

import nasa_auth
from sst_processor import LAT_MIN, LAT_MAX, LON_MIN, LON_MAX  # AOI (0–360 lon)

CMR_GRANULES = "https://cmr.earthdata.nasa.gov/search/granules.json"

# Earthdata dataset registry. `short_name` drives the CMR search; `src_var`
# is the OPeNDAP variable to subset; `scale` converts to the display unit
# (SSHA metres → centimetres); `fallback_days` is how far back CMR may look
# for the newest available granule (SSHA is 5-day with a long latency).
# `short_name` may be a single CMR short_name or a list of candidates tried
# in order (used for ocean colour, whose exact collection name can vary).
DATASETS = {
    "sst": {"short_name": "MUR-JPL-L4-GLOB-v4.1",
            "src_var": "analysed_sst", "scale": 1.0, "fallback_days": 14},
    "chl": {"short_name": ["VIIRSN_L3m_CHL", "VIIRSJ1_L3m_CHL", "MODISA_L3m_CHL"],
            "src_var": "chlor_a", "scale": 1.0, "fallback_days": 60},
    "ssh": {"short_name": "SEA_SURFACE_HEIGHT_ALT_GRIDS_L4_2SATS_5DAY_6THDEG_V_JPL2205",
            "src_var": "SLA", "scale": 100.0, "fallback_days": 150},
}

_coord_cache: dict = {}   # opendap_base → (lat, lon, latname, lonname, has_time)


# ── HTTP helpers ───────────────────────────────────────────────────────────
def _session():
    return nasa_auth.auth_session()


def _clean_base(url: str) -> str:
    """Normalise a CMR OPeNDAP link to a base we can suffix (.dmr.xml, .dap.nc4)."""
    for suf in (".dmr.xml", ".dap.nc4", ".dods", ".dds", ".das", ".html", ".nc4", ".nc"):
        if url.endswith(suf):
            url = url[: -len(suf)]
    return url


# ── CMR granule discovery ──────────────────────────────────────────────────
def find_granule(dataset: str, date: str, log: Callable[[str], None],
                 session=None) -> tuple:
    """Return (opendap_base, granule_date) for the newest granule at/≤ `date`,
    or (None, None)."""
    cfg = DATASETS[dataset]
    s = session or _session()
    d1 = datetime.date.fromisoformat(date)
    d0 = d1 - datetime.timedelta(days=cfg["fallback_days"])
    names = cfg["short_name"]
    if isinstance(names, str):
        names = [names]
    for short_name in names:
        params = {
            "short_name": short_name,
            "temporal": f"{d0.isoformat()}T00:00:00Z,{d1.isoformat()}T23:59:59Z",
            "sort_key": "-start_date",
            "page_size": 20,
        }
        r = s.get(CMR_GRANULES, params=params, timeout=45)
        r.raise_for_status()
        entries = r.json().get("feed", {}).get("entry", [])
        for entry in entries:
            for link in entry.get("links", []):
                href = link.get("href", "")
                # OPeNDAP service link = any http(s) link mentioning 'opendap'
                # that is not a documentation/help page.
                low = href.lower()
                if "opendap" in low and low.startswith("http") and "help" not in low:
                    return _clean_base(href), (entry.get("time_start", "") or "")[:10]
        if entries:
            log(f"  ⚠️ {short_name}：找到 granule 但無 OPeNDAP 連結，試下一候選")
    return None, None


# ── DAP4 metadata (dimension / coordinate discovery) ───────────────────────
def _read_dmr(base: str, src_var: str, s) -> tuple:
    """Parse .dmr.xml → (latname, lonname, has_time). Assumes the data variable's
    last two dims are (lat, lon) and an optional leading time dim."""
    r = s.get(base + ".dmr.xml", timeout=45)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    ns = {"d": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}

    def _local(tag):
        return tag.split("}")[-1]

    # Find the data variable element and read its <Dim name="/x"/> order.
    dims = None
    for el in root.iter():
        if _local(el.tag) in ("Float32", "Float64", "Int16", "Int32", "Byte", "Int8") \
                and el.attrib.get("name") == src_var:
            dims = [d.attrib.get("name", "").lstrip("/")
                    for d in el if _local(d.tag) == "Dim"]
            break
    if not dims:
        raise RuntimeError(f"DMR 中找不到變數 {src_var}")
    has_time = len(dims) >= 3
    latname, lonname = dims[-2], dims[-1]
    return latname, lonname, has_time


def _open_mem(content: bytes):
    return nc.Dataset("inmem.nc", memory=content)


def _get_coords(base: str, src_var: str, s):
    """Return (lat, lon, latname, lonname, has_time), cached per base."""
    if base in _coord_cache:
        return _coord_cache[base]
    latname, lonname, has_time = _read_dmr(base, src_var, s)
    r = s.get(base + ".dap.nc4", params={"dap4.ce": f"/{latname};/{lonname}"},
              timeout=120)
    r.raise_for_status()
    ds = _open_mem(r.content)
    try:
        lat = np.array(ds.variables[latname][:], dtype=np.float64)
        lon = np.array(ds.variables[lonname][:], dtype=np.float64)
    finally:
        ds.close()
    out = (lat, lon, latname, lonname, has_time)
    _coord_cache[base] = out
    return out


# ── Index windows (0–360, dateline-aware) ──────────────────────────────────
def _contiguous_runs(idx: np.ndarray) -> list:
    """Split a sorted index array into contiguous (start, end) inclusive runs."""
    if idx.size == 0:
        return []
    runs, s0 = [], idx[0]
    prev = idx[0]
    for v in idx[1:]:
        if v != prev + 1:
            runs.append((int(s0), int(prev)))
            s0 = v
        prev = v
    runs.append((int(s0), int(prev)))
    return runs


def _windows(lat: np.ndarray, lon: np.ndarray):
    """Return (i0, i1, lon_runs, lon360) covering the AOI."""
    lon360 = np.where(lon < 0, lon + 360.0, lon)
    ii = np.where((lat >= LAT_MIN) & (lat <= LAT_MAX))[0]
    if ii.size == 0:
        raise RuntimeError("AOI 緯度範圍內無格點")
    jj = np.sort(np.where((lon360 >= LON_MIN) & (lon360 <= LON_MAX))[0])
    runs = _contiguous_runs(jj)
    if not runs:
        raise RuntimeError("AOI 經度範圍內無格點")
    return int(ii[0]), int(ii[-1]), runs, lon360


# ── Public: fetch one day's AOI subset ─────────────────────────────────────
def fetch_region_day(dataset: str, date: str, log: Callable[[str], None],
                     stride: int = 1):
    """Fetch the AOI subset of `dataset` for the newest granule ≤ `date`.
    `stride` subsamples the native grid (essential for 1 km MUR).
    Returns (lat, lon0360, arr2d, actual_date) or (None, None, None, None)."""
    if not HAS_NETCDF4:
        raise RuntimeError("缺少 netCDF4 套件")
    stride = max(1, int(stride))
    cfg = DATASETS[dataset]
    s = _session()
    base, gdate = find_granule(dataset, date, log, session=s)
    if base is None:
        return None, None, None, None
    src_var = cfg["src_var"]
    lat, lon, latname, lonname, has_time = _get_coords(base, src_var, s)
    i0, i1, lon_runs, lon360 = _windows(lat, lon)

    lat_sub = lat[i0:i1 + 1:stride]
    lon_parts, arr_parts = [], []
    tprefix = "[0]" if has_time else ""
    for (j0, j1) in lon_runs:
        ce = (f"/{src_var}{tprefix}[{i0}:{stride}:{i1}][{j0}:{stride}:{j1}];"
              f"/{lonname}[{j0}:{stride}:{j1}]")
        r = s.get(base + ".dap.nc4", params={"dap4.ce": ce}, timeout=180)
        r.raise_for_status()
        ds = _open_mem(r.content)
        try:
            a = np.squeeze(np.array(ds.variables[src_var][:], dtype=np.float64))
            while a.ndim > 2:
                a = a[0]
            lo = np.array(ds.variables[lonname][:], dtype=np.float64)
        finally:
            ds.close()
        lo = np.where(lo < 0, lo + 360.0, lo)
        lon_parts.append(lo)
        arr_parts.append(a)

    lon_all = np.concatenate(lon_parts)
    arr = np.concatenate(arr_parts, axis=1)
    order = np.argsort(lon_all, kind="stable")
    lon_all = lon_all[order]
    arr = arr[:, order]
    if lat_sub[0] > lat_sub[-1]:            # ensure ascending latitude
        lat_sub = lat_sub[::-1]
        arr = arr[::-1, :]

    arr = np.where(np.isfinite(arr), arr, np.nan) * cfg["scale"]
    actual = gdate or date
    log(f"  ✅ Earthdata {dataset.upper()} {actual}："
        f"{arr.shape[0]}×{arr.shape[1]} 格（{cfg['short_name']}）")
    return lat_sub, lon_all, arr.astype(np.float32), actual
