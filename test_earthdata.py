"""
Earthdata OPeNDAP self-test — run on the institute machine (which can reach
NASA Earthdata) to validate that SST / Chl-a / SSHA download & subsetting work.

    python test_earthdata.py

It prints, for each variable: token status, the resolved granule date, the
OPeNDAP base URL, the subset grid shape and value range — or the exact error.
If a variable returns 0 granules, adjust its `short_name` in webapp/earthdata.py
(candidates are listed there for ocean colour) and re-run.
"""
import datetime
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "webapp"))

import numpy as np
import nasa_auth
import earthdata as ed


def main():
    st = nasa_auth.token_status()
    print("=" * 64)
    print("Earthdata token:", st["message"])
    print("=" * 64)

    today = datetime.date.today().isoformat()
    stride = {"sst": 8, "chl": 2, "ssh": 1}   # keep MUR (1 km) manageable

    for key in ("sst", "chl", "ssh"):
        cfg = ed.DATASETS[key]
        print(f"\n[{key.upper()}]  short_name={cfg['short_name']}  "
              f"var={cfg['src_var']}  fallback={cfg['fallback_days']}d")
        try:
            base, gdate = ed.find_granule(key, today, print)
            if base is None:
                print("   ✗ CMR 找不到 granule（請檢查 short_name / 網路 / token）")
                continue
            print(f"   granule 日期 {gdate}")
            print(f"   OPeNDAP: {base}")
            lat, lon, arr, actual = ed.fetch_region_day(
                key, today, print, stride=stride[key])
            if arr is None:
                print("   ✗ 取得 granule 但子集擷取失敗")
                continue
            fin = arr[np.isfinite(arr)]
            print(f"   ✓ 子集 {arr.shape[0]}×{arr.shape[1]} 格｜"
                  f"lat {lat.min():.2f}~{lat.max():.2f}｜"
                  f"lon(0-360) {lon.min():.2f}~{lon.max():.2f}｜"
                  f"值域 {fin.min():.3f}~{fin.max():.3f}（有效 {fin.size} 格）")
        except Exception as e:
            import traceback
            print(f"   ✗ 例外：{e}")
            traceback.print_exc()

    print("\n完成。三個變數皆顯示「✓ 子集…」即代表 Earthdata 管線可用。")


if __name__ == "__main__":
    main()
