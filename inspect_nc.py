import pathlib
import netCDF4 as nc
import numpy as np

data_dir = pathlib.Path("mur_data")
nc_files = sorted(data_dir.glob("*.nc"), key=lambda p: p.stat().st_mtime, reverse=True)
if not nc_files:
    print("No .nc files found in mur_data/")
else:
    f = nc_files[0]
    print("File:", f.name)

    # ── Auto-decoded view ──────────────────────────────────────────────────
    ds = nc.Dataset(str(f))
    v = ds.variables["analysed_sst"]
    print("dtype       :", v.dtype)
    print("dimensions  :", v.dimensions)
    print("shape       :", v.shape)
    print("scale_factor:", getattr(v, "scale_factor", "NOT SET"))
    print("add_offset  :", getattr(v, "add_offset",   "NOT SET"))
    print("_FillValue  :", getattr(v, "_FillValue",   "NOT SET"))
    print("missing_val :", getattr(v, "missing_value", "NOT SET"))
    print("units       :", getattr(v, "units",         "NOT SET"))
    print("valid_min   :", getattr(v, "valid_min",     "NOT SET"))
    print("valid_max   :", getattr(v, "valid_max",     "NOT SET"))

    # auto-decoded sample (masked array, possibly already in K or C)
    dec_sample = v[0, 3800:3805, 3200:3205]
    print("auto-decoded sample (raw netCDF4 apply scale+offset):")
    print(np.array(dec_sample))

    # ── Raw stored integers ────────────────────────────────────────────────
    ds2 = nc.Dataset(str(f))
    ds2.set_auto_maskandscale(False)
    vraw = ds2.variables["analysed_sst"]
    raw = vraw[0, 3800:3805, 3200:3205]
    print("raw int16 sample (no scaling):")
    print(np.array(raw))
    ds2.close()
    ds.close()
