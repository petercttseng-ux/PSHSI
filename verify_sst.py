"""Verify final SST values match a known reference (should be ~10-30°C for West Pacific)."""
import pathlib, netCDF4 as nc, numpy as np

LAT_MIN, LAT_MAX = 17.0, 56.0
LON_MIN, LON_MAX = 114.0, 162.0

data_dir = pathlib.Path("mur_data")
nc_files = sorted(data_dir.glob("*.nc"), key=lambda p: p.stat().st_mtime, reverse=True)
if not nc_files:
    print("No .nc files found in mur_data/")
else:
    f = nc_files[0]
    print("File:", f.name)
    ds = nc.Dataset(str(f))

    lat = ds.variables["lat"][:]
    lon = ds.variables["lon"][:]
    ilat = np.where((lat >= LAT_MIN) & (lat <= LAT_MAX))[0]
    ilon = np.where((lon >= LON_MIN) & (lon <= LON_MAX))[0]
    print(f"Lat slice: {ilat[0]}..{ilat[-1]}  ({len(ilat)} pts) => {lat[ilat[0]]:.2f} to {lat[ilat[-1]]:.2f}")
    print(f"Lon slice: {ilon[0]}..{ilon[-1]}  ({len(ilon)} pts) => {lon[ilon[0]]:.2f} to {lon[ilon[-1]]:.2f}")

    # ── Read raw integer ──────────────────────────────────────────────────────
    ds.set_auto_maskandscale(False)
    sst_var = ds.variables["analysed_sst"]
    scale  = float(getattr(sst_var, "scale_factor", 1.0))
    offset = float(getattr(sst_var, "add_offset", 0.0))
    fill   = int(getattr(sst_var, "_FillValue", -32768))
    units  = str(getattr(sst_var, "units", "kelvin")).lower()
    print(f"scale={scale}, offset={offset}, fill={fill}, units={units}")

    sst_raw = sst_var[0, ilat[0]:ilat[-1]+1, ilon[0]:ilon[-1]+1]
    sst_raw = np.array(sst_raw, dtype=np.int32)
    fill_mask = (sst_raw == fill)
    sst_phys = sst_raw.astype(np.float32) * scale + offset
    sst_c = np.ma.array(sst_phys, mask=fill_mask)
    if "kelvin" in units or float(sst_c[~sst_c.mask].mean()) > 200:
        sst_c -= 273.15
    ds.close()

    valid = sst_c[~sst_c.mask]
    print(f"\n=== SST in °C (AOI: {LAT_MIN}-{LAT_MAX}N, {LON_MIN}-{LON_MAX}E) ===")
    print(f"  Min   : {valid.min():.2f} °C")
    print(f"  Max   : {valid.max():.2f} °C")
    print(f"  Mean  : {valid.mean():.2f} °C")
    print(f"  P2    : {np.percentile(valid, 2):.2f} °C")
    print(f"  P98   : {np.percentile(valid, 98):.2f} °C")
    print(f"  Shape : {sst_c.shape}")
    print(f"  Land% : {fill_mask.mean()*100:.1f}%")
    print("\n✅ Values look correct if min~5°C and max~30°C for West Pacific Feb")
