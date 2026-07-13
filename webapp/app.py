"""
GHRSST MUR SST Web Application
Flask backend serving the professional web UI for the
Marine Environmental Research Lab, Fisheries Research Institute, MOA.
"""
from __future__ import annotations

import datetime
import io
import pathlib
import sys
import threading
import traceback
import webbrowser
from collections import deque

# Make stdout UTF-8 so emoji-laden log lines print on Windows cp950 consoles.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
from flask import (
    Flask, jsonify, request, send_from_directory, render_template, abort
)
from werkzeug.utils import secure_filename

import sst_processor as sp
import timeseries as ts
import habitat as hb

APP_ROOT = pathlib.Path(__file__).resolve().parent
app = Flask(
    __name__,
    template_folder=str(APP_ROOT / "templates"),
    static_folder=str(APP_ROOT / "static"),
)
# Reject uploads larger than a full global MUR granule (~1 GB) + margin.
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024


# ── In-memory state ────────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.field: sp.SSTField | None = None
        self.fronts: np.ndarray | None = None
        self.logs: deque[str] = deque(maxlen=500)
        self.download_status = {
            "active": False,
            "progress": 0.0,
            "message": "",
            "completed": False,
            "error": None,
            "filename": None,
        }
        self.fronts_status = {
            "active": False,
            "completed": False,
            "error": None,
        }
        self.series: ts.SeriesStack | None = None
        self.series_status = {
            "active": False, "progress": 0.0, "message": "",
            "completed": False, "error": None,
        }


state = State()


def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with state.lock:
        state.logs.append(line)
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # Windows cp950 console can't print emoji; strip them.
        safe = line.encode("ascii", "replace").decode("ascii")
        print(safe, flush=True)


# ── Startup credential check ───────────────────────────────────────────────
def _check_token_at_startup():
    st = sp.check_credentials()
    icon = "🔑" if st["valid"] else "⚠️"
    log(f"{icon} {st['message']}")
    import nasa_auth
    if nasa_auth.TRUSTSTORE_ACTIVE:
        log("🔒 已啟用 Windows 系統憑證庫（truststore）")
    else:
        log("⚠️ 未安裝 truststore — 機關網路下 NASA 下載可能出現 SSL 錯誤，"
            "請執行：pip install truststore")


# ── Auto-load latest local file at startup ─────────────────────────────────
def _autoload():
    import pathlib as _pl
    files = sorted(sp.DATA_DIR.glob("*.nc"),
                   key=lambda q: q.stat().st_mtime, reverse=True)
    for cand in files:
        try:
            log(f"📂 自動載入：{cand.name}")
            field = sp.load_nc_file(cand)
            with state.lock:
                state.field = field
                state.fronts = None
            log(f"✅ 載入完成 — {field.date} | shape={field.shape} | "
                f"T={field.sst_min:.1f} ~ {field.sst_max:.1f} °C")
            return
        except Exception as e:
            log(f"⚠️ {cand.name} 載入失敗（{e}），嘗試下一個檔案")
    if files:
        log("❌ 所有本機檔案皆無法載入")


# ── API routes ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/token_status")
def api_token_status():
    return jsonify(sp.check_credentials())


@app.route("/api/status")
def api_status():
    with state.lock:
        if state.field is None:
            return jsonify({"loaded": False})
        f = state.field
        return jsonify({
            "loaded": True,
            "filename": f.filename,
            "date": f.date,
            "shape": list(f.shape),
            "stats": {
                "min": round(f.sst_min, 2),
                "max": round(f.sst_max, 2),
                "mean": round(f.sst_mean, 2),
            },
            "fronts_available": state.fronts is not None,
        })


@app.route("/api/files")
def api_files():
    return jsonify({"files": sp.list_local_files()})


@app.route("/api/load", methods=["POST"])
def api_load():
    body = request.get_json(silent=True) or {}
    fname = body.get("filename")
    if not fname:
        return jsonify({"ok": False, "error": "缺少 filename 參數"}), 400
    path = sp.DATA_DIR / fname
    if not path.exists():
        return jsonify({"ok": False, "error": f"檔案不存在：{fname}"}), 404
    try:
        log(f"📂 載入：{fname}")
        field = sp.load_nc_file(path)
        with state.lock:
            state.field = field
            state.fronts = None
        log(f"✅ 載入完成 — {field.date} | shape={field.shape} | "
            f"T={field.sst_min:.1f} ~ {field.sst_max:.1f} °C")
        return jsonify({"ok": True})
    except Exception as e:
        log(f"❌ 載入失敗：{e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/sst")
def api_sst():
    """Return the current SST grid as JSON for plotting."""
    max_points = int(request.args.get("max_points", 220_000))
    with state.lock:
        if state.field is None:
            return jsonify({"loaded": False})
        payload = state.field.to_payload(max_points=max_points)
    return jsonify({"loaded": True, **payload})


@app.route("/api/fronts", methods=["GET", "POST"])
def api_fronts():
    if request.method == "GET":
        with state.lock:
            if state.fronts is None or state.field is None:
                return jsonify({"available": False, **state.fronts_status})
            mask = state.fronts
            field = state.field

        # Reduce to a list of (lon, lat) pixel centers — much smaller than full grid
        rows = np.where(mask)
        if rows[0].size == 0:
            return jsonify({"available": True, "points": [], **state.fronts_status})

        # Limit number of returned points (sub-sample if too many)
        idxs = np.arange(rows[0].size)
        max_points = 25_000
        if idxs.size > max_points:
            idxs = np.linspace(0, idxs.size - 1, max_points).astype(int)
        ri, ci = rows[0][idxs], rows[1][idxs]
        lons = field.lon[ci].astype(float).tolist()
        lats = field.lat[ri].astype(float).tolist()
        return jsonify({
            "available": True,
            "lons": [round(v, 4) for v in lons],
            "lats": [round(v, 4) for v in lats],
            "count": int(mask.sum()),
            **state.fronts_status,
        })

    # POST → start detection
    with state.lock:
        if state.field is None:
            return jsonify({"ok": False, "error": "尚未載入資料"}), 400
        if state.fronts_status.get("active"):
            return jsonify({"ok": False, "error": "偵測進行中"}), 409
        state.fronts_status = {"active": True, "completed": False, "error": None}

    def worker():
        try:
            log("🔍 開始 Cayula-Cornillon 偵測…")
            mask = sp.detect_fronts(state.field, log)
            with state.lock:
                state.fronts = mask
                state.fronts_status = {"active": False, "completed": True, "error": None}
            log("✅ 海洋前緣偵測完成")
        except Exception as e:
            log(f"❌ 偵測失敗：{e}")
            log(traceback.format_exc())
            with state.lock:
                state.fronts_status = {"active": False, "completed": False, "error": str(e)}

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/download", methods=["POST"])
def api_download():
    cred = sp.check_credentials()
    if not cred["valid"]:
        log(f"⚠️ {cred['message']}")
        return jsonify({"ok": False, "error": cred["message"]}), 401
    with state.lock:
        if state.download_status["active"]:
            return jsonify({"ok": False, "error": "下載進行中"}), 409
        state.download_status = {
            "active": True, "progress": 0.0,
            "message": "啟動下載", "completed": False,
            "error": None, "filename": None,
        }

    def worker():
        try:
            sp.setup_netrc()
            url, fname = sp.fetch_latest_granule_url(log)
            if not url:
                raise RuntimeError("無法取得最新 granule 連結")

            with state.lock:
                state.download_status["filename"] = fname
                state.download_status["message"] = f"下載 {fname}"

            dest = sp.DATA_DIR / fname
            field = None
            if dest.exists():
                log(f"ℹ️  已存在，驗證後載入：{fname}")
                try:
                    field = sp.load_nc_file(dest)
                    with state.lock:
                        state.download_status["progress"] = 100.0
                except Exception as e:
                    log(f"⚠️ 既有檔案損毀（{e}），刪除後重新下載")
                    dest.unlink()

            if field is None:
                def prog(pct):
                    with state.lock:
                        state.download_status["progress"] = round(pct, 1)
                sp.download_file(url, dest, log, prog)
                field = sp.load_nc_file(dest)
            with state.lock:
                state.field = field
                state.fronts = None
                state.download_status.update({
                    "active": False,
                    "completed": True,
                    "progress": 100.0,
                    "message": "完成",
                })
            log(f"✅ 載入：{field.filename} | T={field.sst_min:.1f} ~ {field.sst_max:.1f} °C")
        except Exception as e:
            log(f"❌ 下載/載入失敗：{e}")
            log("💡 請確認 NASA Earthdata 帳號已取得 PODAAC 授權")
            with state.lock:
                state.download_status.update({
                    "active": False, "completed": False,
                    "error": str(e), "message": "失敗",
                })

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/download/status")
def api_download_status():
    with state.lock:
        return jsonify(dict(state.download_status))


@app.route("/api/logs")
def api_logs():
    after = int(request.args.get("after", 0))
    with state.lock:
        all_logs = list(state.logs)
    sliced = all_logs[after:]
    return jsonify({"logs": sliced, "total": len(all_logs)})


@app.route("/api/coastline")
def api_coastline():
    polys = sp.get_coastlines()
    return jsonify({"polygons": polys})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "未提供檔案"}), 400
    f = request.files["file"]
    if not f.filename or not f.filename.lower().endswith((".nc", ".nc4")):
        return jsonify({"ok": False, "error": "請上傳 .nc 檔"}), 400
    safe_name = secure_filename(pathlib.Path(f.filename).name)
    if not safe_name or not safe_name.lower().endswith((".nc", ".nc4")):
        return jsonify({"ok": False, "error": "檔名無效"}), 400
    dest = (sp.DATA_DIR / safe_name).resolve()
    if sp.DATA_DIR.resolve() not in dest.parents:
        return jsonify({"ok": False, "error": "檔名無效"}), 400
    f.save(str(dest))
    log(f"📤 上傳：{safe_name}")
    try:
        field = sp.load_nc_file(dest)
        with state.lock:
            state.field = field
            state.fronts = None
        log(f"✅ 載入：{safe_name} | T={field.sst_min:.1f} ~ {field.sst_max:.1f} °C")
        return jsonify({"ok": True, "filename": safe_name})
    except Exception as e:
        log(f"❌ 載入失敗：{e}")
        return jsonify({"ok": False, "error": str(e)}), 500



# ── Time series / animation ────────────────────────────────────────────────
@app.route("/api/series/load", methods=["POST"])
def api_series_load():
    body = request.get_json(silent=True) or {}
    start, end = body.get("start"), body.get("end")
    dataset = body.get("dataset", "mur")
    if dataset not in ts.DATASETS:
        return jsonify({"ok": False, "error": f"未知資料集：{dataset}"}), 400
    stride = int(body.get("stride", ts.DATASETS[dataset]["default_stride"]))
    if not start or not end:
        return jsonify({"ok": False, "error": "缺少 start / end 日期"}), 400
    try:
        ts.date_range(start, end)  # validates span
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    with state.lock:
        if state.series_status["active"]:
            return jsonify({"ok": False, "error": "序列下載進行中"}), 409
        state.series_status = {"active": True, "progress": 0.0,
                               "message": "啟動", "completed": False, "error": None}

    def prog(pct, msg):
        with state.lock:
            state.series_status["progress"] = round(pct, 1)
            state.series_status["message"] = msg

    def worker():
        try:
            log(f"🎞 建立時間序列 {start} ~ {end}"
                f"（{ts.DATASETS[dataset]['name']}, stride {stride}）…")
            stack = ts.build_stack(start, end, stride, log, prog, dataset=dataset)
            with state.lock:
                state.series = stack
                state.series_status.update(
                    {"active": False, "completed": True, "progress": 100.0})
        except Exception as e:
            log(f"❌ 時間序列失敗：{e}")
            with state.lock:
                state.series_status.update(
                    {"active": False, "completed": False, "error": str(e)})

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/series/status")
def api_series_status():
    with state.lock:
        st = dict(state.series_status)
        st["nframes"] = state.series.nframes if state.series else 0
    return jsonify(st)


@app.route("/api/series/meta")
def api_series_meta():
    with state.lock:
        if state.series is None:
            return jsonify({"available": False})
        stk = state.series
        return jsonify({
            "available": True,
            "dataset": stk.dataset,
            "kind": ts.DATASETS.get(stk.dataset, {}).get("kind", "sst"),
            "dates": stk.dates,
            "shape": [int(stk.lat.size), int(stk.lon.size)],
            "stats": [stk.frame_stats(i) for i in range(stk.nframes)],
        })


@app.route("/api/series/frame")
def api_series_frame():
    i = int(request.args.get("index", 0))
    anomaly = request.args.get("anomaly", "0") == "1"
    baseline = int(request.args.get("baseline", 30))
    with state.lock:
        stk = state.series
    if stk is None:
        return jsonify({"error": "尚未載入時間序列"}), 400
    if not (0 <= i < stk.nframes):
        return jsonify({"error": "index 超出範圍"}), 400
    try:
        return jsonify(ts.frame_payload(stk, i, anomaly, baseline))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/series/export.gif")
def api_series_export():
    anomaly = request.args.get("anomaly", "0") == "1"
    baseline = int(request.args.get("baseline", 30))
    fps = max(1, min(12, int(request.args.get("fps", 4))))
    with state.lock:
        stk = state.series
    if stk is None:
        return jsonify({"error": "尚未載入時間序列"}), 400
    kind = "anomaly" if anomaly else "sst"
    out = ts.TS_DIR / f"MUR_{kind}_{stk.dates[0]}_{stk.dates[-1]}.gif"
    try:
        ts.export_gif(stk, out, anomaly=anomaly, baseline=baseline, fps=fps, log=log)
    except ImportError:
        return jsonify({"error": "GIF 匯出需安裝 matplotlib 與 pillow"}), 500
    return send_from_directory(str(out.parent), out.name, as_attachment=True)


# ── Stations (fixed-point monitoring) ──────────────────────────────────────
@app.route("/api/stations", methods=["GET", "POST"])
def api_stations():
    if request.method == "GET":
        return jsonify({"stations": ts.load_stations()})
    body = request.get_json(silent=True) or {}
    try:
        st = ts.add_station(
            body.get("name", ""),
            float(body["lat"]), float(body["lon"]),
            body.get("t_high"), body.get("t_low"),
        )
        log(f"📍 新增測站：{st['name']}（{st['lat']}N, {st['lon']}E）")
        return jsonify({"ok": True, "station": st})
    except (KeyError, TypeError):
        return jsonify({"ok": False, "error": "缺少 lat / lon"}), 400
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/stations/<sid>", methods=["DELETE"])
def api_station_delete(sid):
    if ts.remove_station(sid):
        log(f"🗑 刪除測站 {sid}")
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "測站不存在"}), 404


@app.route("/api/stations/<sid>/series")
def api_station_series(sid):
    days = max(7, min(366, int(request.args.get("days", 60))))
    station = next((s for s in ts.load_stations() if s["id"] == sid), None)
    if station is None:
        return jsonify({"error": "測站不存在"}), 404
    try:
        series = ts.fetch_point_series(station["lat"], station["lon"], days)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    alert = ts.evaluate_alerts(station, series)
    return jsonify({"station": station, **series, **alert})



# ── Transect / GeoJSON / Export / Datasets ─────────────────────────────────
@app.route("/api/datasets")
def api_datasets():
    return jsonify({
        "datasets": [
            {"key": k, "name": v["name"], "kind": v["kind"],
             "default_stride": v["default_stride"]}
            for k, v in ts.DATASETS.items()
        ]
    })


def _current_grid(source: str, index: int):
    """Return (lat, lon, grid2d, label) for 'field' or 'series' source."""
    with state.lock:
        if source == "series" and state.series is not None:
            stk = state.series
            i = max(0, min(stk.nframes - 1, index))
            return stk.lat, stk.lon, stk.data[i], f"{stk.dataset} {stk.dates[i]}"
        if state.field is None:
            return None, None, None, None
        f = state.field
        grid = np.array(f.sst, dtype=np.float32)
        if np.ma.is_masked(f.sst):
            grid = np.where(f.sst.mask, np.nan, grid)
        return f.lat, f.lon, grid, f.date


@app.route("/api/transect", methods=["POST"])
def api_transect():
    body = request.get_json(silent=True) or {}
    try:
        lat1, lon1 = float(body["lat1"]), float(body["lon1"])
        lat2, lon2 = float(body["lat2"]), float(body["lon2"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "缺少端點座標"}), 400
    source = body.get("source", "field")
    index = int(body.get("index", 0))
    lat, lon, grid, label = _current_grid(source, index)
    if grid is None:
        return jsonify({"error": "尚未載入資料"}), 400
    res = ts.transect(lat, lon, grid, lat1, lon1, lat2, lon2,
                      n=int(body.get("n", 300)))
    res["label"] = label
    log(f"📏 剖面：({lat1:.2f}N,{lon1:.2f}E)→({lat2:.2f}N,{lon2:.2f}E) "
        f"{res['total_km']} km，最大梯度 {res['max_grad']} °C/km")
    return jsonify(res)


@app.route("/api/fronts/geojson")
def api_fronts_geojson():
    with state.lock:
        if state.fronts is None or state.field is None:
            return jsonify({"error": "尚未執行前緣偵測"}), 400
        mask = state.fronts
        f = state.field
    try:
        gj = sp.fronts_to_geojson(mask, f.lat, f.lon)
    except ImportError:
        return jsonify({"error": "向量化需安裝 matplotlib"}), 500
    from flask import Response
    import json as _json
    fname = f"fronts_{f.date}.geojson"
    log(f"🗺 前緣 GeoJSON 匯出：{len(gj['features'])} 條線段")
    return Response(
        _json.dumps(gj),
        mimetype="application/geo+json",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


@app.route("/api/export")
def api_export():
    fmt = request.args.get("fmt", "csv")
    source = request.args.get("source", "field")
    index = int(request.args.get("index", 0))
    lat, lon, grid, label = _current_grid(source, index)
    if grid is None:
        return jsonify({"error": "尚未載入資料"}), 400
    try:
        lat0 = float(request.args.get("lat0", lat.min()))
        lat1 = float(request.args.get("lat1", lat.max()))
        lon0 = float(request.args.get("lon0", lon.min()))
        lon1 = float(request.args.get("lon1", lon.max()))
        cla, clo, cg = ts._crop(lat, lon, grid, lat0, lat1, lon0, lon1)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    safe_label = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(label))
    base = f"SST_{safe_label}_{lat0:.1f}N-{lat1:.1f}N_{lon0:.1f}E-{lon1:.1f}E"
    try:
        if fmt == "csv":
            out = ts.EXPORT_DIR / f"{base}.csv"
            _, stride = ts.export_csv(cla, clo, cg, out)
            if stride > 1:
                log(f"ℹ️ CSV 過大，已降採樣 1/{stride}")
        elif fmt == "nc":
            out = ts.EXPORT_DIR / f"{base}.nc"
            ts.export_netcdf(cla, clo, cg, out, title=f"SST subset {label}")
        elif fmt == "tif":
            out = ts.EXPORT_DIR / f"{base}.tif"
            ts.export_geotiff(cla, clo, cg, out)
        else:
            return jsonify({"error": f"未知格式：{fmt}"}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    log(f"💾 匯出 {fmt.upper()}：{out.name}（{cg.shape[0]}×{cg.shape[1]}）")
    return send_from_directory(str(out.parent), out.name, as_attachment=True)


@app.route("/api/overlay/chl")
def api_overlay_chl():
    """單日葉綠素子集（供 SST×Chl 疊圖等值線）。"""
    date = request.args.get("date")
    if not date:
        return jsonify({"error": "缺少 date"}), 400
    stride = int(request.args.get("stride", ts.DATASETS["chl"]["default_stride"]))
    max_points = max(200, min(12000, int(request.args.get("max_points", 4000))))
    threshold = float(request.args.get("threshold", ts.CHL_MIN))
    p, actual = ts.fetch_day_fallback(date, stride, log, dataset="chl")
    if p is None:
        fb = ts.DATASETS["chl"].get("fallback_days", 7)
        return jsonify({"error": f"{date} 起往前 {fb} 天皆無水色資料"}), 502
    try:
        la, lo, arr = ts.load_day(p, var=ts.DATASETS["chl"]["var"])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    payload = ts.chl_payload(la, lo, arr, max_points=max_points, threshold=threshold)
    log(f"🟢 水色點：{actual}，{payload['count']} 點 ≥{threshold} mg/m³"
        f"（{payload['min']}–{payload['max']}）")
    return jsonify({"date": actual, "requested": date, **payload})


@app.route("/api/overlay/currents")
def api_overlay_currents():
    """單日表面地轉流向量（供箭頭疊圖）。"""
    date = request.args.get("date")
    if not date:
        return jsonify({"error": "缺少 date"}), 400
    stride = int(request.args.get("stride", 1))
    max_arrows = max(100, min(10000, int(request.args.get("max_arrows", 3600))))
    p, actual = ts.fetch_day_fallback(date, stride, log, dataset="currents")
    if p is None:
        return jsonify({"error": f"{date} 起往前 7 天皆無海流資料"}), 502
    try:
        la, lo, u, v = ts.load_currents(p)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    payload = ts.currents_payload(la, lo, u, v, max_arrows=max_arrows)
    log(f"🌀 海流向量：{actual}，{len(payload['lons'])} 支箭頭"
        f"（最大流速 {payload['max_speed']} m/s）")
    return jsonify({"date": actual, "requested": date, **payload})


# ── Habitat / fishing-ground prediction (漁場預測 · ECDF-HSI) ───────────────
@app.route("/api/habitat/params")
def api_habitat_params():
    """ECDF-derived optimal environmental ranges + probability-level legend."""
    p = hb.load_params()
    return jsonify({
        "method": p.get("method"),
        "region": p.get("region"),
        "species": {
            k: {
                "name_zh": v["name_zh"], "name_en": v["name_en"], "n": v["n"],
                "ranges": {vv: {"optimal": v["vars"][vv]["optimal"],
                                "suitable": v["vars"][vv]["suitable"],
                                "median": v["vars"][vv]["median"]}
                           for vv in hb.ENV_VARS},
            } for k, v in p["species"].items()
        },
        "prob_levels": [{"min": t, "label": lab, "color": col}
                        for t, lab, col in hb.PROB_LEVELS],
    })


def _fetch_env_grid(date, dataset):
    """Fetch one environmental field for `date` (with fallback) → (lat, lon,
    arr, actual_date) or (None, None, None, None)."""
    stride = ts.DATASETS[dataset]["default_stride"]
    p, actual = ts.fetch_day_fallback(date, stride, log, dataset=dataset)
    if p is None:
        return None, None, None, None
    la, lo, arr = ts.load_day(p, var=ts.DATASETS[dataset]["var"])
    return la, lo, arr, actual


@app.route("/api/habitat/predict", methods=["POST"])
def api_habitat_predict():
    """One-click fishing-ground prediction for a species on a chosen date.

    Fetches SST (MUR), Chl-a (MODIS) and SSHA (altimetry SLA) over the AOI,
    resamples onto a common 0.25° grid and returns the ECDF-HSI probability
    field for skipjack or yellowfin tuna.
    """
    body = request.get_json(silent=True) or {}
    species = body.get("species")
    date = body.get("date")
    if species not in hb.SPECIES:
        return jsonify({"ok": False, "error": f"未知魚種：{species}"}), 400
    if not date:
        return jsonify({"ok": False, "error": "缺少 date 參數"}), 400
    params = hb.load_params()
    zh = params["species"][species]["name_zh"]
    try:
        log(f"🎯 {zh}漁場預測：擷取 {date} 之 SST / Chl-a / SSHA …")
        sla, slo, ssta, sst_date = _fetch_env_grid(date, "mur")
        if ssta is None:
            return jsonify({"ok": False, "error": f"{date} 起無可用 SST 資料"}), 502
        cla, clo, chla, chl_date = _fetch_env_grid(date, "chl")
        hla, hlo, ssha, ssh_date = _fetch_env_grid(date, "ssh")
        missing = [n for n, a in [("Chl-a", chla), ("SSHA", ssha)] if a is None]
        if missing:
            return jsonify({"ok": False,
                            "error": f"{date} 起無可用資料：{', '.join(missing)}"}), 502

        tlat, tlon = hb.target_grid(step=0.25)
        g_sst = hb.regrid_nearest(sla, slo, ssta, tlat, tlon)
        g_chl = hb.regrid_nearest(cla, clo, chla, tlat, tlon)
        g_ssh = hb.regrid_nearest(hla, hlo, ssha, tlat, tlon)
        res = hb.predict_grid(species, g_sst, g_chl, g_ssh, params)
        hsi = res["hsi"]

        def grid_json(a):
            return [[None if not np.isfinite(v) else round(float(v), 3) for v in row]
                    for row in a]

        finite = hsi[np.isfinite(hsi)]
        stats = {"min": round(float(finite.min()), 3) if finite.size else None,
                 "max": round(float(finite.max()), 3) if finite.size else None,
                 "mean": round(float(finite.mean()), 3) if finite.size else None,
                 "hot_cells": int((finite >= 0.75).sum())}
        log(f"✅ {zh}漁場預測完成 — 最適(≥0.75)格點 {stats['hot_cells']} 個"
            f"（SST {sst_date} · Chl {chl_date} · SSHA {ssh_date}）")
        return jsonify({
            "ok": True,
            "species": species, "name_zh": zh,
            "name_en": params["species"][species]["name_en"],
            "date": date,
            "sources": {"sst": sst_date, "chl": chl_date, "ssha": ssh_date},
            "lon": [round(float(x), 3) for x in tlon],
            "lat": [round(float(y), 3) for y in tlat],
            "hsi": grid_json(hsi),
            "sst": grid_json(g_sst),
            "chl": grid_json(g_chl),
            "ssha": grid_json(g_ssh),
            "ranges": {v: params["species"][species]["vars"][v]["optimal"]
                       for v in hb.ENV_VARS},
            "stats": stats,
        })
    except Exception as e:
        log(f"❌ 漁場預測失敗：{e}")
        log(traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Static convenience ─────────────────────────────────────────────────────
@app.route("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "img/fri_logo.jpg")


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-autoload", action="store_true")
    args = parser.parse_args()

    _check_token_at_startup()
    if not args.no_autoload:
        threading.Thread(target=_autoload, daemon=True).start()

    if not args.no_browser:
        url = f"http://{args.host}:{args.port}"
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
