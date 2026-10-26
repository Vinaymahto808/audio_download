import gradio as gr
import pandas as pd
import os
import re
import zipfile
import requests
import struct
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock, local
import time
import glob
import shutil

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
DOWNLOAD_WORKERS = 25
DOWNLOAD_CHUNK_SIZE = 256 * 1024
DOWNLOAD_RETRIES = 3
ZIP_PART_MAX_BYTES = 2 * 1024**3
ZIP_PART_TARGET_BYTES = ZIP_PART_MAX_BYTES - 64 * 1024**2


def is_complete_download(path):
    try:
        size = path.stat().st_size
        if size == 0:
            return False
        if path.suffix.lower() != ".wav":
            return True
        with path.open("rb") as audio_file:
            header = audio_file.read(12)
        if len(header) != 12 or header[8:12] != b"WAVE":
            return False
        if header[:4] == b"RIFF":
            declared_size = struct.unpack_from("<I", header, 4)[0] + 8
            return declared_size <= size
        if header[:4] == b"RIFX":
            declared_size = int.from_bytes(header[4:8], "big") + 8
            return declared_size <= size
        return header[:4] == b"RF64"
    except OSError:
        return False


def write_zip_parts(paths, zip_base):
    groups = []
    current_group = []
    current_size = 0
    for path in paths:
        file_size = path.stat().st_size
        if current_group and current_size + file_size > ZIP_PART_TARGET_BYTES:
            groups.append(current_group)
            current_group = []
            current_size = 0
        current_group.append(path)
        current_size += file_size
    if current_group:
        groups.append(current_group)

    zip_paths = []
    oversized_files = []

    def write_group(group):
        zip_path = Path(f"{zip_base}_part{len(zip_paths) + 1:03d}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in group:
                archive.write(path, arcname=path.name)
        if zip_path.stat().st_size <= ZIP_PART_MAX_BYTES:
            zip_paths.append(zip_path)
            return

        zip_path.unlink()
        if len(group) == 1:
            oversized_files.append(group[0].name)
            return
        midpoint = len(group) // 2
        write_group(group[:midpoint])
        write_group(group[midpoint:])

    for group in groups:
        write_group(group)
    return zip_paths, oversized_files

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

def list_csvs():
    CSV_FILES = sorted(glob.glob("Report_*.csv"), key=lambda x: os.path.getmtime(x))
    # return most recent first
    return sorted(CSV_FILES, key=lambda x: os.path.getmtime(x), reverse=True)

def load_from_path(csv_path):
    try:
        if not csv_path or not os.path.exists(csv_path):
            return pd.DataFrame(), "CSV not found", csv_path
        df = pd.read_csv(csv_path, encoding="utf-8-sig")
        f = get_filtered(df, 5)
        preview_cols = []
        for c in ["Customer Number", "Customer Name", "Dial Status", "Start Time", "Total Duration", "Agent Name", "Recording URL"]:
            if c in f.columns:
                preview_cols.append(c)
        if preview_cols:
            show = f[preview_cols].copy()
        else:
            show = f.copy()
        return show, f"Loaded: {os.path.basename(csv_path)} | Total rows: {len(df)} | Recordings > 5s: {len(f)}", csv_path
    except Exception as e:
        return pd.DataFrame(), f"Error: {e}", csv_path

def load_csv(uploaded_file=None):
    try:
        csv_path = None
        if uploaded_file is not None:
            if hasattr(uploaded_file, "name"):
                csv_path = uploaded_file.name
            elif isinstance(uploaded_file, str):
                csv_path = uploaded_file
            if csv_path and os.path.exists(csv_path):
                dest = os.path.basename(csv_path)
                try:
                    if os.path.abspath(csv_path) != os.path.abspath(dest):
                        shutil.copy2(csv_path, dest)
                        csv_path = dest
                except:
                    pass
        if csv_path is None or not os.path.exists(csv_path):
            files = list_csvs()
            csv_path = files[0] if files else None
        if csv_path is None or not os.path.exists(csv_path):
            return pd.DataFrame(), "No CSV found. Upload a CSV or drop Report_*.csv in this folder.", None
        return load_from_path(csv_path)
    except Exception as e:
        return pd.DataFrame(), f"Error: {e}", None

def next_csv(current_path):
    files = list_csvs()
    if not files:
        return pd.DataFrame(), "No CSVs found", None
    if current_path in files:
        idx = files.index(current_path)
        if idx + 1 < len(files):
            nxt = files[idx + 1]
        else:
            nxt = files[0]  # wrap around
    else:
        nxt = files[0]
    return load_from_path(nxt)

def download_all(uploaded_file=None, current_path=None, progress=gr.Progress()):
    try:
        csv_path = None
        if uploaded_file is not None:
            if hasattr(uploaded_file, "name"):
                csv_path = uploaded_file.name
            elif isinstance(uploaded_file, str):
                csv_path = uploaded_file
            if csv_path and os.path.exists(csv_path):
                dest = os.path.basename(csv_path)
                try:
                    if os.path.abspath(csv_path) != os.path.abspath(dest):
                        shutil.copy2(csv_path, dest)
                        csv_path = dest
                except:
                    pass
        if csv_path is None or not os.path.exists(csv_path):
            csv_path = current_path
        if csv_path is None or not os.path.exists(csv_path):
            files = list_csvs()
            csv_path = files[0] if files else None
        if csv_path is None:
            return None, "No CSV found"
        df = pd.read_csv(csv_path, encoding="utf-8-sig")
        f = get_filtered(df, 5)
        csv_base = Path(csv_path).stem
        out_dir = Path(f"recordings_{csv_base}_gt5s")
        out_dir.mkdir(exist_ok=True)
        urls = []
        used_names = set()
        for _, row in f.iterrows():
            url = str(row["Recording URL"]).strip()
            if not url:
                continue
            fname = Path(url).name
            if not fname or fname == "":
                cid = str(row.get("Call ID", row.get("Refid", ""))).strip()
                fname = f"{cid or len(urls)}.wav"
            name = Path(fname)
            unique_name = fname
            duplicate = 2
            while unique_name in used_names:
                unique_name = f"{name.stem}_{duplicate}{name.suffix}"
                duplicate += 1
            used_names.add(unique_name)
            urls.append((url, out_dir / unique_name))
        total = len(urls)
        if total == 0:
            return None, "Nothing to download"
        session_local = local()
        sessions = []
        sessions_lock = Lock()

        def fetch(item):
            url, path = item
            if is_complete_download(path):
                return path, True, "exists"
            session = getattr(session_local, "session", None)
            if session is None:
                session = requests.Session()
                session.headers.update(HEADERS)
                session_local.session = session
                with sessions_lock:
                    sessions.append(session)
            temp_path = path.with_name(f"{path.name}.part")
            for attempt in range(DOWNLOAD_RETRIES + 1):
                try:
                    temp_path.unlink(missing_ok=True)
                    with session.get(url, timeout=60, stream=True) as response:
                        if response.status_code != 200:
                            if response.status_code == 429 or response.status_code >= 500:
                                if attempt < DOWNLOAD_RETRIES:
                                    time.sleep(min(2**attempt, 8))
                                    continue
                            return path, False, f"http{response.status_code}"
                        with temp_path.open("wb") as audio_file:
                            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                                if chunk:
                                    audio_file.write(chunk)
                    if not is_complete_download(temp_path):
                        if attempt < DOWNLOAD_RETRIES:
                            time.sleep(min(2**attempt, 8))
                            continue
                        return path, False, "invalid or incomplete audio"
                    temp_path.replace(path)
                    return path, True, "ok"
                except requests.RequestException as e:
                    if attempt == DOWNLOAD_RETRIES:
                        return path, False, str(e)
                    time.sleep(min(2**attempt, 8))
                except OSError as e:
                    return path, False, str(e)
                finally:
                    temp_path.unlink(missing_ok=True)
            return path, False, "retries exhausted"

        results = []
        done = 0
        try:
            with ThreadPoolExecutor(max_workers=min(DOWNLOAD_WORKERS, total)) as ex:
                futs = [ex.submit(fetch, u) for u in urls]
                for fut in as_completed(futs):
                    done += 1
                    try:
                        path, ok, msg = fut.result()
                        results.append((path, ok, msg))
                    except Exception:
                        results.append((None, False, "unexpected download error"))
                    progress(done/total, desc=f"Downloaded {done}/{total}")
        finally:
            for session in sessions:
                session.close()
        downloaded_paths = [path for path, ok, _ in results if ok and path is not None]
        failed = [
            (path.name if path is not None else "unknown", message)
            for path, ok, message in results
            if not ok
        ]
        zip_base = f"recordings_{csv_base}_gt5s"
        zip_paths, oversized_files = write_zip_parts(downloaded_paths, zip_base)
        status = (
            f"Available {len(downloaded_paths)}/{total} recordings from "
            f"{os.path.basename(csv_path)} in {len(zip_paths)} ZIP part(s). Folder: {out_dir}"
        )
        if failed:
            examples = "; ".join(f"{name}: {reason}" for name, reason in failed[:3])
            status += f" | Failed: {len(failed)} (first: {examples})"
        if oversized_files:
            status += (
                f" | Excluded {len(oversized_files)} recording(s) larger than the "
                f"{ZIP_PART_MAX_BYTES // 1024**3} GiB ZIP-part limit"
            )
        if zip_paths:
            status += " | ZIP parts: " + ", ".join(path.name for path in zip_paths)
        return [str(path) for path in zip_paths], status
    except Exception as e:
        return None, f"Error: {e}"

with gr.Blocks(title="MCube Call Recordings Downloader") as demo:
    gr.Markdown("# MCube Call Recordings Downloader\nUpload CSV or switch between CSVs. Downloads resume from existing files, retry temporary failures, and split ZIPs into parts under 2 GiB.")
    with gr.Row():
        csv_upload = gr.File(label="Upload CSV (Report_*.csv)", file_types=[".csv"])
    with gr.Row():
        load_btn = gr.Button("Scan Current/Latest CSV", variant="secondary")
        next_btn = gr.Button("Next CSV ▶", variant="secondary")
    status = gr.Textbox(label="Status", interactive=False)
    csv_path_state = gr.State(value=None)
    df_out = gr.Dataframe(label="Recordings > 5s (preview)", wrap=True, interactive=False, max_height=400)
    dl_btn = gr.Button("Download All (>5s)", variant="primary")
    zip_out = gr.Files(label="Download ZIP parts")
    dl_status = gr.Textbox(label="Download Status", interactive=False)
    def _load(f):
        df, msg, p = load_csv(f)
        return df, msg, p
    def _next(p):
        df, msg, np = next_csv(p)
        return df, msg, np
    load_btn.click(_load, inputs=[csv_upload], outputs=[df_out, status, csv_path_state])
    next_btn.click(_next, inputs=[csv_path_state], outputs=[df_out, status, csv_path_state])
    demo.load(_load, inputs=[csv_upload], outputs=[df_out, status, csv_path_state])
    dl_btn.click(download_all, inputs=[csv_upload, csv_path_state], outputs=[zip_out, dl_status])

if __name__ == "__main__":
    demo.launch(server_name="127.0.0.1", server_port=7861, share=False)
