import streamlit as st
import pandas as pd
import os
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

st.set_page_config(page_title="MCube Call Recordings Downloader", layout="wide", page_icon="🎙️")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
BASE_DIR = Path(__file__).resolve().parent
CSV_DIR = Path(os.environ.get("CSV_DIR", BASE_DIR / "csv_files"))
DL_WORKERS = int(os.environ.get("DL_WORKERS", "64"))
CHUNK = 256 * 1024
ZIP_COMPRESS_LEVEL = int(os.environ.get("ZIP_COMPRESS_LEVEL", "1"))
PREVIEW_ROWS = 500

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
            except:
                pass
    except:
        pass

ensure_csv_dir()

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
    key = (str(csv_path), os.path.getmtime(csv_path))
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
    files = glob.glob(str(CSV_DIR / "*.csv")) + glob.glob(str(BASE_DIR / "*.csv"))
    files = [str(Path(f).resolve()) for f in files if Path(f).name.lower().endswith('.csv')]
    # remove duplicates
    files = list(dict.fromkeys(files))
    return sorted(files, key=os.path.getmtime, reverse=True)

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

st.title("🎙️ MCube Call Recordings Downloader")
st.caption(f"CSV Folder: {CSV_DIR} | Fast parallel downloads")

with st.sidebar:
    st.header("Settings")
    dl_workers = st.slider("Download Workers", 16, 100, DL_WORKERS, key="workers")
    st.session_state['dl_workers'] = dl_workers
    
    st.divider()
    uploaded = st.file_uploader("Upload CSV", type=['csv'], key="uploader")
    if uploaded is not None:
        ensure_csv_dir()
        dest = CSV_DIR / uploaded.name
        with open(dest, 'wb') as f:
            f.write(uploaded.getbuffer())
        st.success(f"Uploaded: {uploaded.name}")
        time.sleep(1)
        st.rerun()

files = list_csvs()
if not files:
    st.warning(f"No CSV files found. Upload any CSV file or place it in {CSV_DIR}")
    st.stop()

csv_options = {os.path.basename(f): f for f in files}
selected_name = st.selectbox("Select CSV File", list(csv_options.keys()), key="csv_select")
csv_path = csv_options[selected_name]

try:
    df, filtered = read_csv_cached(csv_path)
    st.success(f"Loaded: {selected_name} | Total: {len(df)} rows | Recordings > 5s: {len(filtered)}")
    
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Rows", len(df))
    c2.metric("Recordings > 5s", len(filtered))
    c3.metric("Files to Download", len(filtered))
    
    st.subheader("Preview (First 100)")
    preview_cols = [c for c in ["Customer Number", "Customer Name", "Dial Status", "Start Time", "Total Duration", "Agent Name"] if c in filtered.columns]
    if preview_cols:
        st.dataframe(filtered[preview_cols].head(100), use_container_width=True, hide_index=True)
    
    if st.button("🚀 Download All (>5s) as ZIP", type="primary", use_container_width=True):
        workers = st.session_state.get('dl_workers', DL_WORKERS)
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        csv_base = Path(csv_path).stem
        out_dir = BASE_DIR / f"recordings_{csv_base}_gt5s"
        out_dir.mkdir(exist_ok=True)
        
        urls_ser = filtered["Recording URL"].astype("string").str.strip()
        names = urls_ser.str.rsplit("/", n=1).str[-1].fillna("")
        id_col = "Call ID" if "Call ID" in filtered.columns else ("Refid" if "Refid" in filtered.columns else None)
        if id_col:
            ids = filtered[id_col].astype("string").str.strip().fillna("")
            names = names.where(names != "", ids)
        names = names.where(names != "", pd.Series([f"{i}.wav" for i in range(len(names))], index=names.index))
        
        urls, seen = [], set()
        for url, name in zip(urls_ser.tolist(), names.tolist()):
            if not url or url == '<NA>': continue
            name = str(name)
            if not name or name == '<NA>': name = "recording.wav"
            if name in seen:
                stem, ext = os.path.splitext(name)
                k = 1
                while f"{stem}_{k}{ext}" in seen: k += 1
                name = f"{stem}_{k}{ext}"
            seen.add(name)
            urls.append((url, out_dir / name))
        
        total = len(urls)
        session = get_session()
        results = []
        done = 0
        
        def fetch(item):
            url, path = item
            tmp = path.with_name(path.name + ".part")
            try:
                if path.exists() and path.stat().st_size > 0:
                    return True
                r = session.get(url, timeout=(10, 60), stream=True)
                if r.status_code == 200:
                    with open(tmp, "wb") as fh:
                        for chunk in r.iter_content(chunk_size=CHUNK):
                            if chunk: fh.write(chunk)
                    r.close()
                    os.replace(tmp, path)
                    return True
                r.close()
                return False
            except:
                try: tmp.unlink(missing_ok=True)
                except: pass
                return False
        
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(fetch, u) for u in urls]
            for fut in as_completed(futs):
                done += 1
                results.append(fut.result())
                progress_bar.progress(done/total, text=f"Downloaded {done}/{total}")
        
        okc = sum(1 for r in results if r)
        status_text.text(f"Downloaded {okc}/{total} files")
        
        zip_path = BASE_DIR / f"recordings_{csv_base}_gt5s.zip"
        with st.spinner("Creating ZIP..."):
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=ZIP_COMPRESS_LEVEL) as z:
                for _, p in urls:
                    if p.exists():
                        z.write(p, arcname=p.name)
        
        with open(zip_path, "rb") as f:
            st.download_button(
                label="📥 Download ZIP",
                data=f,
                file_name=f"recordings_{csv_base}_gt5s.zip",
                mime="application/zip",
                use_container_width=True
            )
        st.success(f"Done! {okc}/{total} downloaded. Folder: {out_dir}")
        
except Exception as e:
    st.error(f"Error: {e}")
    st.exception(e)
