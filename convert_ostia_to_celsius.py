"""
OSTIA 原始資料（凱氏）→ 攝氏溫度轉換程式。

下載 SST（OSTIA）時，原始 analysed_sst（凱氏 K）會保存於
    mur_data/ostia_raw/OSTIA_SST_YYYYMMDD.nc
本程式讀取該原始檔，將溫度自動換算為攝氏（°C = K − 273.15），另存為
    ..._celsius.nc

用法：
    python convert_ostia_to_celsius.py                 # 轉換最新一個原始檔
    python convert_ostia_to_celsius.py 輸入.nc         # 指定輸入檔
    python convert_ostia_to_celsius.py 輸入.nc 輸出.nc  # 指定輸入與輸出
"""
import pathlib
import sys

import numpy as np
import netCDF4 as nc

RAW_DIR = pathlib.Path(__file__).resolve().parent / "mur_data" / "ostia_raw"
VAR = "analysed_sst"


def kelvin_to_celsius(arr):
    a = np.asarray(arr, dtype=np.float64)
    return np.where(np.isfinite(a), a - 273.15, np.nan)


def convert(in_path: pathlib.Path, out_path: pathlib.Path) -> pathlib.Path:
    src = nc.Dataset(str(in_path))
    try:
        latv = src.variables.get("latitude") or src.variables.get("lat")
        lonv = src.variables.get("longitude") or src.variables.get("lon")
        lat = np.array(latv[:], dtype=np.float64)
        lon = np.array(lonv[:], dtype=np.float64)
        var = src.variables.get(VAR) or next(
            v for n, v in src.variables.items()
            if v.ndim >= 2 and n not in ("latitude", "longitude", "lat", "lon"))
        arr = np.ma.filled(np.ma.masked_invalid(np.squeeze(var[:])), np.nan)
    finally:
        src.close()

    celsius = kelvin_to_celsius(arr)

    out = nc.Dataset(str(out_path), "w", format="NETCDF4")
    try:
        out.createDimension("lat", lat.size)
        out.createDimension("lon", lon.size)
        out.createVariable("latitude", "f8", ("lat",))[:] = lat
        out.createVariable("longitude", "f8", ("lon",))[:] = lon
        vv = out.createVariable("sst_celsius", "f4", ("lat", "lon"),
                                zlib=True, fill_value=np.float32(np.nan))
        vv[:] = celsius.astype(np.float32)
        vv.units = "degree_Celsius"
        vv.long_name = "Sea surface temperature (converted from OSTIA analysed_sst)"
        out.institution = "Fisheries Research Institute, MOA"
    finally:
        out.close()

    fin = celsius[np.isfinite(celsius)]
    rng = f"{fin.min():.2f} ~ {fin.max():.2f} °C" if fin.size else "無有效值"
    print(f"✅ 已轉換：{in_path.name} → {out_path.name}（值域 {rng}）")
    return out_path


def main():
    args = sys.argv[1:]
    if args:
        in_path = pathlib.Path(args[0])
    else:
        files = sorted(RAW_DIR.glob("OSTIA_SST_*.nc"))
        if not files:
            print(f"找不到原始檔，請先下載 SST（OSTIA）。目錄：{RAW_DIR}")
            return
        in_path = files[-1]
    out_path = (pathlib.Path(args[1]) if len(args) > 1
                else in_path.with_name(in_path.stem + "_celsius.nc"))
    convert(in_path, out_path)


if __name__ == "__main__":
    main()
