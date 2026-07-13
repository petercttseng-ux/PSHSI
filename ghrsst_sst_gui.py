#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
 GHRSST Level-4 MUR SST 海面水溫桌面展示系統
 Marine Environmental Research, Fisheries Research Institute, MOA
 農業部水產試驗所 漁海況研究小組
==============================================================================
 功能：
  - 自動連結 NASA CMR 下載最新 MUR Global Foundation SST (v4.1)
  - 彩色 SST 分布圖 (西太平洋 114-162°E / 17-56°N)
  - 可選等溫線 (Cayula-Cornillon 白色 front 疊加)
  - 滑鼠懸停即時顯示經緯度 & 水溫
  - 滾輪縮放 / 框選放大 / 視圖歷史
==============================================================================
"""

import os
import sys
import re
import ssl
import time
import math
import threading
import queue
import traceback
import pathlib
import datetime
from functools import partial

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np

# ── Matplotlib ──────────────────────────────────────────────────────────────
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
from matplotlib.colors import LinearSegmentedColormap
import matplotlib.ticker as mticker
from mpl_toolkits.axes_grid1 import make_axes_locatable
import warnings
import logging
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message="Glyph.*missing from font")

# ── CJK Font Setup ──────────────────────────────────────────────────────────
def _setup_cjk_font():
    """Try to set a CJK-capable font for matplotlib."""
    from matplotlib import font_manager as fm
    cjk_candidates = [
        "Microsoft JhengHei",  # Traditional Chinese (Windows TW)
        "Microsoft YaHei",     # Simplified Chinese (Windows CN)
        "SimHei",
        "NSimSun",
        "MingLiU",
        "DFKai-SB",
        "Noto Sans CJK TC",
        "Noto Sans TC",
    ]
    available = {f.name for f in fm.fontManager.ttflist}
    for name in cjk_candidates:
        if name in available:
            matplotlib.rcParams["font.family"] = [name, "DejaVu Sans"]
            return name
    # fallback: no CJK font found, just suppress warnings
    return None

_CJK_FONT = _setup_cjk_font()

# ── Cartopy (optional but preferred) ────────────────────────────────────────
try:
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    from cartopy.mpl.ticker import LongitudeFormatter, LatitudeFormatter
    HAS_CARTOPY = True
except ImportError:
    HAS_CARTOPY = False

# ── Scientific / Network ─────────────────────────────────────────────────────
try:
    import netCDF4 as nc
    HAS_NETCDF4 = True
except ImportError:
    HAS_NETCDF4 = False

try:
    import requests
    from requests.auth import HTTPBasicAuth
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from scipy import ndimage
    from scipy.signal import convolve2d
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

# ── Constants ────────────────────────────────────────────────────────────────
LON_MIN, LON_MAX = 114.0, 162.0
LAT_MIN, LAT_MAX = 17.0, 56.0

# Credentials are managed by nasa_auth.py (env var / token file / netrc).
# Nothing is hard-coded here.
import nasa_auth

COLLECTION_ID = "C1996881146-POCLOUD"
CMR_GRANULE_URL = (
    "https://cmr.earthdata.nasa.gov/search/granules.json"
    "?collection_concept_id={collection_id}"
    "&sort_key=-start_date&page_size=1"
    "&bounding_box={lon_min},{lat_min},{lon_max},{lat_max}"
)
CMR_VIRTUAL_BASE = "https://cmr.earthdata.nasa.gov/virtual-directory/collections"
DATA_DIR = pathlib.Path("mur_data")
DATA_DIR.mkdir(exist_ok=True)

# Custom oceanographic color map
OCEAN_COLORS = [
    (0.00, "#030085"),   # very cold – dark blue
    (0.10, "#0028c8"),
    (0.22, "#0066ff"),
    (0.32, "#00b4e6"),
    (0.42, "#00e6b4"),
    (0.52, "#80ff80"),
    (0.62, "#ffff00"),   # warm – yellow
    (0.72, "#ffaa00"),
    (0.82, "#ff4400"),
    (0.92, "#cc0000"),
    (1.00, "#660000"),   # hot – dark red
]
OCN_CMAP = LinearSegmentedColormap.from_list(
    "ocean_thermal",
    [(p, c) for p, c in OCEAN_COLORS],
    N=512,
)

# ══════════════════════════════════════════════════════════════════════════════
#  Utility helpers
# ══════════════════════════════════════════════════════════════════════════════

def setup_netrc():
    """Deprecated no-op: credentials now come from nasa_auth (token/netrc)."""
    try:
        pass
    except Exception:
        pass  # best effort


def find_latest_nc():
    """Return the most recently modified .nc file in DATA_DIR, or None."""
    nc_files = sorted(DATA_DIR.glob("*.nc"), key=lambda p: p.stat().st_mtime, reverse=True)
    return nc_files[0] if nc_files else None


def fetch_latest_granule_url(log_cb):
    """
    Query NASA CMR API for the latest MUR granule and return its
    (download_url, filename) tuple, or (None, None) on failure.
    Uses the CMR virtual-directory approach: walk 2026 → latest month → latest day.
    """
    if not HAS_REQUESTS:
        log_cb("❌ 需安裝 requests 套件：pip install requests")
        return None, None

    # ── Step 1: query CMR search API ────────────────────────────────────────
    log_cb("🔍 查詢 NASA CMR 最新 MUR granule …")
    api_url = CMR_GRANULE_URL.format(
        collection_id=COLLECTION_ID,
        lon_min=LON_MIN, lat_min=LAT_MIN,
        lon_max=LON_MAX, lat_max=LAT_MAX,
    )
    try:
        session = nasa_auth.auth_session()
        resp = session.get(api_url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        entries = data.get("feed", {}).get("entry", [])
        if not entries:
            log_cb("⚠️  CMR API 無回覆 entry，改用虛擬目錄探索")
            return _crawl_virtual_directory(log_cb, session)

        entry = entries[0]
        # find the https data link
        for link in entry.get("links", []):
            href = link.get("href", "")
            if href.startswith("https://") and href.endswith(".nc") and "podaac" in href:
                fname = href.rsplit("/", 1)[-1]
                log_cb(f"✅ 找到最新 granule: {fname}")
                return href, fname

        # fallback: opendap granule name → build download URL
        granule_id = entry.get("producer_granule_id") or entry.get("title", "")
        if granule_id:
            fname = granule_id if granule_id.endswith(".nc") else granule_id + ".nc"
            dl_url = (
                f"https://archive.podaac.earthdata.nasa.gov/"
                f"podaac-ops-cumulus-protected/MUR-JPL-L4-GLOB-v4.1/{fname}"
            )
            log_cb(f"✅  (API fallback) {fname}")
            return dl_url, fname

    except Exception as e:
        log_cb(f"⚠️  CMR API 查詢失敗：{e}，嘗試虛擬目錄")

    return _crawl_virtual_directory(log_cb, None)


def _crawl_virtual_directory(log_cb, session=None):
    """Walk the CMR virtual-directory to find the latest granule."""
    if session is None:
        session = nasa_auth.auth_session()

    now = datetime.datetime.utcnow()
    years = [str(now.year), str(now.year - 1)]

    for year in years:
        base = f"{CMR_VIRTUAL_BASE}/{COLLECTION_ID}/temporal/{year}"
        log_cb(f"  → 探索年份目錄：{year}")
        try:
            r = session.get(base, timeout=20)
            r.raise_for_status()
            # parse month links  href=".../{year}/MM"
            months = sorted(
                set(re.findall(rf"/{year}/(\d{{2}})", r.text)),
                reverse=True,
            )
            if not months:
                continue
            for month in months:
                mbase = f"{base}/{month}"
                rm = session.get(mbase, timeout=20)
                rm.raise_for_status()
                days = sorted(
                    set(re.findall(rf"/{month}/(\d{{2}})", rm.text)),
                    reverse=True,
                )
                if not days:
                    continue
                for day in days:
                    dbase = f"{mbase}/{day}"
                    rd = session.get(dbase, timeout=20)
                    rd.raise_for_status()
                    # find .nc link
                    nc_links = re.findall(
                        r'href="(https://archive\.podaac\.earthdata\.nasa\.gov[^"]+\.nc)"',
                        rd.text,
                    )
                    if nc_links:
                        url = nc_links[0]
                        fname = url.rsplit("/", 1)[-1]
                        log_cb(f"✅ 虛擬目錄找到：{fname}")
                        return url, fname
        except Exception as e:
            log_cb(f"  ⚠️ 虛擬目錄錯誤：{e}")
            continue

    log_cb("❌ 無法從虛擬目錄找到最新 granule")
    return None, None


def download_file(url, dest_path, log_cb, progress_cb=None):
    """Stream-download url → dest_path with progress callback."""
    session = nasa_auth.auth_session()

    # Earthdata requires following redirects with auth
    resp = session.get(url, stream=True, timeout=60)
    resp.raise_for_status()

    total = int(resp.headers.get("content-length", 0))
    downloaded = 0
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 20):  # 1 MB
            f.write(chunk)
            downloaded += len(chunk)
            if progress_cb and total:
                progress_cb(downloaded / total * 100)
    log_cb(f"💾 下載完成：{dest_path} ({downloaded / 1e6:.1f} MB)")


# ══════════════════════════════════════════════════════════════════════════════
#  Cayula-Cornillon Front Detection
# ══════════════════════════════════════════════════════════════════════════════

def cayula_cornillon(sst_2d, window=32, overlap=0.5, threshold=2.0, pct_edge=0.5):
    """
    Simplified Cayula-Cornillon (1992) bimodal histogram front detection.
    Returns a boolean mask (True = front pixel).

    Parameters
    ----------
    sst_2d  : 2D numpy array, NaN where land/missing
    window  : sliding window size (pixels)
    overlap : fractional overlap between windows
    threshold : min STD inside a window to bother detecting
    pct_edge  : min fraction of valid pixels in a window
    """
    rows, cols = sst_2d.shape
    step = max(1, int(window * (1 - overlap)))
    front_mask = np.zeros((rows, cols), dtype=np.float32)

    for r0 in range(0, rows - window, step):
        for c0 in range(0, cols - window, step):
            r1, c1 = r0 + window, c0 + window
            patch = sst_2d[r0:r1, c0:c1]
            valid = patch[np.isfinite(patch)]
            if valid.size < window * window * pct_edge:
                continue
            if valid.std() < threshold:
                continue

            # Otsu-like bimodal split
            vmin, vmax = valid.min(), valid.max()
            if vmax - vmin < 0.5:
                continue

            hist, edges = np.histogram(valid, bins=32, density=True)
            # interclass variance maximisation (Otsu)
            total = hist.sum()
            best_var, best_t = 0, vmin
            w0 = 0.0
            mu0_sum = 0.0
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

            # gradient magnitude within patch as edge strength
            if HAS_SCIPY:
                grad_r = ndimage.sobel(np.where(np.isfinite(patch), patch, 0), axis=0)
                grad_c = ndimage.sobel(np.where(np.isfinite(patch), patch, 0), axis=1)
                edge   = np.hypot(grad_r, grad_c)
                # threshold: pixels near Otsu boundary with high gradient
                near_t = np.abs(patch - best_t) < 1.5
                high_g = edge > edge[np.isfinite(edge)].mean() + 0.5 * edge[np.isfinite(edge)].std() if edge[np.isfinite(edge)].size > 0 else np.zeros_like(edge, dtype=bool)
                front_mask[r0:r1, c0:c1] += (near_t & high_g).astype(np.float32)
            else:
                near_t = np.abs(patch - best_t) < 1.5
                front_mask[r0:r1, c0:c1] += near_t.astype(np.float32)

    # normalise & threshold
    fm_max = front_mask.max()
    if fm_max > 0:
        front_mask /= fm_max
    return front_mask > 0.15


# ══════════════════════════════════════════════════════════════════════════════
#  GUI Application
# ══════════════════════════════════════════════════════════════════════════════

class SSTApp(tk.Tk):
    """Main application window."""

    TITLE = "🌊 GHRSST MUR SST 海面水溫展示系統  ─  農業部水產試驗所 漁海況研究小組"
    BG   = "#0d1b2a"
    FG   = "#e0f0ff"
    ACCENT = "#00b4d8"
    BTN_BG = "#1a3a5c"
    BTN_FG = "#e0f0ff"
    BTN_ACTIVE = "#0077b6"

    def __init__(self):
        super().__init__()
        self.title(self.TITLE)
        self.configure(bg=self.BG)
        self.state("zoomed")  # maximise on Windows

        # Data holders
        self.sst_data   = None   # 2D numpy masked array (°C)
        self.lon_1d     = None
        self.lat_1d     = None
        self.nc_path    = None
        self.nc_date    = ""

        # Layer state
        self.show_isotherms = tk.BooleanVar(value=True)
        self.show_fronts    = tk.BooleanVar(value=False)
        self.front_mask     = None
        self.isotherm_interval = tk.DoubleVar(value=2.0)

        # View history (xlim, ylim) stack
        self._view_stack = []
        self._box_mode   = False
        self._box_start  = None
        self._box_rect   = None

        # Current view limits
        self._xlim = (LON_MIN, LON_MAX)
        self._ylim = (LAT_MIN, LAT_MAX)

        # Thread queue for log messages
        self._log_queue = queue.Queue()
        self._dl_thread = None

        # Style
        self._setup_style()

        # Build UI
        self._build_ui()

        # Start polling queue
        self._poll_log_queue()

        # Try loading existing data
        nc_file = find_latest_nc()
        if nc_file:
            self._load_nc_file(nc_file)

    # ── Style ────────────────────────────────────────────────────────────────

    def _setup_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".",
            background=self.BG, foreground=self.FG,
            fieldbackground="#1a3a5c",
            troughcolor="#1a3a5c",
            selectbackground=self.ACCENT,
            font=("Segoe UI", 10),
        )
        style.configure("TLabel", background=self.BG, foreground=self.FG)
        style.configure("TFrame", background=self.BG)
        style.configure("TLabelframe", background=self.BG, foreground=self.ACCENT, bordercolor=self.ACCENT)
        style.configure("TLabelframe.Label", background=self.BG, foreground=self.ACCENT, font=("Segoe UI", 10, "bold"))
        style.configure("TCheckbutton", background=self.BG, foreground=self.FG)
        style.configure("Horizontal.TScale", background=self.BG)
        style.configure("TProgressbar", troughcolor="#1a3a5c", background=self.ACCENT, bordercolor=self.BG)

    # ── UI Layout ────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Top banner
        self._build_banner()
        # Main area: sidebar + canvas
        main = ttk.Frame(self)
        main.pack(fill="both", expand=True, padx=0, pady=0)
        self._build_sidebar(main)
        self._build_canvas_area(main)
        # Status bar
        self._build_statusbar()

    def _build_banner(self):
        banner = tk.Frame(self, bg="#051828", height=54)
        banner.pack(fill="x")
        banner.pack_propagate(False)

        tk.Label(
            banner,
            text="🌊  GHRSST Level-4 MUR 海面水溫展示系統",
            bg="#051828", fg="#00e6ff",
            font=("Segoe UI", 15, "bold"),
        ).pack(side="left", padx=20, pady=10)

        tk.Label(
            banner,
            text="農業部水產試驗所  漁海況研究小組",
            bg="#051828", fg="#80d0ff",
            font=("Segoe UI", 12, "italic"),
        ).pack(side="right", padx=20, pady=10)

    def _build_sidebar(self, parent):
        sb = tk.Frame(parent, bg=self.BG, width=230)
        sb.pack(side="left", fill="y", padx=0)
        sb.pack_propagate(False)

        # ── Data section ──────────────────
        lf_data = ttk.LabelFrame(sb, text="📡  資料管理", padding=8)
        lf_data.pack(fill="x", padx=8, pady=(10, 4))

        self._btn(lf_data, "⬇  下載最新 MUR SST", self._on_download).pack(fill="x", pady=2)
        self._btn(lf_data, "📂  開啟本機 .nc 檔", self._on_open_file).pack(fill="x", pady=2)

        self.date_label = tk.Label(lf_data, text="資料日期：—",
            bg=self.BG, fg="#80d0ff", font=("Segoe UI", 9))
        self.date_label.pack(anchor="w", pady=(4, 0))

        self.progress_bar = ttk.Progressbar(lf_data, mode="determinate", length=200)
        self.progress_bar.pack(fill="x", pady=(4, 2))

        # ── Layers ────────────────────────
        lf_layer = ttk.LabelFrame(sb, text="🎨  圖層控制", padding=8)
        lf_layer.pack(fill="x", padx=8, pady=4)

        ttk.Checkbutton(
            lf_layer, text="黑色等溫線 (Isotherms)",
            variable=self.show_isotherms,
            command=self._refresh_plot,
        ).pack(anchor="w", pady=1)

        row_iso = ttk.Frame(lf_layer)
        row_iso.pack(fill="x", pady=2)
        ttk.Label(row_iso, text="間距 °C:").pack(side="left")
        self.iso_spin = ttk.Spinbox(
            row_iso, from_=0.5, to=5.0, increment=0.5,
            textvariable=self.isotherm_interval, width=5,
            command=self._refresh_plot,
        )
        self.iso_spin.pack(side="left", padx=4)

        ttk.Checkbutton(
            lf_layer, text="白色海洋前緣 (Fronts)",
            variable=self.show_fronts,
            command=self._refresh_plot,
        ).pack(anchor="w", pady=1)

        self._btn(lf_layer, "🔍  執行 Cayula-Cornillon 偵測",
            self._on_detect_fronts).pack(fill="x", pady=(4, 2))

        # ── View controls ─────────────────
        lf_view = ttk.LabelFrame(sb, text="🔭  視圖操作", padding=8)
        lf_view.pack(fill="x", padx=8, pady=4)

        self.box_btn = self._btn(lf_view, "📐  框選放大", self._toggle_box_mode)
        self.box_btn.pack(fill="x", pady=2)

        self._btn(lf_view, "↩  上一視圖", self._prev_view).pack(fill="x", pady=2)
        self._btn(lf_view, "🏠  重置視圖", self._reset_view).pack(fill="x", pady=2)

        # ── Export ────────────────────────
        lf_exp = ttk.LabelFrame(sb, text="💾  匯出", padding=8)
        lf_exp.pack(fill="x", padx=8, pady=4)

        self._btn(lf_exp, "💾  儲存圖片 (PNG)", self._save_image).pack(fill="x", pady=2)

        # ── Log ───────────────────────────
        lf_log = ttk.LabelFrame(sb, text="📋  訊息記錄", padding=8)
        lf_log.pack(fill="both", expand=True, padx=8, pady=4)

        log_frame = tk.Frame(lf_log, bg=self.BG)
        log_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(
            log_frame, bg="#071422", fg="#b0d8f0",
            font=("Consolas", 8), wrap="word",
            state="disabled", relief="flat", height=10,
        )
        sb_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb_scroll.set)
        sb_scroll.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)

    def _build_canvas_area(self, parent):
        canvas_frame = tk.Frame(parent, bg="#060e18")
        canvas_frame.pack(side="left", fill="both", expand=True)

        self.fig = Figure(figsize=(10, 8), facecolor="#060e18", dpi=100)
        self.fig.subplots_adjust(left=0.06, right=0.92, top=0.96, bottom=0.06)

        self.canvas = FigureCanvasTkAgg(self.fig, master=canvas_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        # Matplotlib events
        self.canvas.mpl_connect("scroll_event",    self._on_scroll)
        self.canvas.mpl_connect("button_press_event",   self._on_btn_press)
        self.canvas.mpl_connect("button_release_event", self._on_btn_release)
        self.canvas.mpl_connect("motion_notify_event",  self._on_mouse_move)

        # Draw blank axes
        self._init_axes()

    def _build_statusbar(self):
        status_frame = tk.Frame(self, bg="#051828", height=26)
        status_frame.pack(fill="x", side="bottom")
        status_frame.pack_propagate(False)

        self.status_var = tk.StringVar(value="就緒 │ 請下載或載入 NetCDF 資料")
        tk.Label(
            status_frame, textvariable=self.status_var,
            bg="#051828", fg="#80d0ff",
            font=("Segoe UI", 9), anchor="w",
        ).pack(side="left", padx=10, fill="x", expand=True)

        tk.Label(
            status_frame,
            text="農業部水產試驗所  漁海況研究小組",
            bg="#051828", fg="#3a6080",
            font=("Segoe UI", 9, "italic"),
        ).pack(side="right", padx=10)

    # ── Widget Helper ─────────────────────────────────────────────────────────

    def _btn(self, parent, text, command):
        b = tk.Button(
            parent, text=text, command=command,
            bg=self.BTN_BG, fg=self.BTN_FG,
            activebackground=self.BTN_ACTIVE, activeforeground="white",
            relief="flat", bd=0, padx=6, pady=5,
            font=("Segoe UI", 9, "bold"), cursor="hand2",
        )
        return b

    # ── Axes Init ────────────────────────────────────────────────────────────

    def _init_axes(self):
        self.fig.clear()
        if HAS_CARTOPY:
            self.ax = self.fig.add_subplot(
                1, 1, 1,
                projection=ccrs.PlateCarree(),
            )
            self.ax.set_extent([LON_MIN, LON_MAX, LAT_MIN, LAT_MAX], crs=ccrs.PlateCarree())
        else:
            self.ax = self.fig.add_subplot(1, 1, 1)
            self.ax.set_xlim(LON_MIN, LON_MAX)
            self.ax.set_ylim(LAT_MIN, LAT_MAX)

        self.ax.set_facecolor("#090f1a")
        self.fig.patch.set_facecolor("#060e18")
        self._style_axes()

        # Placeholder text
        self.ax.text(
            0.5, 0.5,
            "🌊  尚無資料\n請下載或載入 NetCDF 資料",
            transform=self.ax.transAxes,
            ha="center", va="center",
            fontsize=14, color="#3a6080",
            fontfamily="Segoe UI",
        )

        self.canvas.draw_idle()
        self._pcolormesh  = None
        self._colorbar    = None
        self._contour_obj = None
        self._front_obj   = None

    def _style_axes(self, ax=None):
        ax = ax or self.ax
        ax.tick_params(colors=self.FG, labelsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor(self.ACCENT)
        ax.title.set_color(self.FG)
        ax.xaxis.label.set_color(self.FG)
        ax.yaxis.label.set_color(self.FG)

    # ── Logging ──────────────────────────────────────────────────────────────

    def _log(self, msg):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._log_queue.put(f"[{ts}] {msg}")

    def _poll_log_queue(self):
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(200, self._poll_log_queue)

    # ── Download ─────────────────────────────────────────────────────────────

    def _on_download(self):
        if self._dl_thread and self._dl_thread.is_alive():
            self._log("⚠️  下載中，請稍候")
            return
        cred = nasa_auth.token_status()
        self._log(("🔑 " if cred["valid"] else "⚠️  ") + cred["message"])
        if not cred["valid"]:
            return
        self._dl_thread = threading.Thread(target=self._download_worker, daemon=True)
        self._dl_thread.start()

    def _download_worker(self):
        self._log("🚀 開始資料下載流程 …")
        url, fname = fetch_latest_granule_url(self._log)
        if not url:
            self._log("❌ 無法取得下載連結，請確認帳號權限或網路狀態")
            return

        dest = DATA_DIR / fname
        if dest.exists():
            self._log(f"ℹ️  本機已有：{fname}，直接載入")
            self.after(0, lambda: self._load_nc_file(dest))
            return

        self._log(f"⬇️  下載：{fname}")
        try:
            def prog(pct):
                self.after(0, lambda: self._set_progress(pct))
            download_file(url, dest, self._log, prog)
            self.after(0, lambda: self._load_nc_file(dest))
        except Exception as e:
            self._log(f"❌ 下載失敗：{e}")
            self._log("💡 請確認：1) NASA Earthdata 帳號已取得 PODAAC 授權")
            self._log("          2) 可至 https://urs.earthdata.nasa.gov 申請")
            self._log(traceback.format_exc())

    def _set_progress(self, pct):
        self.progress_bar["value"] = pct

    # ── Open File ─────────────────────────────────────────────────────────────

    def _on_open_file(self):
        path = filedialog.askopenfilename(
            title="開啟 MUR SST NetCDF",
            filetypes=[("NetCDF", "*.nc *.nc4"), ("All", "*.*")],
            initialdir=str(DATA_DIR),
        )
        if path:
            self._load_nc_file(pathlib.Path(path))

    # ── Load NetCDF ──────────────────────────────────────────────────────────

    def _load_nc_file(self, path):
        if not HAS_NETCDF4:
            messagebox.showerror("缺少套件", "請安裝 netCDF4：pip install netCDF4")
            return
        self._log(f"📂 載入：{path.name}")
        try:
            ds = nc.Dataset(str(path))

            # ── coordinates ──────────────────────────────────────────────────
            lat = ds.variables["lat"][:]
            lon = ds.variables["lon"][:]

            # mask to AOI
            ilat = np.where((lat >= LAT_MIN) & (lat <= LAT_MAX))[0]
            ilon = np.where((lon >= LON_MIN) & (lon <= LON_MAX))[0]

            lat_sub = lat[ilat]
            lon_sub = lon[ilon]

            # ── SST ──────────────────────────────────────────────────────────
            # We MUST disable auto mask+scale to avoid double-application.
            # MUR stores int16 with scale_factor=0.001, add_offset=298.15 (Kelvin).
            # netCDF4 applies these automatically when set_auto_maskandscale=True,
            # so reading directly would give the correct Kelvin value already.
            # We disable it and apply manually for explicit control.
            ds.set_auto_maskandscale(False)
            sst_var = ds.variables["analysed_sst"]

            # Read raw packed integers
            if sst_var.ndim == 3:
                sst_raw = sst_var[0, ilat[0]:ilat[-1]+1, ilon[0]:ilon[-1]+1]
            else:
                sst_raw = sst_var[ilat[0]:ilat[-1]+1, ilon[0]:ilon[-1]+1]

            # Read metadata attributes
            scale  = float(getattr(sst_var, "scale_factor", 1.0))
            offset = float(getattr(sst_var, "add_offset",   0.0))
            fill   = int(getattr(sst_var, "_FillValue",   -32768))
            units  = str(getattr(sst_var, "units", "kelvin")).lower()

            ds.close()

            # Apply scale + offset ONCE → physical value in declared units
            sst_raw = np.array(sst_raw, dtype=np.int32)   # avoid int16 overflow
            fill_mask = (sst_raw == fill)
            sst_phys = sst_raw.astype(np.float32) * scale + offset

            # Mask fill/land pixels
            sst_c = np.ma.array(sst_phys, mask=fill_mask)

            # Convert to Celsius
            if "kelvin" in units or (not sst_c.mask.all() and float(sst_c[~sst_c.mask].mean()) > 200):
                sst_c -= 273.15

            self.sst_data = sst_c
            self.lon_1d   = np.array(lon_sub)
            self.lat_1d   = np.array(lat_sub)
            self.front_mask = None
            self.show_fronts.set(False)
            self.nc_path  = path

            # Try to parse date from filename
            m = re.search(r"(\d{8})", path.name)
            if m:
                d = m.group(1)
                self.nc_date = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            else:
                self.nc_date = path.name

            self.date_label.config(text=f"資料日期：{self.nc_date}")
            self._log(f"✅ 資料載入成功 shape={sst_c.shape}  T範圍={float(sst_c.min()):.1f}~{float(sst_c.max()):.1f} °C")

            # Reset view
            self._xlim = (LON_MIN, LON_MAX)
            self._ylim = (LAT_MIN, LAT_MAX)
            self._view_stack.clear()
            self._refresh_plot()

        except Exception as e:
            self._log(f"❌ 讀取 NetCDF 失敗：{e}")
            self._log(traceback.format_exc())

    # ── Plot ─────────────────────────────────────────────────────────────────

    def _refresh_plot(self):
        if self.sst_data is None:
            return
        self._draw_map()

    def _draw_map(self):
        self.fig.clear()

        if HAS_CARTOPY:
            self.ax = self.fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
            transform = ccrs.PlateCarree()
        else:
            self.ax = self.fig.add_subplot(1, 1, 1)
            transform = None

        self.ax.set_facecolor("#090f1a")
        self.fig.patch.set_facecolor("#060e18")

        lon2d, lat2d = np.meshgrid(self.lon_1d, self.lat_1d)
        sst = self.sst_data

        # Determine T range
        valid = sst.compressed() if np.ma.is_masked(sst) else sst[np.isfinite(sst)]
        vmin = np.percentile(valid, 2)
        vmax = np.percentile(valid, 98)

        # ── SST pcolormesh ───────────────────────────────────────────────────
        kwargs = dict(cmap=OCN_CMAP, vmin=vmin, vmax=vmax, shading="auto")
        if HAS_CARTOPY:
            kwargs["transform"] = transform
        pcm = self.ax.pcolormesh(lon2d, lat2d, sst, **kwargs)

        # ── Colorbar ─────────────────────────────────────────────────────────
        cbar = self.fig.colorbar(pcm, ax=self.ax, fraction=0.025, pad=0.01, shrink=0.85)
        cbar.set_label("海面水溫 SST (°C)", color=self.FG, fontsize=9)
        cbar.ax.yaxis.set_tick_params(color=self.FG, labelcolor=self.FG)
        cbar.outline.set_edgecolor(self.ACCENT)
        plt.setp(cbar.ax.get_yticklabels(), color=self.FG)

        # ── Isotherms ────────────────────────────────────────────────────────
        if self.show_isotherms.get():
            interval = float(self.isotherm_interval.get())
            lvls = np.arange(np.ceil(vmin / interval) * interval,
                             np.floor(vmax / interval) * interval + interval,
                             interval)
            if len(lvls) > 1:
                cnt_kwargs = dict(colors="black", linewidths=0.7, alpha=0.75, levels=lvls)
                if HAS_CARTOPY:
                    cnt_kwargs["transform"] = transform
                cs = self.ax.contour(lon2d, lat2d, sst, **cnt_kwargs)
                self.ax.clabel(cs, inline=True, fmt="%.0f", fontsize=6, colors="black")

        # ── Fronts ───────────────────────────────────────────────────────────
        if self.show_fronts.get() and self.front_mask is not None:
            fm = self.front_mask.astype(float)
            fm_ma = np.ma.masked_where(fm == 0, fm)
            fr_kwargs = dict(
                cmap=LinearSegmentedColormap.from_list("wh", ["white", "white"]),
                alpha=0.85, vmin=0, vmax=1, shading="auto",
            )
            if HAS_CARTOPY:
                fr_kwargs["transform"] = transform
            self.ax.pcolormesh(lon2d, lat2d, fm_ma, **fr_kwargs)

        # ── Coastline (cartopy) ───────────────────────────────────────────────
        if HAS_CARTOPY:
            self.ax.add_feature(
                cfeature.NaturalEarthFeature(
                    "physical", "land", "10m",
                    facecolor="#2a3f2a", edgecolor="#6aaf6a", linewidth=0.7,
                ),
                zorder=3,
            )
            self.ax.add_feature(
                cfeature.NaturalEarthFeature(
                    "cultural", "admin_0_boundary_lines_land", "10m",
                    facecolor="none", edgecolor="#888888", linewidth=0.5,
                ),
                zorder=4,
            )
            self.ax.set_extent([self._xlim[0], self._xlim[1], self._ylim[0], self._ylim[1]],
                               crs=ccrs.PlateCarree())

            gl = self.ax.gridlines(
                crs=ccrs.PlateCarree(), draw_labels=True,
                linewidth=0.4, color="#2a4060", alpha=0.7,
                xlocs=mticker.MultipleLocator(10),
                ylocs=mticker.MultipleLocator(10),
            )
            gl.top_labels   = False
            gl.right_labels = False
            gl.xlabel_style = {"color": self.FG, "fontsize": 8}
            gl.ylabel_style = {"color": self.FG, "fontsize": 8}

        else:
            # Fallback: simple grid
            self.ax.set_xlim(self._xlim)
            self.ax.set_ylim(self._ylim)
            self.ax.grid(color="#2a4060", linewidth=0.4, alpha=0.7)
            self.ax.tick_params(colors=self.FG, labelsize=8)
            for spine in self.ax.spines.values():
                spine.set_edgecolor(self.ACCENT)
            self.ax.set_xlabel("Longitude (°E)", color=self.FG, fontsize=9)
            self.ax.set_ylabel("Latitude (°N)", color=self.FG, fontsize=9)

        # Title
        self.ax.set_title(
            f"GHRSST Level-4 MUR Global Foundation SST Analysis (v4.1)  ─  {self.nc_date}",
            color=self.FG, fontsize=10, pad=8,
        )

        self.canvas.draw_idle()
        self.status_var.set(f"資料日期：{self.nc_date}  │  T範圍：{float(self.sst_data.min()):.1f} ~ {float(self.sst_data.max()):.1f} °C  │  懸停即時顯示座標")

    # ── Cayula-Cornillon ──────────────────────────────────────────────────────

    def _on_detect_fronts(self):
        if self.sst_data is None:
            messagebox.showwarning("無資料", "請先載入 SST 資料")
            return
        self._log("🔍 開始 Cayula-Cornillon 偵測（可能需 1-3 分鐘）…")
        t = threading.Thread(target=self._detect_worker, daemon=True)
        t.start()

    def _detect_worker(self):
        try:
            sst = np.array(self.sst_data, dtype=float)
            if np.ma.is_masked(self.sst_data):
                sst[self.sst_data.mask] = np.nan

            # Downsample for speed if grid is large
            rows, cols = sst.shape
            factor = max(1, max(rows, cols) // 500)
            if factor > 1:
                self._log(f"  ↳ 資料較大，降採樣 1/{factor} 加速偵測")
                sst_ds = sst[::factor, ::factor]
            else:
                sst_ds = sst

            self._log(f"  ↳ 偵測網格：{sst_ds.shape}")
            fm_ds = cayula_cornillon(sst_ds, window=24, overlap=0.5)

            # Upsample back
            if factor > 1:
                from scipy.ndimage import zoom as ndz
                fm = ndz(fm_ds.astype(float), factor, order=0) > 0.5
                # trim to original shape
                fm = fm[:rows, :cols]
            else:
                fm = fm_ds

            self.front_mask = fm
            self._log(f"  ↳ 偵測完成，front 像素：{int(fm.sum())}/{fm.size} ({fm.mean()*100:.1f}%)")
            self.show_fronts.set(True)
            self.after(0, self._refresh_plot)
        except Exception as e:
            self._log(f"❌ 偵測錯誤：{e}")
            self._log(traceback.format_exc())

    # ── Mouse / Interaction ───────────────────────────────────────────────────

    def _on_mouse_move(self, event):
        if event.inaxes != self.ax or self.sst_data is None:
            return

        lon_c, lat_c = event.xdata, event.ydata
        if lon_c is None or lat_c is None:
            return

        # Find nearest grid value
        ilon = np.argmin(np.abs(self.lon_1d - lon_c))
        ilat = np.argmin(np.abs(self.lat_1d - lat_c))
        sst_val = self.sst_data[ilat, ilon]

        if np.ma.is_masked(sst_val) or not np.isfinite(float(sst_val)):
            t_str = "陸地/遮罩"
        else:
            t_str = f"{float(sst_val):.2f} °C"

        self.status_var.set(
            f"經度：{lon_c:.3f}°E  │  緯度：{lat_c:.3f}°N  │  水溫：{t_str}"
            f"  │  資料日期：{self.nc_date}"
        )

        # Update box drawing
        if self._box_mode and self._box_start is not None:
            x0, y0 = self._box_start
            if self._box_rect:
                self._box_rect.remove()
            self._box_rect = Rectangle(
                (min(x0, lon_c), min(y0, lat_c)),
                abs(lon_c - x0), abs(lat_c - y0),
                linewidth=1.5, edgecolor="yellow", facecolor="none",
                linestyle="--",
            )
            self.ax.add_patch(self._box_rect)
            self.canvas.draw_idle()

    def _on_scroll(self, event):
        if event.inaxes != self.ax or self.sst_data is None:
            return

        factor = 0.8 if event.button == "up" else 1.25
        xdata, ydata = event.xdata, event.ydata
        if xdata is None or ydata is None:
            return

        x0, x1 = self._get_xlim()
        y0, y1 = self._get_ylim()
        dx = (x1 - x0) * factor / 2
        dy = (y1 - y0) * factor / 2

        new_xlim = (xdata - dx, xdata + dx)
        new_ylim = (ydata - dy, ydata + dy)
        self._push_view()
        self._apply_view(new_xlim, new_ylim)

    def _on_btn_press(self, event):
        if event.inaxes != self.ax:
            return
        if self._box_mode and event.button == 1:
            self._box_start = (event.xdata, event.ydata)

    def _on_btn_release(self, event):
        if not self._box_mode or event.button != 1:
            return
        if self._box_start is None or event.inaxes != self.ax:
            return

        x0, y0 = self._box_start
        x1, y1 = event.xdata, event.ydata
        if x1 is None or y1 is None:
            return
        if abs(x1 - x0) < 0.2 or abs(y1 - y0) < 0.2:
            return

        self._push_view()
        self._apply_view(
            (min(x0, x1), max(x0, x1)),
            (min(y0, y1), max(y0, y1)),
        )
        self._box_start = None
        if self._box_rect:
            try:
                self._box_rect.remove()
            except Exception:
                pass
            self._box_rect = None
        self._toggle_box_mode()  # exit box mode

    def _push_view(self):
        self._view_stack.append((self._xlim, self._ylim))
        if len(self._view_stack) > 50:
            self._view_stack.pop(0)

    def _prev_view(self):
        if not self._view_stack:
            return
        self._xlim, self._ylim = self._view_stack.pop()
        self._apply_view(self._xlim, self._ylim, push=False)

    def _reset_view(self):
        self._push_view()
        self._apply_view((LON_MIN, LON_MAX), (LAT_MIN, LAT_MAX), push=False)

    def _apply_view(self, xlim, ylim, push=True):
        if push:
            self._push_view()
        self._xlim = xlim
        self._ylim = ylim
        if HAS_CARTOPY and hasattr(self, "ax"):
            try:
                self.ax.set_extent(
                    [xlim[0], xlim[1], ylim[0], ylim[1]],
                    crs=ccrs.PlateCarree(),
                )
                self.canvas.draw_idle()
                return
            except Exception:
                pass
        if hasattr(self, "ax"):
            self.ax.set_xlim(xlim)
            self.ax.set_ylim(ylim)
            self.canvas.draw_idle()

    def _get_xlim(self):
        if HAS_CARTOPY and hasattr(self, "ax"):
            try:
                return self.ax.get_xlim()
            except Exception:
                pass
        return self._xlim

    def _get_ylim(self):
        if HAS_CARTOPY and hasattr(self, "ax"):
            try:
                return self.ax.get_ylim()
            except Exception:
                pass
        return self._ylim

    def _toggle_box_mode(self):
        self._box_mode = not self._box_mode
        if self._box_mode:
            self.box_btn.config(bg="#005577", relief="sunken", text="📐 框選中… (再按取消)")
            self.canvas.get_tk_widget().config(cursor="crosshair")
        else:
            self.box_btn.config(bg=self.BTN_BG, relief="flat", text="📐 框選放大")
            self.canvas.get_tk_widget().config(cursor="")

    # ── Save ─────────────────────────────────────────────────────────────────

    def _save_image(self):
        fname = f"SST_{self.nc_date or 'output'}.png"
        path = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("All", "*.*")],
            initialfile=fname,
        )
        if path:
            self.fig.savefig(path, dpi=200, bbox_inches="tight",
                             facecolor=self.fig.get_facecolor())
            self._log(f"💾 圖片已儲存：{path}")
            messagebox.showinfo("儲存完成", f"圖片已儲存：{path}")


# ══════════════════════════════════════════════════════════════════════════════
#  Entry Point
# ══════════════════════════════════════════════════════════════════════════════

def check_dependencies():
    missing = []
    if not HAS_NETCDF4:
        missing.append("netCDF4")
    if not HAS_REQUESTS:
        missing.append("requests")
    if not HAS_SCIPY:
        missing.append("scipy")
    if not HAS_CARTOPY:
        missing.append("cartopy (optional, fallback available)")
    return missing


if __name__ == "__main__":
    missing = check_dependencies()
    critical_missing = [m for m in missing if "optional" not in m]
    if critical_missing:
        root = tk.Tk()
        root.withdraw()
        msg = (
            "缺少必要套件，請安裝後再啟動：\n\n"
            + "\n".join(f"  pip install {m}" for m in critical_missing)
            + "\n\n建議使用：\n  conda install -c conda-forge netCDF4 requests scipy cartopy"
        )
        messagebox.showerror("缺少套件", msg)
        sys.exit(1)

    if missing:
        import warnings
        warnings.warn(f"可選套件未安裝：{missing}，部分功能受限")

    app = SSTApp()
    app.mainloop()
