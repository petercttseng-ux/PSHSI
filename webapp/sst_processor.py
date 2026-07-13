"""
GHRSST MUR SST data processor
Refactored from ghrsst_sst_gui.py for use in the Flask web application.
Marine Environmental Research, Fisheries Research Institute, MOA
"""
from __future__ import annotations

import datetime
import pathlib
import re
import sys
import threading
import traceback
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

try:
    import netCDF4 as nc
    HAS_NETCDF4 = True
except ImportError:
    HAS_NETCDF4 = False

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from scipy import ndimage
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False


# ── Constants ──────────────────────────────────────────────────────────────
# Area of interest: tropical West/Central Pacific tuna grounds.
#   Latitude  : 20°S – 20°N
#   Longitude : 130°E – 150°W  → expressed in the 0–360° convention as
#               130 – 210 (the band crosses the 180° dateline).
# Throughout the system longitude is handled in the 0–360 convention so the
# region is a single contiguous interval; datasets stored in the −180…180
# convention are converted on the fly (see load_nc_file / ERDDAP helpers).
LON_MIN, LON_MAX = 130.0, 210.0
LAT_MIN, LAT_MAX = -20.0, 20.0

# −180…180 form of the same corners (needed by CMR bounding-box queries,
# which do not accept longitudes > 180). West corner stays 130; the east
# corner 210 maps to 210 − 360 = −150. lon_min > lon_max signals CMR that
# the box crosses the antimeridian.
LON_MIN_PM180 = LON_MIN if LON_MIN <= 180 else LON_MIN - 360   # 130
LON_MAX_PM180 = LON_MAX if LON_MAX <= 180 else LON_MAX - 360   # -150
CROSSES_DATELINE = LON_MAX > 180

# Credentials are managed by nasa_auth.py (env var / token file / netrc).
# Nothing is hard-coded here.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import nasa_auth

COLLECTION_ID = "C1996881146-POCLOUD"
CMR_GRANULE_URL = (
    "https://cmr.earthdata.nasa.gov/search/granules.json"
    "?collection_concept_id={collection_id}"
    "&sort_key=-start_date&page_size=1"
    "&bounding_box={lon_min},{lat_min},{lon_max},{lat_max}"
)
CMR_VIRTUAL_BASE = "https://cmr.earthdata.nasa.gov/virtual-directory/collections"

DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "mur_data"
DATA_DIR.mkdir(exist_ok=True)


# ── Credentials ────────────────────────────────────────────────────────────
def _auth_session() -> "requests.Session":
    """requests Session with Earthdata credentials (see nasa_auth.py)."""
    return nasa_auth.auth_session()


def check_credentials() -> dict:
    """Return token status dict; see nasa_auth.token_status()."""
    return nasa_auth.token_status()


def setup_netrc() -> None:
    """No-op kept for backward compatibility — see nasa_auth.py."""
    return


# ── File utilities ─────────────────────────────────────────────────────────
def find_latest_nc() -> Optional[pathlib.Path]:
    files = sorted(DATA_DIR.glob("*.nc"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def list_local_files() -> list[dict]:
    items = []
    for p in sorted(DATA_DIR.glob("*.nc"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = p.stat()
        m = re.search(r"(\d{8})", p.name)
        date_str = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]}" if m else ""
        items.append({
            "name": p.name,
            "size_mb": round(st.st_size / 1e6, 1),
            "mtime": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "date": date_str,
        })
    return items


# ── NASA download ──────────────────────────────────────────────────────────
def fetch_latest_granule_url(log: Callable[[str], None]):
    if not HAS_REQUESTS:
        log("❌ 需安裝 requests：pip install requests")
        return None, None

    log("🔍 查詢 NASA CMR 最新 MUR granule …")
    api_url = CMR_GRANULE_URL.format(
        collection_id=COLLECTION_ID,
        lon_min=LON_MIN_PM180, lat_min=LAT_MIN,
        lon_max=LON_MAX_PM180, lat_max=LAT_MAX,
    )
    try:
        session = _auth_session()
        resp = session.get(api_url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        entries = data.get("feed", {}).get("entry", [])
        if not entries:
            log("⚠️  CMR API 無回覆，改用虛擬目錄")
            return _crawl_virtual_directory(log, session)

        entry = entries[0]
        for link in entry.get("links", []):
            href = link.get("href", "")
            if href.startswith("https://") and href.endswith(".nc") and "podaac" in href:
                fname = href.rsplit("/", 1)[-1]
                log(f"✅ 找到最新 granule：{fname}")
                return href, fname

        granule_id = entry.get("producer_granule_id") or entry.get("title", "")
        if granule_id:
            fname = granule_id if granule_id.endswith(".nc") else granule_id + ".nc"
            dl_url = (
                "https://archive.podaac.earthdata.nasa.gov/"
                f"podaac-ops-cumulus-protected/MUR-JPL-L4-GLOB-v4.1/{fname}"
            )
            log(f"✅ (API fallback) {fname}")
            return dl_url, fname
    except Exception as e:
        log(f"⚠️  CMR API 失敗：{e}，改用虛擬目錄")

    return _crawl_virtual_directory(log, None)


def _crawl_virtual_directory(log, session=None):
    if session is None:
        session = _auth_session()

    now = datetime.datetime.utcnow()
    for year in (str(now.year), str(now.year - 1)):
        base = f"{CMR_VIRTUAL_BASE}/{COLLECTION_ID}/temporal/{year}"
        log(f"  → 探索：{year}")
        try:
            r = session.get(base, timeout=20)
            r.raise_for_status()
            months = sorted(set(re.findall(rf"/{year}/(\d{{2}})", r.text)), reverse=True)
            if not months:
                continue
            for month in months:
                rm = session.get(f"{base}/{month}", timeout=20)
                rm.raise_for_status()
                days = sorted(set(re.findall(rf"/{month}/(\d{{2}})", rm.text)), reverse=True)
                if not days:
                    continue
                for day in days:
                    rd = session.get(f"{base}/{month}/{day}", timeout=20)
                    rd.raise_for_status()
                    nc_links = re.findall(
                        r'href="(https://archive\.podaac\.earthdata\.nasa\.gov[^"]+\.nc)"',
                        rd.text,
                    )
                    if nc_links:
                        url = nc_links[0]
                        fname = url.rsplit("/", 1)[-1]
                        log(f"✅ 虛擬目錄：{fname}")
                        return url, fname
        except Exception as e:
            log(f"  ⚠️ 虛擬目錄錯誤：{e}")
            continue

    log("❌ 無法取得最新 granule")
    return None, None


def download_file(url, dest_path, log, progress_cb=None) -> None:
    session = _auth_session()
    # PODAAC redirects through urs.earthdata; let requests follow it,
    # but DO NOT strip Authorization across the redirect — it's the same trust domain.
    resp = session.get(url, stream=True, timeout=120, allow_redirects=True)
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))
    downloaded = 0
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
    try:
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb and total:
                    progress_cb(downloaded / total * 100)
        if total and downloaded != total:
            raise RuntimeError(
                f"下載不完整：{downloaded / 1e6:.1f} / {total / 1e6:.1f} MB")
        tmp_path.replace(dest_path)   # 原子改名：只有完整檔會出現在 mur_data
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    log(f"💾 下載完成：{dest_path.name} ({downloaded / 1e6:.1f} MB)")


# ── NetCDF loading ─────────────────────────────────────────────────────────
@dataclass
class SSTField:
    """Container for processed SST data."""
    lon: np.ndarray
    lat: np.ndarray
    sst: np.ndarray   # masked array, °C
    date: str
    filename: str
    shape: tuple
    sst_min: float
    sst_max: float
    sst_mean: float

    def downsample(self, factor: int) -> "SSTField":
        if factor <= 1:
            return self
        sst_ds = self.sst[::factor, ::factor]
        return SSTField(
            lon=self.lon[::factor].copy(),
            lat=self.lat[::factor].copy(),
            sst=sst_ds,
            date=self.date,
            filename=self.filename,
            shape=sst_ds.shape,
            sst_min=self.sst_min,
            sst_max=self.sst_max,
            sst_mean=self.sst_mean,
        )

    def to_payload(self, max_points: int = 220_000) -> dict:
        """Convert to a JSON-serializable dict for the browser."""
        rows, cols = self.sst.shape
        factor = max(1, int(np.ceil(np.sqrt(rows * cols / max_points))))
        ds = self.downsample(factor) if factor > 1 else self

        # Replace masked / non-finite with None for JSON
        arr = np.array(ds.sst, dtype=np.float32)
        if np.ma.is_masked(ds.sst):
            arr = np.where(ds.sst.mask, np.nan, arr)
        # Convert NaN → None for JSON
        values = [
            [None if not np.isfinite(v) else round(float(v), 2) for v in row]
            for row in arr
        ]
        return {
            "lon": [round(float(x), 4) for x in ds.lon],
            "lat": [round(float(y), 4) for y in ds.lat],
            "values": values,
            "date": ds.date,
            "filename": ds.filename,
            "shape": list(ds.shape),
            "factor": factor,
            "stats": {
                "min": round(self.sst_min, 2),
                "max": round(self.sst_max, 2),
                "mean": round(self.sst_mean, 2),
            },
        }


def _windows_safe_path(path: pathlib.Path) -> str:
    """
    netCDF4-python on Windows can't open files whose path contains
    non-ASCII characters (the underlying C library mishandles UTF-8).
    Convert to the 8.3 short path name to side-step this on Windows.
    """
    s = str(path)
    if sys.platform != "win32":
        return s
    try:
        s.encode("ascii")
        return s   # already ASCII, no need
    except UnicodeEncodeError:
        pass
    try:
        import ctypes
        from ctypes import wintypes
        GetShortPathNameW = ctypes.windll.kernel32.GetShortPathNameW
        GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        GetShortPathNameW.restype  = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(260)
        rv = GetShortPathNameW(s, buf, 260)
        if rv and rv < 260:
            return buf.value
    except Exception:
        pass
    return s


def _open_dataset(path: pathlib.Path):
    """
    Open a NetCDF file. netCDF4-python on Windows mishandles non-ASCII
    paths, so we try (1) the short path, (2) chdir + relative open,
    (3) reading the file into memory and opening from bytes.
    """
    # 1) direct open (works on POSIX or pure-ASCII paths)
    try:
        return nc.Dataset(_windows_safe_path(path))
    except (OSError, ValueError):
        pass

    # 2) chdir + relative open — file name is ASCII even if parent isn't.
    if sys.platform == "win32":
        import threading as _th
        with _open_dataset._chdir_lock:
            old = pathlib.Path.cwd()
            try:
                import os
                os.chdir(str(path.parent))
                return nc.Dataset(path.name)
            except Exception:
                pass
            finally:
                try:
                    os.chdir(str(old))
                except Exception:
                    pass

    # 3) read into memory (last resort — loads whole file in RAM)
    with open(path, "rb") as f:
        blob = f.read()
    return nc.Dataset(path.name, mode="r", memory=blob)


_open_dataset._chdir_lock = threading.Lock()


def load_nc_file(path: pathlib.Path) -> SSTField:
    if not HAS_NETCDF4:
        raise RuntimeError("缺少 netCDF4 套件，請安裝：pip install netCDF4")

    ds = _open_dataset(path)
    try:
        lat = ds.variables["lat"][:]
        lon = np.asarray(ds.variables["lon"][:], dtype=np.float64)
        # Work in 0–360 so the dateline-crossing AOI is one contiguous band.
        lon360 = np.where(lon < 0, lon + 360.0, lon)

        ilat = np.where((lat >= LAT_MIN) & (lat <= LAT_MAX))[0]
        # East segment: native positive lon 130…180 (contiguous mid-grid).
        ie = np.where((lon360 >= LON_MIN) & (lon360 <= min(LON_MAX, 180.0)))[0]
        # West segment: native negative lon (−180…−150) → 180…210 in 0–360.
        iw = (np.where((lon360 > 180.0) & (lon360 <= LON_MAX))[0]
              if LON_MAX > 180 else np.array([], dtype=int))
        # Order columns west→east by 0–360 longitude, then de-duplicate.
        ilon = np.concatenate([ie, iw])
        order = np.argsort(lon360[ilon], kind="stable")
        ilon = ilon[order]
        lat_sub = lat[ilat]
        lon_sub = lon360[ilon]

        ds.set_auto_maskandscale(False)
        sst_var = ds.variables["analysed_sst"]
        r0, r1 = ilat[0], ilat[-1] + 1
        if sst_var.ndim == 3:
            sst_raw = np.asarray(sst_var[0, r0:r1, :])[:, ilon]
        else:
            sst_raw = np.asarray(sst_var[r0:r1, :])[:, ilon]

        scale  = float(getattr(sst_var, "scale_factor", 1.0))
        offset = float(getattr(sst_var, "add_offset",   0.0))
        fill   = int(getattr(sst_var, "_FillValue",   -32768))
        units  = str(getattr(sst_var, "units", "kelvin")).lower()
    finally:
        ds.close()

    sst_raw = np.array(sst_raw, dtype=np.int32)
    fill_mask = (sst_raw == fill)
    sst_phys = sst_raw.astype(np.float32) * scale + offset
    sst_c = np.ma.array(sst_phys, mask=fill_mask)

    if "kelvin" in units or (
        not sst_c.mask.all() and float(sst_c[~sst_c.mask].mean()) > 200
    ):
        sst_c = sst_c - 273.15

    m = re.search(r"(\d{8})", path.name)
    date_str = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]}" if m else path.stem

    valid = sst_c.compressed() if np.ma.is_masked(sst_c) else sst_c[np.isfinite(sst_c)]
    return SSTField(
        lon=np.array(lon_sub),
        lat=np.array(lat_sub),
        sst=sst_c,
        date=date_str,
        filename=path.name,
        shape=tuple(sst_c.shape),
        sst_min=float(valid.min()),
        sst_max=float(valid.max()),
        sst_mean=float(valid.mean()),
    )


# ── Cayula-Cornillon front detection (with cohesion check) ────────────────
def _cohesion(cls: np.ndarray) -> tuple:
    """
    Cayula-Cornillon cohesion: for each population, the fraction of
    valid neighbour pairs that stay within the same population.
    cls: 2-D int array, 0 / 1 for the two populations, -1 = invalid.
    Returns (C1, C2).
    """
    same0 = tot0 = same1 = tot1 = 0
    for a, b in (
        (cls[:, :-1], cls[:, 1:]),   # horizontal neighbours
        (cls[:-1, :], cls[1:, :]),   # vertical neighbours
    ):
        valid = (a >= 0) & (b >= 0)
        a_, b_ = a[valid], b[valid]
        m0 = (a_ == 0) | (b_ == 0)
        m1 = (a_ == 1) | (b_ == 1)
        tot0 += int(m0.sum());  same0 += int(((a_ == 0) & (b_ == 0)).sum())
        tot1 += int(m1.sum());  same1 += int(((a_ == 1) & (b_ == 1)).sum())
    c1 = same0 / tot0 if tot0 else 0.0
    c2 = same1 / tot1 if tot1 else 0.0
    return c1, c2


def cayula_cornillon(sst_2d, window=24, overlap=0.5, threshold=2.0, pct_edge=0.5,
                     cohesion_min=0.90):
    """
    Bimodal-histogram front detection with neighbourhood cohesion check
    (Cayula & Cornillon 1992).  Windows whose two temperature populations
    are not spatially coherent are rejected, which removes most noise.
    Front pixels are the class boundaries inside accepted windows.
    """
    rows, cols = sst_2d.shape
    step = max(1, int(window * (1 - overlap)))
    front_mask = np.zeros((rows, cols), dtype=np.float32)

    for r0 in range(0, rows - window, step):
        for c0 in range(0, cols - window, step):
            r1, c1 = r0 + window, c0 + window
            patch = sst_2d[r0:r1, c0:c1]
            finite = np.isfinite(patch)
            valid = patch[finite]
            if valid.size < window * window * pct_edge:
                continue
            if valid.std() < threshold:
                continue
            vmin, vmax = valid.min(), valid.max()
            if vmax - vmin < 0.5:
                continue

            # Otsu-style optimal threshold on the histogram
            hist, edges = np.histogram(valid, bins=32, density=True)
            total = hist.sum()
            best_var, best_t = 0.0, vmin
            w0 = mu0_sum = 0.0
            mu_total = (hist * (edges[:-1] + edges[1:]) / 2).sum()
            for i in range(1, len(hist)):
                w0 += hist[i - 1]
                mu0_sum += hist[i - 1] * (edges[i - 1] + edges[i]) / 2
                w1 = total - w0
                if w0 == 0 or w1 == 0:
                    continue
                mu0 = mu0_sum / w0
                mu1 = (mu_total - mu0_sum) / w1
                var = w0 * w1 * (mu0 - mu1) ** 2
                if var > best_var:
                    best_var = var
                    best_t = (edges[i - 1] + edges[i]) / 2

            # ── Cohesion check: both populations must be spatially coherent
            cls = np.full(patch.shape, -1, dtype=np.int8)
            cls[finite & (patch < best_t)] = 0
            cls[finite & (patch >= best_t)] = 1
            c1_, c2_ = _cohesion(cls)
            if c1_ < cohesion_min or c2_ < cohesion_min:
                continue   # incoherent bimodality → cloud/noise, reject window

            # ── Front pixels = class boundary (4-neighbour class change)
            boundary = np.zeros(patch.shape, dtype=bool)
            h = (cls[:, :-1] >= 0) & (cls[:, 1:] >= 0) & (cls[:, :-1] != cls[:, 1:])
            v = (cls[:-1, :] >= 0) & (cls[1:, :] >= 0) & (cls[:-1, :] != cls[1:, :])
            boundary[:, :-1] |= h
            boundary[:, 1:]  |= h
            boundary[:-1, :] |= v
            boundary[1:, :]  |= v

            if HAS_SCIPY:
                grad_r = ndimage.sobel(np.where(finite, patch, 0), axis=0)
                grad_c = ndimage.sobel(np.where(finite, patch, 0), axis=1)
                edge = np.hypot(grad_r, grad_c)
                ev = edge[finite]
                if ev.size > 0:
                    high_g = edge > ev.mean() + 0.5 * ev.std()
                    boundary &= high_g
            front_mask[r0:r1, c0:c1] += boundary.astype(np.float32)

    fm_max = front_mask.max()
    if fm_max > 0:
        front_mask /= fm_max
    return front_mask > 0.15


def remove_small_fronts(mask: np.ndarray, min_pixels: int = 12) -> np.ndarray:
    """Drop connected front components smaller than min_pixels (8-connected)."""
    if min_pixels <= 1 or not mask.any():
        return mask
    if HAS_SCIPY:
        structure = np.ones((3, 3), dtype=int)
        labels, n = ndimage.label(mask, structure=structure)
        if n == 0:
            return mask
        sizes = ndimage.sum(mask, labels, index=np.arange(1, n + 1))
        keep = np.zeros(n + 1, dtype=bool)
        keep[1:] = sizes >= min_pixels
        return keep[labels]
    # Pure-numpy fallback: BFS over front pixels only (8-connected)
    from collections import deque
    coords = set(zip(*np.where(mask)))
    out = np.zeros_like(mask, dtype=bool)
    while coords:
        seed = coords.pop()
        comp = [seed]
        q = deque([seed])
        while q:
            r, c = q.popleft()
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    nb = (r + dr, c + dc)
                    if nb in coords:
                        coords.discard(nb)
                        comp.append(nb)
                        q.append(nb)
        if len(comp) >= min_pixels:
            rr, cc = zip(*comp)
            out[list(rr), list(cc)] = True
    return out


def fronts_to_geojson(mask: np.ndarray, lat: np.ndarray, lon: np.ndarray,
                      min_vertices: int = 5) -> dict:
    """
    Vectorise the front mask into GeoJSON LineStrings (EPSG:4326)
    via marching-squares contouring of the binary mask.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(1, 1))
    try:
        cs = plt.contour(lon, lat, mask.astype(float), levels=[0.5])
        segs = cs.allsegs[0] if cs.allsegs else []
    finally:
        plt.close(fig)

    features = []
    for seg in segs:
        if len(seg) < min_vertices:
            continue
        coords = [[round(float(x), 4), round(float(y), 4)] for x, y in seg]
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {"n_vertices": len(coords)},
        })
    return {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }


def detect_fronts(sst_field: SSTField, log: Callable[[str], None]) -> np.ndarray:
    sst = np.array(sst_field.sst, dtype=float)
    if np.ma.is_masked(sst_field.sst):
        sst[sst_field.sst.mask] = np.nan

    rows, cols = sst.shape
    factor = max(1, max(rows, cols) // 500)
    if factor > 1:
        log(f"  ↳ 降採樣 1/{factor}")
        sst_ds = sst[::factor, ::factor]
    else:
        sst_ds = sst

    log(f"  ↳ 偵測網格：{sst_ds.shape}")
    fm_ds = cayula_cornillon(sst_ds, window=24, overlap=0.5)
    fm_ds = remove_small_fronts(fm_ds, min_pixels=12)

    if factor > 1 and HAS_SCIPY:
        from scipy.ndimage import zoom as ndz
        fm = ndz(fm_ds.astype(float), factor, order=0) > 0.5
        fm = fm[:rows, :cols]
    else:
        fm = fm_ds

    log(f"  ↳ 完成，front 像素：{int(fm.sum())}/{fm.size} ({fm.mean()*100:.1f}%)")
    return fm


# ── Coastline (one-time, cached) ───────────────────────────────────────────
_coastline_cache: Optional[list] = None


def get_coastlines() -> list:
    """Return simplified coastline polygons within the AOI as list of [[lon,lat],...]."""
    global _coastline_cache
    if _coastline_cache is not None:
        return _coastline_cache
    try:
        import cartopy.feature as cfeature
        from cartopy.io.shapereader import Reader
        feature = cfeature.NaturalEarthFeature("physical", "coastline", "50m")
        polys = []
        for geom in feature.geometries():
            if hasattr(geom, "geoms"):
                lines = list(geom.geoms)
            else:
                lines = [geom]
            for ln in lines:
                coords = list(ln.coords) if hasattr(ln, "coords") else []
                # Natural Earth is −180…180; convert to 0–360 so the
                # dateline-crossing AOI clips as one contiguous band.
                clipped = [
                    (round(x + 360.0 if x < 0 else x, 3), round(y, 3))
                    for x, y in coords
                    if (LON_MIN - 1 <= (x + 360.0 if x < 0 else x) <= LON_MAX + 1
                        and LAT_MIN - 1 <= y <= LAT_MAX + 1)
                ]
                if len(clipped) > 2:
                    polys.append(clipped)
        _coastline_cache = polys
        return polys
    except Exception:
        return []
