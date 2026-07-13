"""
定點監測每日通報腳本 — 農業部水產試驗所 漁海況研究小組

讀取 stations.json 中的所有測站，向 NOAA ERDDAP 查詢最近 N 日
MUR SST 點位序列，依高/低溫閾值判定警報，輸出文字摘要與（可選）
HTML 報告。可搭配 Windows 工作排程器或 Cowork 排程每日執行，
再將輸出寄送 Gmail（沿用魚價報告模式）。

用法：
    python check_stations.py                 # 文字摘要（近 10 日）
    python check_stations.py --days 30
    python check_stations.py --html out.html # 另存 HTML 報告
    退出碼：0 = 正常；2 = 有警報（方便排程判斷是否寄信）
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "webapp"))
import timeseries as ts  # noqa: E402


def trend_arrow(values: list) -> str:
    v = [x for x in values if x is not None]
    if len(v) < 2:
        return "—"
    d = v[-1] - v[-2]
    if d > 0.15:
        return f"↑ +{d:.2f}"
    if d < -0.15:
        return f"↓ {d:.2f}"
    return f"→ {d:+.2f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--html", type=str, default=None)
    args = ap.parse_args()

    stations = ts.load_stations()
    if not stations:
        print("尚未設定任何測站（stations.json 為空）。請先在網頁介面新增測站。")
        return 0

    today = datetime.date.today().isoformat()
    rows, any_alert = [], False
    print(f"═══ 定點 SST 監測通報　{today}（近 {args.days} 日）═══\n")
    for st in stations:
        try:
            series = ts.fetch_point_series(st["lat"], st["lon"], args.days)
            res = ts.evaluate_alerts(st, series)
        except Exception as e:
            print(f"⚠️ {st['name']}：查詢失敗（{e}）")
            rows.append((st, None, None, [f"查詢失敗：{e}"], "—"))
            continue
        arrow = trend_arrow(series["values"])
        mark = "🚨" if res["alerts"] else "✅"
        if res["alerts"]:
            any_alert = True
        print(f"{mark} {st['name']}（{st['lat']}°N, {st['lon']}°E）")
        print(f"    最新 {res['latest_date']}：{res['latest_value']} °C　日變化 {arrow}")
        for a in res["alerts"]:
            print(f"    ⚠ {a}")
        rows.append((st, res["latest_date"], res["latest_value"], res["alerts"], arrow))

    if args.html:
        body_rows = ""
        for st, d, v, alerts, arrow in rows:
            color = "#c0392b" if alerts else "#1e8449"
            alert_txt = "；".join(alerts) if alerts else "正常"
            body_rows += (
                f"<tr><td>{st['name']}</td><td>{st['lat']}°N, {st['lon']}°E</td>"
                f"<td>{d or '—'}</td><td style='text-align:right'>{v if v is not None else '—'}</td>"
                f"<td>{arrow}</td><td style='color:{color};font-weight:600'>{alert_txt}</td></tr>"
            )
        html = f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">
<style>
 body{{font-family:'Noto Sans TC',sans-serif;background:#f4f7fa;padding:24px}}
 h2{{color:#0b3d5c}} table{{border-collapse:collapse;background:#fff;width:100%;max-width:860px}}
 th,td{{border:1px solid #d5dde5;padding:8px 12px;font-size:14px}}
 th{{background:#0b3d5c;color:#fff}} tr:nth-child(even){{background:#eef4f8}}
 .foot{{color:#888;font-size:12px;margin-top:12px}}
</style></head><body>
<h2>🌊 定點 SST 監測通報　{today}</h2>
<table><tr><th>測站</th><th>座標</th><th>最新日期</th><th>SST (°C)</th><th>日變化</th><th>狀態</th></tr>
{body_rows}</table>
<p class="foot">資料：NASA JPL MUR v4.1（NOAA ERDDAP jplMURSST41）·
農業部水產試驗所 漁海況研究小組 · 自動產生</p>
</body></html>"""
        out = pathlib.Path(args.html)
        out.write_text(html, encoding="utf-8")
        print(f"\n💾 HTML 報告：{out.resolve()}")

    return 2 if any_alert else 0


if __name__ == "__main__":
    sys.exit(main())
