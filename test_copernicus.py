"""
Copernicus Marine self-test — run on the institute machine to validate that
SST / Chl-a / SSHA download & subsetting work via the copernicusmarine toolbox.

Prerequisites (once):
    pip install copernicusmarine
    copernicusmarine login        # enter your Copernicus Marine account

Then:
    python test_copernicus.py [YYYY-MM-DD]

It prints, for each variable, the subset grid shape and value range — or the
exact error. If a variable fails with a dataset-not-found error, adjust its
`dataset_id` in webapp/copernicus.py and re-run.
"""
import datetime
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "webapp"))

import numpy as np
import copernicus as cop


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else \
        (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    print("=" * 64)
    print(f"Copernicus Marine self-test — 日期 {date}")
    print("（若提示未登入，請先執行： copernicusmarine login）")
    print("=" * 64)

    labels = {"mur": "SST OSTIA", "chl": "Chl-a GlobColour", "ssh": "SSHA SLA"}
    for key in ("mur", "chl", "ssh"):
        cfg = cop.DATASETS[key]
        print(f"\n[{labels[key]}]  dataset_id={cfg['dataset_id']}  var={cfg['var']}")
        try:
            lat, lon, arr, actual = cop.fetch_region_day(key, date, print)
            if arr is None:
                print("   ✗ 無輸出（該日資料可能尚未發布，試更早日期）")
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

    print("\n完成。三個變數皆顯示「✓ 子集…」即代表 Copernicus 管線可用。")


if __name__ == "__main__":
    main()
