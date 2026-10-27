#!/usr/bin/env python3
"""
Local video browser for videos_catalog.duckdb.

Serves a web page listing a normalized set of all videos (Insta360 lens pairs,
sidecars and proxies collapsed into one logical clip), grouped by capture date.
Data is read from the DuckDB file LIVE on every request, so the page always
reflects the current catalog.

  * Filter by drive / collection (storage_root) - drives that aren't plugged
    in are shown as offline.
  * Every clip gets a time: from the filename where it has one (VID_, LRV_,
    PXL_, DJI_ ...), otherwise from the file's modified time when that falls on
    the capture date.
  * Filename column, plus Play (default player) and Show in folder (Explorer).
  * NAS-backup and duplicate flags; project notes from the `projects` table.
  * In-browser preview. Plays the lightest playable file of a clip (proxy ->
    LRV/sidecar -> original; N-RAW .NEV is never used). Files the browser
    can't decode (DNxHR proxies, etc.) are converted once with ffmpeg to a
    small H.264 copy, cached in %LOCALAPPDATA%\\VideoBrowser\\previews.

Run:   python video_browser.py
Open:  http://127.0.0.1:8000   (Ctrl+C to stop)
Dep:   pip install "duckdb>=2.0"   (the catalog is in DuckDB 2 format)
       ffmpeg + ffprobe on PATH for converted previews (winget install Gyan.FFmpeg)

Config (all optional, environment variables):
  VB_DB           catalog path (default: videos_catalog.duckdb here or one level up)
  VB_HOST/VB_PORT listen address (default 127.0.0.1:8000)
  VB_CACHE        preview cache folder
  VB_ROOT_LABELS  "H:/=H: Processed;F:/=F: Raw"  display names for storage roots
  VB_PATH_MAP     "H:/=/media/1;F:/=/media/2"    where catalog paths are mounted
"""
import json, os, re, sys, datetime, subprocess, hashlib, shutil, threading, tempfile, contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    import duckdb
except ImportError:
    sys.exit('Missing dependency. Run:  pip install "duckdb>=2.0"')

HERE = os.path.dirname(os.path.abspath(__file__))
# The catalog sits next to this script or one folder up.
DB_PATH = os.environ.get("VB_DB") or next(
    (p for p in (os.path.join(HERE, "videos_catalog.duckdb"),
                 os.path.join(os.path.dirname(HERE), "videos_catalog.duckdb"))
     if os.path.exists(p)), os.path.join(os.path.dirname(HERE), "videos_catalog.duckdb"))
HOST = os.environ.get("VB_HOST", "127.0.0.1")
PORT = int(os.environ.get("VB_PORT", "8000"))

def _pairs(var):
    """Parse "A=B;C=D" from an environment variable."""
    return [tuple(x.split("=", 1)) for x in os.environ.get(var, "").split(";") if "=" in x]


# Friendly names for storage roots, e.g. VB_ROOT_LABELS="H:/=H: Processed;F:/=F: Raw"
ROOT_LABELS = dict(_pairs("VB_ROOT_LABELS"))

# Catalog paths are Windows drive paths; in a container they're mounted elsewhere.
# VB_PATH_MAP="H:/=/media/1;F:/=/media/2"
PATH_MAP = _pairs("VB_PATH_MAP")

# Preview cache lives outside synced folders (OneDrive, Dropbox...) by default.
CACHE_DIR = os.environ.get("VB_CACHE") or os.path.join(
    os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "VideoBrowser", "previews")
CACHE_MAX_BYTES = 20 * 1024**3          # oldest previews are pruned beyond 20 GB
MAX_JOBS = 2                            # concurrent ffmpeg conversions
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0   # no console pop-ups
DIRECT_CODECS = {"h264", "vp9", "av1"}  # every Chromium/Edge plays these
NEVER_PREVIEW = {".nev", ".gyro"}       # N-RAW and gyro data can't be decoded

FILES_SQL = """
SELECT filename, ext, kind, device, capture_date, size_bytes, modified,
       rel_path, location, project, collection, storage_root, nas_path, dup_of
FROM videos
ORDER BY capture_date DESC NULLS LAST, rel_path
"""

# Insta360 originals, lens pairs and LRV sidecars share date_time_seq.
INSTA_RE = re.compile(r"^(?:VID|IMG|LRV)_(\d{8})_(\d{6})_(?:\d{2}_)?(\d+)", re.I)
# Any filename carrying YYYYMMDD[_-]HHMMSS (VID_, PXL_, DJI_, ...).
TIME_RE = re.compile(r"(?:19|20)\d{6}[_-]?([0-2]\d)([0-5]\d)([0-5]\d)")


def local_path(p):
    """Translate a catalog path (H:/...) to where it is mounted here, per VB_PATH_MAP."""
    if not p:
        return p
    norm = p.replace("\\", "/")
    for src, dst in PATH_MAP:
        if norm.upper().startswith(src.upper()):
            return dst.rstrip("/") + "/" + norm[len(src):].lstrip("/")
    return p


def clip_key(fn):
    m = INSTA_RE.match(fn)
    if m:
        return f"{m.group(1)}_{m.group(2)}_{m.group(3)}"
    return os.path.splitext(fn)[0].upper()


def clip_time(primary, capture_date, files):
    m = TIME_RE.search(primary["filename"])
    if m:
        return f"{m.group(1)}:{m.group(2)}:{m.group(3)}", "filename"
    # Fall back to modified time, but only when it is on the capture date -
    # otherwise it is a copy/export time, not when the clip was shot.
    mods = [f["modified"] for f in files
            if f["modified"] and capture_date and f["modified"].date() == capture_date]
    if mods:
        return min(mods).strftime("%H:%M:%S"), "modified"
    return "", ""


def preview_candidates(files):
    """Files of a clip worth previewing, lightest-first: proxy, sidecar, original."""
    def rank(f):
        if (f["ext"] or "").lower() in NEVER_PREVIEW:
            return None
        return {"proxy": 0, "sidecar": 1, "video": 2}.get(f["kind"])
    ranked = [(rank(f), f["size_bytes"] or 0, f["rel_path"]) for f in files]
    return [rel for r, _, rel in sorted(x for x in ranked if x[0] is not None)]


# DuckDB won't hold read-only and read-write handles to one file in the same
# process, so connections are opened one at a time. Each is short-lived, which
# also keeps the file free for other programs between requests.
_db_lock = threading.Lock()


@contextlib.contextmanager
def db(read_only=True):
    with _db_lock:
        con = duckdb.connect(DB_PATH, read_only=read_only)
        try:
            yield con
        finally:
            con.close()


def load_projects(con):
    try:
        rows = con.execute(
            "SELECT project, resolve_project, editing_media, notes FROM projects").fetchall()
    except duckdb.Error:
        return {}
    return {p: {"resolve": r, "media": m, "notes": n} for p, r, m, n in rows}


def load_data():
    with db() as con:
        cur = con.execute(FILES_SQL)
        cols = [d[0] for d in cur.description]
        files = [dict(zip(cols, r)) for r in cur.fetchall()]
        projects = load_projects(con)

    clips = {}
    roots = {}
    for f in files:
        root = f["storage_root"] or ""
        r = roots.setdefault(root, {"root": root, "collection": f["collection"],
                                    "label": ROOT_LABELS.get(root, root), "files": 0, "bytes": 0})
        r["files"] += 1
        r["bytes"] += f["size_bytes"] or 0
        k = (f["capture_date"], f["device"], root, clip_key(f["filename"]))
        clips.setdefault(k, []).append(f)

    groups = {}
    for (d, device, root, key), fs in clips.items():
        videos = [f for f in fs if f["kind"] == "video"]
        if not videos:
            continue  # orphan sidecars/proxies aren't clips
        primary = max(videos, key=lambda f: f["size_bytes"] or 0)
        t, tsrc = clip_time(primary, d, fs)
        ds = d.isoformat() if d else "unknown"
        g = groups.setdefault(ds, {"date": ds, "clips": []})
        loc = next((f["location"] for f in fs if f["location"]), None)
        proj = next((f["project"] for f in fs if f["project"]), None)
        g["clips"].append({
            "time": t, "time_src": tsrc, "device": device or "", "root": root,
            "collection": primary["collection"], "location": loc, "project": proj,
            "file": primary["filename"], "rel": primary["rel_path"],
            "rels": [f["rel_path"] for f in fs],
            "folder": os.path.dirname(primary["rel_path"] or ""),
            "files": len(fs), "video_files": len(videos),
            "all": sorted(f["filename"] + ("" if f["kind"] == "video" else f"  ({f['kind']})")
                          for f in fs),
            "size": sum(f["size_bytes"] or 0 for f in fs),
            "nas": all(f["nas_path"] for f in fs),
            "dup": next((f["dup_of"] for f in fs if f["dup_of"]), None),
            "pv": preview_candidates(fs),
        })

    out = []
    for g in groups.values():
        try:
            g["weekday"] = datetime.date.fromisoformat(g["date"]).strftime("%a")
        except ValueError:
            g["weekday"] = ""
        g["clips"].sort(key=lambda c: (c["time"] or "99", c["device"], c["file"]))
        out.append(g)
    out.sort(key=lambda x: x["date"], reverse=True)

    for r in roots.values():
        r["online"] = bool(r["root"]) and os.path.isdir(local_path(r["root"]))
    return {"summary": {"db": DB_PATH, "rows": len(files),
                        "loaded": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            "roots": sorted(roots.values(), key=lambda r: r["label"]),
            "projects": projects, "groups": out}


def lookup_path(rel):
    with db() as con:
        row = con.execute("SELECT full_path FROM videos WHERE rel_path = ?", [rel]).fetchone()
    return local_path(row[0]) if row else None


def lookup_paths(rels):
    rels = [r for r in rels if isinstance(r, str)][:20]
    if not rels:
        return {}
    with db() as con:
        rows = con.execute(
            f"SELECT rel_path, full_path FROM videos WHERE rel_path IN ({','.join('?' * len(rels))})",
            rels).fetchall()
    return {rel: local_path(full) for rel, full in rows}


EDITABLE = ("project", "location")


def update_clips(rels, changes):
    """Set project and/or location on catalog rows. A blank value clears the field."""
    rels = sorted({r for r in rels if isinstance(r, str)})
    if not rels:
        return {"ok": False, "error": "Nothing selected."}
    sets, args = [], []
    for col in EDITABLE:
        if col in changes:
            v = changes[col].strip()[:200] if isinstance(changes[col], str) else ""
            sets.append(f"{col} = ?")
            args.append(v or None)
    if not sets:
        return {"ok": False, "error": "Tick Project and/or Location to change."}
    try:
        with db(read_only=False) as con:
            n = con.execute(f"UPDATE videos SET {', '.join(sets)} WHERE list_contains(?, rel_path)",
                            args + [rels]).fetchone()[0]
    except duckdb.Error as e:
        return {"ok": False, "error": "Couldn't save - the catalog may be open in another program "
                f"or mounted read-only.\n{e}"}
    return {"ok": True, "updated": n}


# ---------------------------------------------------------------- previews --

_probe_cache = {}
_jobs = {}            # cache key -> {"state", "progress", "error"}
_jobs_lock = threading.Lock()
_job_slots = threading.Semaphore(MAX_JOBS)


def probe(path):
    """(video codec, duration seconds) via ffprobe; cached per file version."""
    st = os.stat(path)
    k = (path, st.st_size, st.st_mtime)
    if k in _probe_cache:
        return _probe_cache[k]
    codec, dur = None, 0.0
    if FFPROBE:
        try:
            out = subprocess.run(
                [FFPROBE, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name:format=duration", "-of", "json", path],
                capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW).stdout
            j = json.loads(out or "{}")
            codec = (j.get("streams") or [{}])[0].get("codec_name")
            dur = float((j.get("format") or {}).get("duration") or 0)
        except (subprocess.SubprocessError, ValueError, OSError):
            pass
    _probe_cache[k] = (codec, dur)
    return codec, dur


def cache_key(path):
    st = os.stat(path)
    return hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()[:20]


def prune_cache():
    try:
        files = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR) if f.endswith(".mp4")]
        files.sort(key=os.path.getmtime)
        total = sum(os.path.getsize(f) for f in files)
        while files and total > CACHE_MAX_BYTES:
            f = files.pop(0)
            total -= os.path.getsize(f)
            os.remove(f)
    except OSError:
        pass


def transcode(src, key, duration):
    job = _jobs[key]
    out = os.path.join(CACHE_DIR, key + ".mp4")
    tmp = out + ".part"
    with _job_slots:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            cmd = [FFMPEG, "-hide_banner", "-nostats", "-y", "-i", src,
                   "-map", "0:v:0", "-map", "0:a:0?",
                   "-vf", "scale=-2:'min(540,ih)'", "-c:v", "libx264", "-preset", "veryfast",
                   "-crf", "26", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ac", "2",
                   "-movflags", "+faststart", "-f", "mp4", "-progress", "pipe:1", tmp]
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, creationflags=NO_WINDOW)
            for line in p.stdout:
                if line.startswith("out_time_us=") and duration:
                    try:
                        job["progress"] = min(0.99, int(line.split("=")[1]) / 1e6 / duration)
                    except ValueError:
                        pass
            err = p.stderr.read()
            if p.wait() != 0:
                raise RuntimeError(err.strip().splitlines()[-1] if err.strip() else "ffmpeg failed")
            os.replace(tmp, out)
            job.update(state="ready", progress=1.0)
            prune_cache()
        except Exception as e:  # noqa: BLE001 - report any failure to the page
            job.update(state="error", error=f"Conversion failed: {e}")
            try:
                os.remove(tmp)
            except OSError:
                pass


def prepare_preview(cands, hevc_ok, force):
    """Pick the clip file to preview and say how to play it."""
    paths = lookup_paths(cands)
    src_rel = next((r for r in cands if r in paths and os.path.exists(paths[r])), None)
    if not src_rel:
        known = [paths[r] for r in cands if r in paths]
        drive = known[0][:2] if known and known[0][1:2] == ":" else "the drive"
        return {"state": "error", "error": f"File not found - is {drive} connected?"}
    src = os.path.normpath(paths[src_rel])
    name = os.path.basename(src)
    codec, duration = probe(src)
    direct = codec in DIRECT_CODECS or (codec == "hevc" and hevc_ok) or (codec is None and not FFMPEG)
    if direct and not force:
        return {"state": "ready", "url": "/media?rel=" + _q(src_rel), "source": name,
                "how": f"direct ({codec or 'unknown codec'})"}
    if not FFMPEG:
        return {"state": "error", "error": f"{name} is {codec or 'an unsupported format'}, which the "
                "browser can't play. Install ffmpeg (winget install Gyan.FFmpeg) and restart "
                "the browser script to enable converted previews."}
    key = cache_key(src)
    if os.path.exists(os.path.join(CACHE_DIR, key + ".mp4")):
        return {"state": "ready", "url": f"/preview/{key}.mp4", "source": name,
                "how": f"converted from {codec or 'source'}"}
    with _jobs_lock:
        job = _jobs.get(key)
        # (a "ready" job whose file is gone was pruned from the cache - redo it)
        if not job or job["state"] == "ready" or (job["state"] == "error" and force):
            job = _jobs[key] = {"state": "working", "progress": 0.0, "error": None}
            threading.Thread(target=transcode, args=(src, key, duration), daemon=True).start()
    if job["state"] == "error":
        return {"state": "error", "error": job["error"]}
    if job["state"] == "ready":
        return {"state": "ready", "url": f"/preview/{key}.mp4", "source": name,
                "how": f"converted from {codec or 'source'}"}
    return {"state": "working", "progress": job["progress"], "source": name,
            "how": f"converting {codec or 'source'} to H.264"}


def _q(s):
    from urllib.parse import quote
    return quote(s, safe="")


def open_file(rel, mode):
    full = lookup_path(rel)
    if not full:
        return {"ok": False, "error": "Not in the catalog."}
    path = os.path.normpath(full)
    if not os.path.exists(path):
        drive = full[:2] if full[1:2] == ":" else (os.path.splitdrive(path)[0] or "the drive")
        return {"ok": False, "error": f"File not found - is {drive} connected?\n{path}"}
    if sys.platform.startswith("win"):
        if mode == "play":
            os.startfile(path)
        else:
            subprocess.Popen(["explorer", f"/select,{path}"])
    elif sys.platform != "darwin" and not shutil.which("xdg-open"):
        return {"ok": False, "error": "Opening files isn't available when running in a container - "
                "use the preview instead."}
    else:
        target = path if mode == "play" else os.path.dirname(path)
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", target])
    return {"ok": True}


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Video Catalog</title>
<style>
  :root{--bg:#0f1115;--panel:#171a21;--panel2:#1e222b;--line:#2a2f3a;--txt:#e6e9ef;
    --muted:#98a2b3;--accent:#6ea8fe;--chip:#242a35;--ok:#4cc38a;--warn:#f0b44c;--bad:#f07171}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  header{position:sticky;top:0;z-index:5;background:#12151bee;border-bottom:1px solid var(--line);
    padding:16px 22px;backdrop-filter:blur(6px)}
  h1{margin:0 0 4px;font-size:18px;font-weight:650}
  .sub{color:var(--muted);font-size:12.5px}
  .stats{display:flex;gap:18px;flex-wrap:wrap;margin-top:10px}
  .stat b{font-size:17px;font-variant-numeric:tabular-nums}.stat span{color:var(--muted);font-size:12px;margin-left:5px}
  .toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:14px}
  input[type=search]{flex:1;min-width:220px;background:var(--panel2);border:1px solid var(--line);
    color:var(--txt);border-radius:9px;padding:9px 12px;font-size:13.5px;outline:none}
  input[type=search]:focus{border-color:var(--accent)}
  button{background:var(--chip);border:1px solid var(--line);color:var(--txt);
    border-radius:9px;padding:8px 12px;font-size:12.5px;cursor:pointer}
  button:hover{border-color:var(--accent)}
  .seg{display:inline-flex;border:1px solid var(--line);border-radius:9px;overflow:hidden}
  .seg button{border:0;border-radius:0;border-right:1px solid var(--line);background:var(--panel2)}
  .seg button:last-child{border-right:0}
  .seg button.on{background:#25344f;color:#cfe0ff}
  .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;vertical-align:1px}
  .dot.on{background:var(--ok)}.dot.off{background:var(--muted);opacity:.5}
  main{padding:16px 22px 60px;max-width:1280px}
  .grp>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:14px;padding:10px 6px;border-radius:9px}
  .grp>summary::-webkit-details-marker{display:none}
  .grp>summary:hover{background:var(--panel2)}
  .grp.yr{margin-bottom:6px}
  .grp.yr>summary .date{font-size:17px;font-weight:700}
  .grp.mo{margin-left:18px}
  .grp.mo>summary .date{font-weight:600}
  .grp>.kids{padding:2px 0 8px}
  .grp.mo>.kids{margin-left:18px}
  .day{border:1px solid var(--line);border-radius:12px;margin-bottom:10px;overflow:hidden;background:var(--panel)}
  .day>summary{list-style:none;cursor:pointer;padding:13px 16px;display:flex;align-items:center;gap:14px}
  .day>summary::-webkit-details-marker{display:none}
  .day>summary:hover{background:var(--panel2)}
  .caret{transition:transform .15s;color:var(--muted)}
  details[open]>summary .caret{transform:rotate(90deg)}
  .date{font-weight:640;min-width:118px}
  .wd{color:var(--muted);font-weight:400;font-size:12px;margin-left:6px}
  .count{color:var(--accent);white-space:nowrap}
  .spacer{flex:1}
  .chips{display:flex;gap:6px;flex-wrap:wrap}
  .chip{background:var(--chip);border:1px solid var(--line);color:var(--muted);
    font-size:11px;padding:2px 8px;border-radius:999px;white-space:nowrap}
  .sz{color:var(--muted);font-variant-numeric:tabular-nums;min-width:78px;text-align:right}
  .tw{overflow-x:auto}
  table{width:100%;border-collapse:collapse;font-size:13px}
  tbody tr{border-top:1px solid var(--line)}
  tbody tr:hover{background:#1b1f27}
  th,td{text-align:left;padding:7px 12px;vertical-align:middle}
  th:first-child,td:first-child{padding-left:16px}
  thead th{color:var(--muted);font-weight:500;font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;white-space:nowrap}
  td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
  td.t{font-variant-numeric:tabular-nums;white-space:nowrap}
  td.t.mod{color:var(--muted);font-style:italic}
  .fn{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;white-space:nowrap}
  .tag{background:#20304a;color:#bcd3ff;border-radius:6px;padding:1px 7px;font-size:11.5px;white-space:nowrap}
  .tag.pj{background:#2a2440;color:#d7c6ff}
  .tag.pj[title]{cursor:help;text-decoration:underline dotted #8c7cc0}
  .badge{font-size:10.5px;border-radius:5px;padding:1px 6px;margin-left:6px;white-space:nowrap}
  .badge.nas{color:var(--ok);border:1px solid #2c5a45}
  .badge.nonas{color:var(--warn);border:1px solid #6a5426}
  .badge.dup{color:var(--bad);border:1px solid #6a3030}
  .drv{color:var(--muted);font-size:12px;white-space:nowrap}
  .act{display:flex;gap:6px;justify-content:flex-end}
  .act button{padding:4px 8px;font-size:12px;line-height:1}
  .muted{color:var(--muted)}
  .hide{display:none!important}
  .empty{color:var(--muted);padding:30px;text-align:center}
  #toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:#3a2026;
    border:1px solid #6a3030;color:#ffd7d7;padding:10px 14px;border-radius:9px;font-size:12.5px;
    white-space:pre-line;max-width:90vw;display:none;z-index:20}
  #toast.ok{background:#1d3328;border-color:#2c5a45;color:#d2f5e3}
  th.sel,td.sel{width:1%;padding-right:0}
  input[type=checkbox]{accent-color:var(--accent);cursor:pointer}
  tbody tr.on{background:#1c2638}
  #selbar{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:15;display:none;
    align-items:center;gap:10px;flex-wrap:wrap;background:var(--panel2);border:1px solid #36507a;
    border-radius:12px;padding:10px 14px;box-shadow:0 10px 40px #000a;max-width:calc(100vw - 32px)}
  body.selecting #selbar{display:flex}
  body.selecting main{padding-bottom:130px}
  #selbar label{color:var(--muted);font-size:12.5px;display:flex;align-items:center;gap:5px;white-space:nowrap}
  #selbar input[type=text]{background:var(--bg);border:1px solid var(--line);color:var(--txt);border-radius:8px;
    padding:6px 9px;font-size:13px;width:170px;outline:none}
  #selbar input[type=text]:focus{border-color:var(--accent)}
  #selbar input[type=text]:disabled{opacity:.4}
  #selbar button.go{background:#25344f;border-color:#36507a;color:#cfe0ff}
  .act button.pv{color:#cfe0ff;border-color:#36507a;background:#1f2b40}
  .act button:disabled{opacity:.35;cursor:not-allowed;border-color:var(--line)}
  #pv{position:fixed;inset:0;background:#05070bdd;z-index:30;display:none;align-items:center;justify-content:center;padding:24px}
  #pv.open{display:flex}
  #pv .box{background:var(--panel);border:1px solid var(--line);border-radius:14px;width:min(1100px,100%);
    max-height:100%;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 20px 60px #000a}
  #pv .bar{display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--line)}
  #pv .ttl{font-weight:600}#pv .ttl .fn{margin-left:4px}
  #pv .how{color:var(--muted);font-size:12px}
  #pv .stage{background:#000;display:flex;align-items:center;justify-content:center;min-height:300px;position:relative}
  #pv video{width:100%;max-height:calc(100vh - 160px);display:block;background:#000}
  #pv .msg{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
    gap:12px;color:var(--muted);text-align:center;padding:30px;white-space:pre-line}
  #pv .msg.err{color:#ffd7d7}
  #pv .prog{width:260px;height:6px;border-radius:3px;background:var(--line);overflow:hidden}
  #pv .prog i{display:block;height:100%;width:0;background:var(--accent);transition:width .4s}
  #pv .nav{display:flex;gap:6px}
  footer{color:var(--muted);font-size:11.5px;padding:0 22px 40px}
  code{background:var(--panel2);padding:1px 6px;border-radius:5px}
</style></head>
<body>
<header>
  <h1>Video Catalog <span class="sub">- normalized, collapsed by date</span></h1>
  <div class="sub" id="dbline"></div>
  <div class="stats" id="stats"></div>
  <div class="toolbar">
    <span class="seg" id="roots"></span>
    <input id="q" type="search" placeholder="Filter by date, file, device, location, project...">
    <button id="expand">Expand all</button>
    <button id="collapse">Collapse all</button>
    <button id="reload">&#8635; Reload</button>
  </div>
</header>
<main id="main"><div class="empty">Loading...</div></main>
<div id="toast"></div>
<div id="selbar"><b id="selcount"></b>
  <label><input type="checkbox" id="setpj"> Project</label>
  <input type="text" id="pj" list="pjlist" placeholder="blank clears it" disabled>
  <label><input type="checkbox" id="setloc"> Location</label>
  <input type="text" id="loc" list="loclist" placeholder="blank clears it" disabled>
  <button class="go" id="apply">Apply</button><button id="selnone">Clear selection</button></div>
<datalist id="pjlist"></datalist><datalist id="loclist"></datalist>
<div id="pv"><div class="box">
  <div class="bar"><span class="ttl">Preview <span class="fn" id="pvname"></span></span>
    <span class="how" id="pvhow"></span><span class="spacer"></span>
    <span class="nav"><button id="pvprev" title="Previous clip (Alt+&#8592; or PgUp)">&#8592;</button>
      <button id="pvnext" title="Next clip (Alt+&#8594; or PgDn)">&#8594;</button>
      <button id="pvopen" title="Open the original in your player">Open original</button>
      <button id="pvclose" title="Close (Esc)">&#10005;</button></span></div>
  <div class="stage"><video id="pvvid" controls playsinline preload="auto"></video>
    <div class="msg" id="pvmsg"></div></div>
</div></div>
<footer>Data read live from the DuckDB file on each reload. Times in <i>italics</i> come from the
file's modified time; the rest from the filename. Runs locally only.</footer>
<script>
let DATA=null, ROOT='all', CLIPS={}, LAST=null;
const SEL=new Set();   // primary rel_path of each selected clip
try{ROOT=localStorage.getItem('vb.root')||'all'}catch(e){}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=b=>{const mb=b/1048576;return mb>=1048576?(mb/1048576).toFixed(2)+' TB':mb>=1024?(mb/1024).toFixed(2)+' GB':Math.round(mb)+' MB'};
const rootLabel=r=>(DATA.roots.find(x=>x.root===r)||{label:r}).label;

async function load(){
  const r=await fetch('/api/videos');DATA=await r.json();
  if(DATA.error){document.getElementById('main').innerHTML='<div class="empty">'+esc(DATA.error)+'</div>';return}
  if(ROOT!=='all'&&!DATA.roots.some(x=>x.root===ROOT))ROOT='all';
  render();
}
function renderRoots(){
  const opts=[{root:'all',label:'All drives'}].concat(DATA.roots);
  document.getElementById('roots').innerHTML=opts.map(o=>
    `<button data-root="${esc(o.root)}" class="${o.root===ROOT?'on':''}" title="${o.root==='all'?'':esc(o.root)+(o.online?' - connected':' - not connected')}">`+
    (o.root==='all'?'':`<span class="dot ${o.online?'on':'off'}"></span>`)+esc(o.label)+`</button>`).join('');
  document.querySelectorAll('#roots button').forEach(b=>b.onclick=()=>{
    ROOT=b.dataset.root;try{localStorage.setItem('vb.root',ROOT)}catch(e){}
    renderRoots();applyFilter();});
}
function projTitle(p){
  const x=DATA.projects[p];if(!x)return '';
  return [x.notes,x.resolve&&('Resolve: '+x.resolve),x.media&&('Media: '+x.media)].filter(Boolean).join('\n');
}
function render(){
  const s=DATA.summary;
  document.getElementById('dbline').innerHTML='DB: <code>'+esc(s.db)+'</code> - '+s.rows+' files - loaded '+esc(s.loaded);
  renderRoots();
  const main=document.getElementById('main'),scroll=window.scrollY;
  // Keep whatever was expanded across reloads; first load opens the newest year.
  const wasOpen=new Set([...main.querySelectorAll('details[open]')].map(d=>d.dataset.key));
  main.innerHTML='';CLIPS={};
  const pjs=new Set(Object.keys(DATA.projects)),locs=new Set();
  let yEl=null,mEl=null,yKey=null,mKey=null;
  DATA.groups.forEach(g=>{
    const known=g.date!=='unknown',y=known?g.date.slice(0,4):'unknown',m=known?g.date.slice(0,7):'unknown';
    if(y!==yKey){yKey=y;mKey=null;yEl=section('yr',known?y:'No date','y'+y);main.appendChild(yEl)}
    if(m!==mKey){mKey=m;mEl=section('mo',known?new Date(+y,+m.slice(5)-1,1).toLocaleString(undefined,{month:'long'}):'No date','m'+m);
      yEl.lastElementChild.appendChild(mEl)}
    const d=document.createElement('details');d.className='day';d.dataset.date=g.date;d.dataset.key='d'+g.date;
    const rows=g.clips.map((c,i)=>{
      CLIPS[c.rel]=c;if(c.project)pjs.add(c.project);if(c.location)locs.add(c.location);
      const pt=c.project?projTitle(c.project):'',on=SEL.has(c.rel);
      const blob=[g.date,c.device,c.location,c.project,c.file,c.folder,rootLabel(c.root)].join(' ').toLowerCase();
      return `<tr class="${on?'on':''}" data-root="${esc(c.root)}" data-size="${c.size}" data-vf="${c.video_files}" data-blob="${esc(blob)}">
        <td class="sel"><input type="checkbox" data-sel="${esc(c.rel)}" ${on?'checked':''}></td>
        <td class="t ${c.time_src==='modified'?'mod':''}">${c.time||'<span class="muted">-</span>'}</td>
        <td><span class="fn" title="${esc(c.folder+'\n\n'+c.all.join('\n'))}">${esc(c.file)}</span>${
          c.dup?`<span class="badge dup" title="Duplicate of ${esc(c.dup)}">dup</span>`:''}</td>
        <td>${esc(c.device)}</td>
        <td class="drv">${esc(rootLabel(c.root))}${c.nas?'<span class="badge nas" title="Backed up to NAS">NAS</span>':'<span class="badge nonas" title="No NAS copy recorded">no NAS</span>'}</td>
        <td>${c.location?`<span class="tag">${esc(c.location)}</span>`:'<span class="muted">-</span>'}</td>
        <td>${c.project?`<span class="tag pj"${pt?` title="${esc(pt)}"`:''}>${esc(c.project)}</span>`:'<span class="muted">-</span>'}</td>
        <td class="num muted">${c.files}</td><td class="num">${fmt(c.size)}</td>
        <td><div class="act"><button class="pv" data-pv="${g.date}|${i}" ${c.pv.length?'title="Preview in the browser"':'disabled title="Nothing previewable - N-RAW (.NEV) without a proxy"'}>&#128065;</button>
          <button data-rel="${esc(c.rel)}" data-mode="play" title="Open in your default player">&#9654;</button>
          <button data-rel="${esc(c.rel)}" data-mode="folder" title="Show in folder">&#128193;</button></div></td></tr>`}).join('');
    d.innerHTML=`<summary><span class="caret">&#9654;</span>
      <span class="date">${g.date}<span class="wd">${g.weekday}</span></span>
      <span class="count"></span><span class="chips"></span><span class="spacer"></span>
      <span class="sz"></span></summary>
      <div class="tw"><table><thead><tr><th class="sel"><input type="checkbox" class="selall" title="Select every shown clip on this day"></th><th>Time</th><th>File</th><th>Device</th><th>Drive</th><th>Location</th>
        <th>Project</th><th class="num">Files</th><th class="num">Size</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table></div>`;
    d._clips=g.clips;
    mEl.lastElementChild.appendChild(d);
  });
  if(wasOpen.size)main.querySelectorAll('details').forEach(d=>d.open=wasOpen.has(d.dataset.key));
  else if(main.firstChild)main.firstChild.open=true;
  const opts=s=>[...s].sort((a,b)=>a.localeCompare(b)).map(v=>`<option value="${esc(v)}">`).join('');
  document.getElementById('pjlist').innerHTML=opts(pjs);
  document.getElementById('loclist').innerHTML=opts(locs);
  for(const r of [...SEL])if(!CLIPS[r])SEL.delete(r);
  applyFilter();updateSel();
  window.scrollTo(0,scroll);
}
function section(cls,label,key){
  const s=document.createElement('details');s.className='grp '+cls;s.dataset.key=key;
  s.innerHTML=`<summary><span class="caret">&#9654;</span><span class="date">${esc(label)}</span>
    <span class="count"></span><span class="spacer"></span><span class="sz"></span></summary><div class="kids"></div>`;
  return s;
}
function applyFilter(){
  const q=document.getElementById('q').value.trim().toLowerCase();
  let dates=0,clips=0,vfiles=0,bytes=0;
  document.querySelectorAll('.day').forEach(day=>{
    let shown=0,sz=0;const devs=new Set();
    day.querySelectorAll('tbody tr').forEach((tr,i)=>{
      const hit=(ROOT==='all'||tr.dataset.root===ROOT)&&(!q||tr.dataset.blob.includes(q));
      tr.classList.toggle('hide',!hit);
      if(hit){shown++;sz+=+tr.dataset.size;vfiles+=+tr.dataset.vf;devs.add(day._clips[i].device)}});
    day.classList.toggle('hide',shown===0);
    day._shown=shown;day._sz=sz;
    if(shown){dates++;clips+=shown;bytes+=sz}
    day.querySelector('.count').textContent=shown+' clip'+(shown===1?'':'s');
    day.querySelector('.chips').innerHTML=[...devs].sort().map(x=>`<span class="chip">${esc(x)}</span>`).join('');
    day.querySelector('.sz').textContent=fmt(sz);
    if(q) day.open=shown>0;
  });
  document.querySelectorAll('.grp').forEach(grp=>{
    let n=0,days=0,sz=0;
    grp.querySelectorAll('.day').forEach(d=>{if(d._shown){n+=d._shown;days++;sz+=d._sz}});
    grp.classList.toggle('hide',n===0);
    grp.querySelector(':scope>summary .count').textContent=
      n+' clip'+(n===1?'':'s')+' · '+days+' day'+(days===1?'':'s');
    grp.querySelector(':scope>summary .sz').textContent=fmt(sz);
    if(q) grp.open=n>0;
  });
  updateSel();
  document.getElementById('stats').innerHTML=[['Dates',dates],['Clips',clips],['Video files',vfiles],['Total',fmt(bytes)]]
    .map(([k,v])=>`<div class="stat"><b>${v}</b><span>${k}</span></div>`).join('');
}
function toast(msg,ok){const t=document.getElementById('toast');t.textContent=msg;t.className=ok?'ok':'';t.style.display='block';
  clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',6000)}
// ---- multi-select and bulk edit ----
const shownBoxes=root=>[...root.querySelectorAll('tbody tr:not(.hide) input[data-sel]')].filter(b=>b.offsetParent);
function setSel(box,on){box.checked=on;box.closest('tr').classList.toggle('on',on);
  if(on)SEL.add(box.dataset.sel);else SEL.delete(box.dataset.sel)}
function updateSel(){
  const files=[...SEL].reduce((n,r)=>n+(CLIPS[r]?CLIPS[r].rels.length:0),0);
  document.body.classList.toggle('selecting',SEL.size>0);
  document.getElementById('selcount').textContent=SEL.size+' clip'+(SEL.size===1?'':'s')+' ('+files+' files)';
  document.querySelectorAll('.day').forEach(day=>{
    const all=day.querySelector('.selall'),boxes=[...day.querySelectorAll('tbody tr:not(.hide) input[data-sel]')];
    const n=boxes.filter(b=>b.checked).length;
    all.checked=n>0&&n===boxes.length;all.indeterminate=n>0&&n<boxes.length;
  });
}
document.getElementById('main').addEventListener('click',e=>{
  const box=e.target.closest('input[data-sel]'),all=e.target.closest('input.selall');
  if(box){
    // Shift-click selects the range of visible clips since the last click.
    const vis=shownBoxes(document),a=vis.indexOf(LAST),b=vis.indexOf(box);
    if(e.shiftKey&&a>=0&&b>=0)vis.slice(Math.min(a,b),Math.max(a,b)+1).forEach(x=>setSel(x,box.checked));
    else setSel(box,box.checked);
    LAST=box;updateSel();
  }else if(all){
    all.closest('table').querySelectorAll('tbody tr:not(.hide) input[data-sel]').forEach(x=>setSel(x,all.checked));
    updateSel();
  }
});
for(const [chk,inp] of [['setpj','pj'],['setloc','loc']]){
  const c=document.getElementById(chk),i=document.getElementById(inp);
  c.onchange=()=>{i.disabled=!c.checked;if(c.checked)i.focus()};
}
function clearSel(){
  document.querySelectorAll('input[data-sel]:checked').forEach(b=>setSel(b,false));
  SEL.clear();LAST=null;updateSel();
}
document.getElementById('selnone').onclick=clearSel;
document.getElementById('apply').onclick=async()=>{
  const body={rels:[...SEL].flatMap(r=>CLIPS[r]?CLIPS[r].rels:[])};
  if(document.getElementById('setpj').checked)body.project=document.getElementById('pj').value;
  if(document.getElementById('setloc').checked)body.location=document.getElementById('loc').value;
  if(!('project' in body)&&!('location' in body))return toast('Tick Project and/or Location to change.');
  const what=['project','location'].filter(k=>k in body)
    .map(k=>body[k].trim()?`${k} to "${body[k].trim()}"`:`clear ${k}`).join(' and ');
  if(!confirm(`Set ${what} on ${SEL.size} clip${SEL.size===1?'':'s'}?`))return;
  let j;
  try{const r=await fetch('/api/update',{method:'POST',headers:{'Content-Type':'application/json','X-Video-Browser':'1'},
    body:JSON.stringify(body)});j=await r.json()}
  catch(e){j={ok:false,error:'The browser script stopped responding.'}}
  if(!j.ok)return toast(j.error||'Could not save.');
  const n=SEL.size;clearSel();
  for(const [chk,inp] of [['setpj','pj'],['setloc','loc']]){
    document.getElementById(chk).checked=false;const i=document.getElementById(inp);i.value='';i.disabled=true}
  await load();
  toast(`Updated ${n} clip${n===1?'':'s'} (${j.updated} files).`,true);
};
document.getElementById('main').addEventListener('click',async e=>{
  const b=e.target.closest('button[data-rel]');if(!b)return;
  e.preventDefault();
  const r=await fetch('/api/open',{method:'POST',headers:{'Content-Type':'application/json','X-Video-Browser':'1'},
    body:JSON.stringify({rel:b.dataset.rel,mode:b.dataset.mode})});
  const j=await r.json();if(!j.ok)toast(j.error||'Could not open the file.');
});
// ---- preview player ----
const PV={el:document.getElementById('pv'),vid:document.getElementById('pvvid'),msg:document.getElementById('pvmsg'),
  name:document.getElementById('pvname'),how:document.getElementById('pvhow'),clip:null,btn:null,token:0,forced:false};
const HEVC=(()=>{const v=document.createElement('video');
  return ['video/mp4; codecs="hvc1.1.6.L93.B0"','video/mp4; codecs="hev1.1.6.L93.B0"'].some(t=>v.canPlayType(t))})();
function pvMsg(html,err){PV.msg.className='msg'+(err?' err':'');PV.msg.innerHTML=html;PV.msg.style.display=html?'flex':'none'}
function pvStop(){PV.vid.pause();PV.vid.removeAttribute('src');PV.vid.load()}
function pvClose(){PV.token++;pvStop();PV.el.classList.remove('open')}
async function pvOpen(btn,force){
  const [date,i]=btn.dataset.pv.split('|');const g=DATA.groups.find(x=>x.date===date);const c=g.clips[+i];
  PV.clip=c;PV.btn=btn;PV.forced=!!force;const my=++PV.token;
  PV.el.classList.add('open');pvStop();
  PV.name.textContent=c.file;PV.how.textContent='';pvMsg('Finding a previewable file...');
  while(my===PV.token){
    let j;
    try{const r=await fetch('/api/preview',{method:'POST',headers:{'Content-Type':'application/json','X-Video-Browser':'1'},
      body:JSON.stringify({cands:c.pv,hevc:HEVC,force:!!force})});j=await r.json()}
    catch(e){j={state:'error',error:'The browser script stopped responding.'}}
    if(my!==PV.token)return;
    if(j.source)PV.name.textContent=j.source;
    PV.how.textContent=j.how?(j.source!==c.file?'proxy of '+c.file+' - ':'')+j.how:'';
    if(j.state==='ready'){pvMsg('');PV.vid.src=j.url;PV.vid.play().catch(()=>{});return}
    if(j.state==='error'){pvMsg(esc(j.error),true);return}
    pvMsg(`Preparing a preview the browser can play...<div class="prog"><i style="width:${Math.round((j.progress||0)*100)}%"></i></div>`+
      `<span style="font-size:12px">${Math.round((j.progress||0)*100)}% - done once, then cached</span>`);
    await new Promise(r=>setTimeout(r,1000));
  }
}
PV.vid.addEventListener('error',()=>{
  if(!PV.vid.getAttribute('src'))return;
  // Browser couldn't decode it directly (e.g. HEVC without GPU support): convert instead.
  if(!PV.forced&&PV.btn){pvOpen(PV.btn,true);return}
  pvMsg('The browser could not play this preview.',true);
});
function pvStep(d){
  if(!PV.btn)return;
  const all=[...document.querySelectorAll('.day:not(.hide) tr:not(.hide) button.pv:not(:disabled)')];
  const k=all.indexOf(PV.btn);const nb=all[k+d];
  if(nb){for(let p=nb.closest('details');p;p=p.parentElement.closest('details'))p.open=true;
    nb.scrollIntoView({block:'nearest'});pvOpen(nb)}
}
document.getElementById('pvclose').onclick=pvClose;
document.getElementById('pvprev').onclick=()=>pvStep(-1);
document.getElementById('pvnext').onclick=()=>pvStep(1);
document.getElementById('pvopen').onclick=async()=>{
  if(!PV.clip)return;
  const r=await fetch('/api/open',{method:'POST',headers:{'Content-Type':'application/json','X-Video-Browser':'1'},
    body:JSON.stringify({rel:PV.clip.rel,mode:'play'})});const j=await r.json();if(!j.ok)toast(j.error||'Could not open the file.');
};
PV.el.addEventListener('click',e=>{if(e.target===PV.el)pvClose()});
document.addEventListener('keydown',e=>{
  if(!PV.el.classList.contains('open'))return;
  if(e.key==='Escape')pvClose();
  else if(e.key==='ArrowRight'&&e.altKey||e.key==='PageDown'){e.preventDefault();pvStep(1)}
  else if(e.key==='ArrowLeft'&&e.altKey||e.key==='PageUp'){e.preventDefault();pvStep(-1)}
});
document.getElementById('main').addEventListener('click',e=>{
  const b=e.target.closest('button.pv');if(b&&!b.disabled){e.preventDefault();pvOpen(b)}
});
document.getElementById('q').addEventListener('input',applyFilter);
// Expand opens years and months only, so the result is an outline of days rather than every clip.
document.getElementById('expand').onclick=()=>document.querySelectorAll('.grp:not(.hide)').forEach(d=>d.open=true);
document.getElementById('collapse').onclick=()=>document.querySelectorAll('.grp,.day').forEach(d=>d.open=false);
document.getElementById('reload').onclick=load;
load();
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, default=str).encode(), "application/json", code)

    def _stream(self, path):
        """Serve a video file with HTTP Range support so the player can seek."""
        size = os.path.getsize(path)
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:                                   # suffix range: last N bytes
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        remaining = end - start + 1
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                while remaining > 0:
                    chunk = fh.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # player seeked or closed - normal

    def _cross_site(self):
        return self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site")

    def do_GET(self):
        try:
            u = urlparse(self.path)
            path = u.path
            if path == "/api/videos":
                self._json(load_data())
            elif path in ("/", "/index.html"):
                self._send(PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/media":
                if self._cross_site():
                    return self.send_error(403)
                rel = (parse_qs(u.query).get("rel") or [""])[0]
                full = lookup_path(rel)
                if not full or (os.path.splitext(rel)[1].lower() in NEVER_PREVIEW):
                    return self.send_error(404)
                full = os.path.normpath(full)
                if not os.path.exists(full):
                    return self.send_error(404, "Drive not connected")
                self._stream(full)
            elif re.fullmatch(r"/preview/[0-9a-f]{20}\.mp4", path):
                if self._cross_site():
                    return self.send_error(403)
                f = os.path.join(CACHE_DIR, os.path.basename(path))
                if not os.path.exists(f):
                    return self.send_error(404)
                self._stream(f)
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        # The custom header forces a CORS preflight, so other websites open in
        # your browser can't trigger file opens on this local server.
        path = urlparse(self.path).path
        if (path not in ("/api/open", "/api/preview", "/api/update")
                or self.headers.get("X-Video-Browser") != "1"):
            self.send_error(403)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            if path == "/api/preview":
                cands = req.get("cands") if isinstance(req.get("cands"), list) else []
                self._json(prepare_preview(cands, bool(req.get("hevc")), bool(req.get("force"))))
            elif path == "/api/update":
                rels = req.get("rels") if isinstance(req.get("rels"), list) else []
                changes = {k: req[k] for k in EDITABLE if k in req}
                self._json(update_clips(rels[:20000], changes))
            else:
                mode = "play" if req.get("mode") == "play" else "folder"
                self._json(open_file(str(req.get("rel", "")), mode))
        except Exception as e:
            self._json({"ok": False, "state": "error", "error": str(e)}, 500)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        sys.exit(f"Database not found next to this script: {DB_PATH}")
    if int(duckdb.__version__.split(".")[0]) < 2:
        sys.exit(f"DuckDB {duckdb.__version__} can't read this catalog (DuckDB 2 format). "
                 'Run:  pip install -U "duckdb>=2.0"')
    print(f"Video catalog  ->  http://{HOST}:{PORT}")
    print(f"Reading: {DB_PATH}\nPress Ctrl+C to stop.")
    try:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
