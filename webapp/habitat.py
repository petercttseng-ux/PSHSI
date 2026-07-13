"""
Habitat suitability (漁場預測) backend — ECDF / HSI method.

Species: 正鰹 Skipjack tuna (Katsuwonus pelamis)
         黃鰭鮪 Yellowfin tuna (Thunnus albacares)

Method
──────
For each species the historical CPUE logbook (1998–2007) gives, at every
fishing location, the sea-surface temperature (SST, °C), ocean colour /
chlorophyll-a (Chl-a, mg m⁻³), sea-surface height anomaly (SSHA, cm) and the
catch-per-unit-effort of the target species.

1.  Empirical Cumulative Distribution Function (ECDF).
    Using only presence records (CPUE > 0), we build a CPUE-**weighted** ECDF
    for each environmental variable.  Weighting by CPUE means the distribution
    reflects where the *fish were actually abundant*, not merely where boats
    happened to sample.  F(x) = Σ w_i · 1[x_i ≤ x] / Σ w_i.

2.  Optimal-habitat ranges.
    The weighted ECDF is summarised by percentiles:
        · optimal core   = p25 – p75  (inter-quartile, "最適")
        · suitable band  = p10 – p90  (10th–90th percentile, "適宜")

3.  Single-variable suitability score, s(x) ∈ [0, 1]  (trapezoidal SI curve).
    Using the ECDF percentiles p05/p25/p75/p95:
        s(x) = 1                              for p25 ≤ x ≤ p75   (optimal core)
        s(x) rises 0→1 linearly               for p05 ≤ x < p25
        s(x) falls 1→0 linearly               for p75 < x ≤ p95
        s(x) = 0                              outside [p05, p95]
    This is the classic fisheries habitat SI-curve: a flat optimal plateau
    over the inter-quartile catch range, tapering to zero at the tails — no
    distributional assumption, driven purely by the empirical CDF.

4.  Joint Habitat Suitability Index (HSI).
        HSI = ( s_SST · s_Chl · s_SSHA )^(1/3)
    The geometric mean enforces limiting-factor behaviour: if any single
    variable is unsuitable the whole cell is downgraded, as expected of a
    real habitat.  HSI is read as the *probability that a cell is good
    fishing ground* and is displayed in graded probability levels.

Marine Environmental Research, Fisheries Research Institute, MOA.
"""
from __future__ import annotations

import csv
import json
import pathlib
from typing import Optional

import numpy as np

# ── Locations ──────────────────────────────────────────────────────────────
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
PARAMS_FILE = PROJECT_ROOT / "habitat_params.json"

# Environmental variables used by the model and their CSV column names.
ENV_VARS = ["SST", "Chla", "SSHA"]

SPECIES = {
    "skipjack": {
        "cpue_col": "Skj_cp",
        "csv": "skipjack-chla-ssha-sst-1998to2007.csv",
        "name_zh": "正鰹", "name_en": "Skipjack tuna",
    },
    "yellowfin": {
        "cpue_col": "Yft_cp",
        "csv": "yellowfin-chla-ssha-sst-1998to2007.csv",
        "name_zh": "黃鰭鮪", "name_en": "Yellowfin tuna",
    },
}

# Percentile grid on which the weighted ECDF is stored (0,1,…,100).
_PCTL = np.arange(0, 101, dtype=np.float64)

# Probability-level thresholds for the prediction map legend.
PROB_LEVELS = [
    (0.75, "最適 (Highest)",  "#b91c1c"),
    (0.50, "高 (High)",       "#f97316"),
    (0.25, "中 (Moderate)",   "#facc15"),
    (0.05, "低 (Low)",        "#38bdf8"),
    (0.00, "不適 (Unsuitable)", "#1e3a5f"),
]


# ── Fitting the ECDF envelopes ─────────────────────────────────────────────
def _read_columns(csv_path: pathlib.Path) -> dict:
    """Read the CSV into {column: np.ndarray(float)} with whitespace-tolerant
    headers/values (the source files have padded fields)."""
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = [h.strip() for h in next(reader)]
        cols: dict = {h: [] for h in header}
        for row in reader:
            if not row or len(row) < len(header):
                continue
            for h, val in zip(header, row):
                try:
                    cols[h].append(float(str(val).strip()))
                except ValueError:
                    cols[h].append(np.nan)
    return {h: np.asarray(v, dtype=np.float64) for h, v in cols.items()}


def _weighted_ecdf_quantiles(values: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Return the value at each integer percentile 0…100 of the CPUE-weighted
    ECDF of `values`."""
    order = np.argsort(values, kind="stable")
    v = values[order]
    w = weights[order]
    cw = np.cumsum(w)
    cw = cw / cw[-1]                      # cumulative weight fraction ∈ (0,1]
    return np.interp(_PCTL / 100.0, cw, v).astype(np.float64)


def fit_species(species: str, project_root: pathlib.Path = PROJECT_ROOT) -> dict:
    """Fit CPUE-weighted ECDF envelopes for one species from its CSV."""
    spec = SPECIES[species]
    cols = _read_columns(project_root / spec["csv"])
    cpue = cols[spec["cpue_col"]]
    mask = np.isfinite(cpue) & (cpue > 0)
    for v in ENV_VARS:
        mask &= np.isfinite(cols[v])
    cpue = cpue[mask]
    out = {
        "name_zh": spec["name_zh"], "name_en": spec["name_en"],
        "cpue_col": spec["cpue_col"], "n": int(mask.sum()),
        "vars": {},
    }
    for v in ENV_VARS:
        x = cols[v][mask]
        q = _weighted_ecdf_quantiles(x, cpue)
        out["vars"][v] = {
            "quantiles": [round(float(z), 5) for z in q],  # value at pctl 0..100
            "optimal": [round(float(q[25]), 4), round(float(q[75]), 4)],
            "suitable": [round(float(q[10]), 4), round(float(q[90]), 4)],
            "median": round(float(q[50]), 4),
            "min": round(float(q[0]), 4), "max": round(float(q[100]), 4),
        }
    return out


def build_params(project_root: pathlib.Path = PROJECT_ROOT,
                 out_file: Optional[pathlib.Path] = None) -> dict:
    """Fit both species and write habitat_params.json."""
    params = {"method": "CPUE-weighted ECDF · HSI = geomean(s_SST,s_Chl,s_SSHA)",
              "region": {"lat": [-20, 20], "lon_0360": [130, 210],
                         "lon_label": "130°E–150°W"},
              "species": {}}
    for sp in SPECIES:
        params["species"][sp] = fit_species(sp, project_root)
    out_file = out_file or PARAMS_FILE
    out_file.write_text(json.dumps(params, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return params


# ── Runtime: load params & score grids ─────────────────────────────────────
_params_cache: Optional[dict] = None


def load_params(force: bool = False) -> dict:
    global _params_cache
    if _params_cache is not None and not force:
        return _params_cache
    if not PARAMS_FILE.exists():
        _params_cache = build_params()
    else:
        _params_cache = json.loads(PARAMS_FILE.read_text(encoding="utf-8"))
    return _params_cache


def _suitability(x: np.ndarray, quantiles: list) -> np.ndarray:
    """Trapezoidal ECDF suitability s(x) ∈ [0,1] with a flat optimal plateau
    over p25–p75, tapering to 0 at p05/p95. NaN in → NaN out."""
    q = np.asarray(quantiles, dtype=np.float64)
    p05, p25, p75, p95 = q[5], q[25], q[75], q[95]
    x = np.asarray(x, dtype=np.float64)
    s = np.zeros_like(x, dtype=np.float64)
    # optimal plateau
    s = np.where((x >= p25) & (x <= p75), 1.0, s)
    # rising limb p05→p25
    if p25 > p05:
        rise = (x >= p05) & (x < p25)
        s = np.where(rise, (x - p05) / (p25 - p05), s)
    # falling limb p75→p95
    if p95 > p75:
        fall = (x > p75) & (x <= p95)
        s = np.where(fall, (p95 - x) / (p95 - p75), s)
    s = np.clip(s, 0.0, 1.0)
    s = np.where(np.isfinite(x), s, np.nan)
    return s


def predict_grid(species: str, sst: np.ndarray, chl: np.ndarray,
                 ssha: np.ndarray, params: Optional[dict] = None) -> dict:
    """
    Compute the HSI probability grid for one species on aligned SST / Chl /
    SSHA grids (same shape). Returns {hsi, s_sst, s_chl, s_ssha} as 2-D arrays
    with NaN where any input is missing.
    """
    params = params or load_params()
    sv = params["species"][species]["vars"]
    s_sst = _suitability(np.asarray(sst, float), sv["SST"]["quantiles"])
    s_chl = _suitability(np.asarray(chl, float), sv["Chla"]["quantiles"])
    s_ssh = _suitability(np.asarray(ssha, float), sv["SSHA"]["quantiles"])
    with np.errstate(invalid="ignore"):
        hsi = np.cbrt(s_sst * s_chl * s_ssh)   # geometric mean of three scores
    valid = np.isfinite(s_sst) & np.isfinite(s_chl) & np.isfinite(s_ssh)
    hsi = np.where(valid, hsi, np.nan)
    return {"hsi": hsi, "s_sst": s_sst, "s_chl": s_chl, "s_ssha": s_ssh}


# ── Regridding onto a common prediction grid ───────────────────────────────
def target_grid(step: float = 0.25):
    """Regular 0–360 lat/lon grid covering the AOI (default 0.25°)."""
    from sst_processor import LAT_MIN, LAT_MAX, LON_MIN, LON_MAX
    lat = np.arange(LAT_MIN, LAT_MAX + step / 2, step)
    lon = np.arange(LON_MIN, LON_MAX + step / 2, step)   # 0–360
    return lat, lon


def regrid_nearest(src_lat, src_lon, src_arr, tgt_lat, tgt_lon):
    """Nearest-neighbour resample src_arr (on src_lat/src_lon, any lon
    convention) onto the 0–360 target grid. NaN preserved."""
    src_lat = np.asarray(src_lat, float)
    src_lon = np.where(np.asarray(src_lon, float) < 0,
                       np.asarray(src_lon, float) + 360.0, np.asarray(src_lon, float))
    order = np.argsort(src_lon)
    src_lon = src_lon[order]
    arr = np.asarray(src_arr, float)[:, order]
    if src_lat[0] > src_lat[-1]:
        src_lat = src_lat[::-1]; arr = arr[::-1, :]
    ii = np.clip(np.searchsorted(src_lat, tgt_lat), 0, src_lat.size - 1)
    jj = np.clip(np.searchsorted(src_lon, tgt_lon), 0, src_lon.size - 1)
    return arr[np.ix_(ii, jj)]


def prob_level(hsi_value: float) -> Optional[str]:
    if hsi_value is None or not np.isfinite(hsi_value):
        return None
    for thr, label, _ in PROB_LEVELS:
        if hsi_value >= thr:
            return label
    return PROB_LEVELS[-1][1]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Fit ECDF habitat params.")
    ap.add_argument("--build", action="store_true", help="rebuild habitat_params.json")
    args = ap.parse_args()
    p = build_params() if args.build else load_params()
    for sp, d in p["species"].items():
        print(f"\n{sp} ({d['name_zh']} / {d['name_en']}) — n={d['n']} presence records")
        for v in ENV_VARS:
            vv = d["vars"][v]
            print(f"  {v:5s} optimal(p25–p75)={vv['optimal']}  "
                  f"suitable(p10–p90)={vv['suitable']}  median={vv['median']}")
