from flask import Flask, render_template, request, jsonify, send_file
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
from werkzeug.utils import secure_filename

app = Flask(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
BASE_DIR = Path(__file__).resolve().parent
CSV_DIR = Path(os.environ.get("CSV_DIR", "/tmp/csv_files"))
DL_WORKERS = int(os.environ.get("DL_WORKERS", "8"))
CHUNK = 512 * 1024
ZIP_COMPRESS_LEVEL = int(os.environ.get("ZIP_COMPRESS_LEVEL", "1"))

def ensure_csv_dir():
    try:
        CSV_DIR.mkdir(parents=True, exist_ok=True)
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
CACHE_SIZE = 2

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
    ensure_csv_dir()
    bundled = glob.glob(str(BASE_DIR / "csv_files" / "*.csv"))
    for b in bundled:
        dest = CSV_DIR / os.path.basename(b)
        if not dest.exists():
            try:
                shutil.copy2(b, dest)
            except:
                pass
    files = glob.glob(str(CSV_DIR / "*.csv"))
    files = list(set([str(Path(f).resolve()) for f in files]))
    return sorted(files, key=os.path.getmtime, reverse=True)

_session = None
_session_lock = threading.Lock()

def get_session():
    global _session
    with _session_lock:
        if _session is None:
            s = requests.Session()
            retry = Retry(total=1, connect=1, read=1, backoff_factor=0.3,
                          status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
            adapter = HTTPAdapter(pool_connections=DL_WORKERS, pool_maxsize=DL_WORKERS,
                                  max_retries=retry)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            s.headers.update(HEADERS)
            _session = s
        return _session

@app.route('/')
def index():
    files = list_csvs()
    return render_template('index.html', files=files)

@app.route('/api/upload', methods=['POST'])
def upload():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file'}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        if not file.filename.lower().endswith('.csv'):
            return jsonify({'error': 'Only CSV files allowed'}), 400
        ensure_csv_dir()
        filename = secure_filename(file.filename)
        dest = CSV_DIR / filename
        file.save(str(dest))
        return jsonify({'success': True, 'filename': filename})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/load/<path:filename>')
def load_csv(filename):
    try:
        filename = os.path.basename(filename)
        csv_path = CSV_DIR / filename
        if not csv_path.exists():
            bundled = BASE_DIR / "csv_files" / filename
            if bundled.exists():
                try:
                    shutil.copy2(bundled, csv_path)
                except:
                    pass
        if not csv_path.exists():
            return jsonify({'error': 'File not found. Upload CSV from your device.'}), 404
        df, f = read_csv_cached(str(csv_path))
        preview_cols = ["Customer Number", "Customer Name", "Dial Status", "Start Time", "Total Duration", "Agent Name"]
        preview = []
        for _, row in f.head(50).iterrows():
            item = {}
            for c in preview_cols:
                if c in row:
                    item[c] = str(row[c])
            item['DurationSec'] = int(row.get('dur_sec', 0))
            preview.append(item)
        return jsonify({
            'success': True,
            'total': len(df),
            'filtered': len(f),
            'preview': preview
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/download', methods=['POST'])
def download():
    try:
        data = request.get_json()
        filename = data.get('filename')
        if not filename:
            return jsonify({'error': 'No filename'}), 400
        filename = os.path.basename(filename)
        csv_path = CSV_DIR / filename
        if not csv_path.exists():
            bundled = BASE_DIR / "csv_files" / filename
            if bundled.exists():
                try:
                    shutil.copy2(bundled, csv_path)
                except:
                    pass
        if not csv_path.exists():
            return jsonify({'error': 'File not found'}), 404
        
        df, f = read_csv_cached(str(csv_path))
        csv_base = csv_path.stem
        out_dir = Path(f"/tmp/rec_{csv_base[:20]}")
        zip_path = Path(f"/tmp/rec_{csv_base[:20]}.zip")
        
        # Clean up old files
        try:
            if out_dir.exists():
                shutil.rmtree(out_dir)
            if zip_path.exists():
                zip_path.unlink()
        except:
            pass
        
        out_dir.mkdir(exist_ok=True)
        
        urls_ser = f["Recording URL"].astype("string").str.strip()
        names = urls_ser.str.rsplit("/", n=1).str[-1].fillna("")
        urls_list = []
        seen = set()
        for url, name in zip(urls_ser.tolist(), names.tolist()):
            if not url: continue
            name = name or "rec.wav"
            if name in seen:
                stem, ext = os.path.splitext(name)
                k = 1
                while f"{stem}_{k}{ext}" in seen: k += 1
                name = f"{stem}_{k}{ext}"
            seen.add(name)
            urls_list.append((url, out_dir / name))
        
        total = len(urls_list)
        if total == 0:
            return jsonify({'error': 'No recordings'}), 400
        if total > 500:
            return jsonify({'error': f'Too many files ({total}). Vercel has limited space. Use local app for large downloads.'}), 400
        
        session = get_session()
        def fetch(item):
            url, path = item
            try:
                if path.exists() and path.stat().st_size > 0:
                    return True
                r = session.get(url, timeout=(10, 25), stream=True)
                if r.status_code == 200:
                    with open(path, "wb") as fh:
                        for chunk in r.iter_content(chunk_size=CHUNK):
                            if chunk:
                                fh.write(chunk)
                    r.close()
                    return True
                r.close()
                return False
            except:
                return False
        
        with ThreadPoolExecutor(max_workers=DL_WORKERS) as ex:
            list(ex.map(fetch, urls_list))
        
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=ZIP_COMPRESS_LEVEL) as z:
            for _, p in urls_list:
                if p.exists():
                    z.write(p, arcname=p.name)
        
        # Delete wav files to free space
        try:
            shutil.rmtree(out_dir)
        except:
            pass
        
        return send_file(str(zip_path), as_attachment=True, download_name=f"recordings_{csv_base}_gt5s.zip")
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5000)
