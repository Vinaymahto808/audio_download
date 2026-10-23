import gradio as gr
import pandas as pd
import os
import re
import zipfile
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from collections import OrderedDict
import threading
import time
import glob
import shutil
import tkinter as tk
from tkinter import filedialog

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

BASE_DIR = Path(__file__).resolve().parent
CSV_DIR = Path(os.environ.get("CSV_DIR", BASE_DIR / "csv_files"))
DL_WORKERS = int(os.environ.get("DL_WORKERS", "64"))
CHUNK = 256 * 1024
ZIP_COMPRESS_LEVEL = int(os.environ.get("ZIP_COMPRESS_LEVEL", "1"))
PREVIEW_ROWS = 500

DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", BASE_DIR))

def ensure_csv_dir():
    try:
        CSV_DIR.mkdir(parents=True, exist_ok=True)
        for old in glob.glob(str(BASE_DIR / "Report_*.csv")):
            src = Path(old)
            dest = CSV_DIR / src.name
            if dest.exists():
                dest = CSV_DIR / f"{src.stem}__{int(src.stat().st_mtime)}{src.suffix}"
            try:
                shutil.move(str(src), str(dest))
            except Exception:
                pass
    except Exception:
        pass

ensure_csv_dir()

def select_download_folder():
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        folder = filedialog.askdirectory(initialdir=str(DOWNLOAD_DIR), title="Select Download Folder")
        root.destroy()
        if folder:
            return folder
        return str(DOWNLOAD_DIR)
    except Exception as e:
        return str(DOWNLOAD_DIR)

def parse_duration(d):
    if not d or pd.isna(d):
        return 0
    s = str(d).strip()
    if not s:
        return 0
    parts = s.split(":")
    try:
        parts = [int(x) for x in parts]
    except:
        return 0
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0]*3600 + parts[1]*60 + parts[2]

def get_filtered(df, min_sec=5):
    df = df.copy()
    df["dur_sec"] = df["Total Duration"].apply(parse_duration)
    df = df[df["dur_sec"] > min_sec]
    df = df[df["Recording URL"].notna() & (df["Recording URL"].astype(str).str.strip() != "")]
    return df

_csv_cache = OrderedDict()
_cache_lock = threading.Lock()
CACHE_SIZE = 3

def read_csv_cached(csv_path):
    st = os.stat(csv_path)
    key = (str(csv_path), st.st_mtime_ns, st.st_size)
    with _cache_lock:
        if key in _csv_cache:
            _csv_cache.move_to_end(key)
            return _csv_cache[key]
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    filtered = get_filtered(df, 5)
    with _cache_lock:
        _csv_cache[key] = (df, filtered)
        while len(_csv_cache) > CACHE_SIZE:
            _csv_cache.popitem(last=False)
    return df, filtered

def list_csvs():
    files = glob.glob(str(CSV_DIR / "Report_*.csv")) + glob.glob(str(BASE_DIR / "Report_*.csv"))
    files = [str(Path(f).resolve()) for f in files]
    return sorted(files, key=os.path.getmtime, reverse=True)

def load_from_path(csv_path):
    try:
        if not csv_path or not os.path.exists(csv_path):
            return pd.DataFrame(), "CSV not found", csv_path
        df, f = read_csv_cached(csv_path)
        preview_cols = []
        for c in ["Customer Number", "Customer Name", "Dial Status", "Start Time", "Total Duration", "Agent Name", "Recording URL"]:
            if c in f.columns:
                preview_cols.append(c)
        if preview_cols:
            show = f[preview_cols].copy()
        else:
            show = f.copy()
        note = ""
        if len(show) > PREVIEW_ROWS:
            note = f" (showing first {PREVIEW_ROWS})"
            show = show.head(PREVIEW_ROWS)
        return show, (f"Loaded: {os.path.basename(csv_path)} | Total rows: {len(df)} | "
                      f"Recordings > 5s: {len(f)}{note}"), csv_path
    except Exception as e:
        return pd.DataFrame(), f"Error: {e}", csv_path

def _import_upload(uploaded_file):
    if uploaded_file is None:
        return None
    src = None
    if hasattr(uploaded_file, "name"):
        src = uploaded_file.name
    elif isinstance(uploaded_file, str):
        src = uploaded_file
    if not src or not os.path.exists(src):
        return None
    ensure_csv_dir()
    dest = CSV_DIR / os.path.basename(src)
    try:
        if os.path.abspath(src) != os.path.abspath(dest):
            shutil.copy2(src, dest)
        return str(dest.resolve())
    except Exception:
        return str(dest.resolve()) if dest.exists() else src

def load_csv(uploaded_file=None):
    try:
        csv_path = _import_upload(uploaded_file)
        if csv_path is None or not os.path.exists(csv_path):
            files = list_csvs()
            csv_path = files[0] if files else None
        if csv_path is None or not os.path.exists(csv_path):
            return pd.DataFrame(), f"No CSV found. Drop Report_*.csv in {CSV_DIR}.", None
        return load_from_path(csv_path)
    except Exception as e:
        return pd.DataFrame(), f"Error: {e}", None

def next_csv(current_path):
    files = list_csvs()
    if not files:
        return pd.DataFrame(), "No CSVs found", None
    if current_path in files:
        idx = files.index(current_path)
        nxt = files[idx + 1] if idx + 1 < len(files) else files[0]
    else:
        nxt = files[0]
    return load_from_path(nxt)

_session = None
_session_lock = threading.Lock()

def get_session():
    global _session
    with _session_lock:
        if _session is None:
            s = requests.Session()
            retry = Retry(total=3, connect=3, read=2, backoff_factor=0.4,
                          status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
            adapter = HTTPAdapter(pool_connections=DL_WORKERS, pool_maxsize=DL_WORKERS,
                                  max_retries=retry)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            s.headers.update(HEADERS)
            _session = s
        return _session

def _zip_up_to_date(zip_path, files_on_disk):
    if not files_on_disk or not zip_path.exists() or zip_path.stat().st_size == 0:
        return False
    try:
        with zipfile.ZipFile(zip_path) as z:
            stored = len(z.namelist())
        newest = max(p.stat().st_mtime for p in files_on_disk)
        return stored == len(files_on_disk) and zip_path.stat().st_mtime >= newest
    except Exception:
        return False

def download_all(uploaded_file=None, current_path=None, download_folder=None, progress=gr.Progress()):
    try:
        csv_path = _import_upload(uploaded_file)
        if csv_path is None or not os.path.exists(csv_path):
            csv_path = current_path
        if csv_path is None or not os.path.exists(csv_path):
            files = list_csvs()
            csv_path = files[0] if files else None
        if csv_path is None:
            return None, "No CSV found"

        df, f = read_csv_cached(csv_path)
        csv_base = Path(csv_path).stem
        
        if download_folder and os.path.exists(download_folder):
            base_out = Path(download_folder)
        else:
            base_out = DOWNLOAD_DIR
        base_out.mkdir(parents=True, exist_ok=True)
        
        out_dir = base_out / f"recordings_{csv_base}_gt5s"
        out_dir.mkdir(exist_ok=True)

        urls_ser = f["Recording URL"].astype("string").str.strip()
        names = urls_ser.str.rsplit("/", n=1).str[-1].fillna("")
        id_col = "Call ID" if "Call ID" in f.columns else ("Refid" if "Refid" in f.columns else None)
        if id_col:
            ids = f[id_col].astype("string").str.strip().fillna("")
            names = names.where(names != "", ids)
        names = names.where(names != "", pd.Series(
            [f"{i}.wav" for i in range(len(names))], index=names.index, dtype="string"))

        urls, seen = [], set()
        for url, name in zip(urls_ser.tolist(), names.tolist()):
            name = name or "recording.wav"
            if name in seen:
                stem, ext = os.path.splitext(name)
                k = 1
                while f"{stem}_{k}{ext}" in seen:
                    k += 1
                name = f"{stem}_{k}{ext}"
            seen.add(name)
            urls.append((url, out_dir / name))

        total = len(urls)
        if total == 0:
            return None, "Nothing to download"

        session = get_session()

        def fetch(item):
            url, path = item
            tmp = path.with_name(path.name + ".part")
            try:
                if path.exists() and path.stat().st_size > 0:
                    return path, True, "exists"
                r = session.get(url, timeout=(10, 60), stream=True)
                if r.status_code == 200:
                    with open(tmp, "wb") as fh:
                        for chunk in r.iter_content(chunk_size=CHUNK):
                            if chunk:
                                fh.write(chunk)
                    r.close()
                    os.replace(tmp, path)
                    return path, True, "ok"
                r.close()
                return path, False, f"http{r.status_code}"
            except Exception as e:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                return path, False, str(e)

        results = []
        done = 0
        last_emit = 0.0
        with ThreadPoolExecutor(max_workers=DL_WORKERS) as ex:
            futs = [ex.submit(fetch, u) for u in urls]
            for fut in as_completed(futs):
                done += 1
                try:
                    path, ok, msg = fut.result()
                    results.append((path.name, ok, msg))
                except Exception:
                    results.append(("unknown", False, "err"))
                now = time.time()
                if now - last_emit >= 0.25 or done == total:
                    progress(done / total, desc=f"Downloaded {done}/{total}")
                    last_emit = now

        okc = sum(1 for _, ok, _ in results if ok)
        failed = total - okc
        zip_path = base_out / f"recordings_{csv_base}_gt5s.zip"
        files_on_disk = [p for _, p in urls if p.exists() and p.stat().st_size > 0]

        if _zip_up_to_date(zip_path, files_on_disk):
            zip_msg = "ZIP already up-to-date (skipped)"
        else:
            t0 = time.time()
            try:
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                                     compresslevel=ZIP_COMPRESS_LEVEL, allowZip64=True) as z:
                    for i, p in enumerate(files_on_disk, 1):
                        z.write(p, arcname=p.name)
                        if i % 500 == 0:
                            progress(i / max(len(files_on_disk), 1), desc=f"Zipping {i}/{len(files_on_disk)}")
            except Exception:
                try:
                    zip_path.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
            zip_msg = f"ZIP built in {time.time() - t0:.0f}s"

        return str(zip_path), (f"Downloaded {okc}/{total} from {os.path.basename(csv_path)} "
                               f"({failed} failed). Folder: {out_dir} | {zip_msg} | ZIP: {zip_path}")
    except Exception as e:
        return None, f"Error: {e}"

with gr.Blocks(title="MCube Call Recordings Downloader") as demo:
    gr.Markdown(
        "# MCube Call Recordings Downloader\n"
        f"CSV folder: `{CSV_DIR.name}` — har CSV ke recordings apne alag folder me "
        "download honge."
    )
    with gr.Row():
        csv_upload = gr.File(label="Upload CSV (Report_*.csv)", file_types=[".csv"])
    with gr.Row():
        load_btn = gr.Button("Scan Current/Latest CSV", variant="secondary")
        next_btn = gr.Button("Next CSV ▶", variant="secondary")
    status = gr.Textbox(label="Status", interactive=False)
    csv_path_state = gr.State(value=None)
    df_out = gr.Dataframe(label="Recordings > 5s (preview)", wrap=True, interactive=False, max_height=400)
    
    with gr.Row():
        folder_btn = gr.Button("📁 Select Download Folder", variant="secondary")
        folder_path = gr.Textbox(label="Download Folder", value=str(DOWNLOAD_DIR), interactive=False)
    folder_state = gr.State(value=str(DOWNLOAD_DIR))
    
    dl_btn = gr.Button("Download All (>5s) Instantly", variant="primary")
    zip_out = gr.File(label="Download ZIP")
    dl_status = gr.Textbox(label="Download Status", interactive=False)

    def _load(f):
        df, msg, p = load_csv(f)
        return df, msg, p

    def _next(p):
        df, msg, np = next_csv(p)
        return df, msg, np
    
    def _select_folder():
        folder = select_download_folder()
        return folder, folder
    
    load_btn.click(_load, inputs=[csv_upload], outputs=[df_out, status, csv_path_state])
    next_btn.click(_next, inputs=[csv_path_state], outputs=[df_out, status, csv_path_state])
    folder_btn.click(_select_folder, inputs=[], outputs=[folder_path, folder_state])
    demo.load(_load, inputs=[csv_upload], outputs=[df_out, status, csv_path_state])
    dl_btn.click(download_all, inputs=[csv_upload, csv_path_state, folder_state], outputs=[zip_out, dl_status])

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    demo.launch(server_name="127.0.0.1", server_port=port, share=False)
