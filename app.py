"""
ভিডিও ডাউনলোডার — YouTube, Facebook, Instagram
চালাতে: start.bat ডাবল-ক্লিক করুন, অথবা:  py app.py
তারপর ব্রাউজারে খুলুন: http://127.0.0.1:5000

ফোন থেকে (একই WiFi):  py app.py --lan
ডেভেলপমেন্ট মোড:      py app.py --dev

কীভাবে কাজ করে:
  ডাউনলোডে চাপ দিলে ব্রাউজার সাথে সাথে ডাউনলোড শুরু করে। পেছনে yt-dlp ভিডিও আর অডিও
  টুকরো টুকরো করে দ্রুত নামায়, ffmpeg সেগুলো সাথে সাথে জোড়া লাগিয়ে একটা temp ফাইলে লেখে,
  আর ব্রাউজার সেই ফাইল তৈরি হতে হতেই পড়ে নেয়। সার্ভারের ডাউনলোড ব্রাউজারের সংযোগের উপর
  নির্ভর করে না: অ্যাপ বন্ধ হলেও, নেট কেটে গেলেও শেষ পর্যন্ত চলে; ব্রাউজারে Retry করলে বা
  "আবার সেভ করুন" চাপলে আবার নামাতে হয় না।
"""

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
import mimetypes
from collections import deque
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory

import yt_dlp

# ফোনে "Add to Home Screen" করলে অ্যাপের মতো খোলার জন্য (manifest ফাইলের ধরন)
mimetypes.add_type("application/manifest+json", ".webmanifest")

APP_DIR = Path(__file__).resolve().parent
PORT = 5000
CHUNK = 256 * 1024

# ভিডিওর তথ্য (JSON) এখানে অস্থায়ীভাবে রাখা হয়; অ্যাপ চালু হলে আগেরগুলো মুছে যায়
CACHE_DIR = Path(tempfile.gettempdir()) / "video-downloader"
shutil.rmtree(CACHE_DIR, ignore_errors=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
INFO_TTL = 45 * 60  # ভিডিওর লিংকগুলো কিছুক্ষণ পর মেয়াদোত্তীর্ণ হয়, তখন নতুন করে আনা হয়
JOBS_DIR = CACHE_DIR / "jobs"  # ডাউনলোড হতে থাকা ফাইলগুলো
KEEP_DONE = 2 * 3600  # শেষ হওয়া ফাইল ২ ঘণ্টা রাখা হয় (ব্রাউজারে আটকে গেলে আবার সেভ করার জন্য)
ACTIVE = ("starting", "downloading")

# Instagram/Facebook-এর প্রাইভেট বা লগইন-লাগা ভিডিওর জন্য (ঐচ্ছিক)
COOKIES_FILE = APP_DIR / "cookies.txt"

# ডেভেলপমেন্ট মোড:  py app.py --dev
# কোড বদলালে সার্ভার নিজে রিস্টার্ট হয়, ব্রাউজার নিজে রিফ্রেশ হয়, পুরো error কনসোলে দেখায়
DEV = "--dev" in sys.argv
BOOT_ID = uuid.uuid4().hex  # প্রতিবার সার্ভার রিস্টার্ট হলে বদলায়

LIVE_RELOAD_JS = """<script>
(() => { let v = null;
  setInterval(async () => {
    try { const t = await (await fetch('/__dev/version', {cache: 'no-store'})).text();
          if (v && t !== v) location.reload(); v = t; } catch {}
  }, 1000);
})();
</script>"""

# ভিডিও বাছাইয়ের নিয়ম: তালিকা দেখানো আর আসল ডাউনলোড একই নিয়মে চলে, তাই সাইজও মিলে যায়
# আগে সর্বোচ্চ রেজুলেশন, তারপর fps, তারপর H.264 (সব ফোন/টিভি/কম্পিউটারে চলে) পাওয়া গেলে সেটা
VIDEO_SORT = ["res", "fps", "vcodec:h264", "ext:mp4:m4a"]

# সাইটে না থাকলেও এই রেজুলেশনগুলো দেওয়া হয় (বড়টা থেকে ছোট করে বানিয়ে)
LADDER = [4320, 2160, 1440, 1080, 720, 480, 360, 240, 144]
SCALE_KBPS = {4320: 40000, 2160: 16000, 1440: 8000, 1080: 4000, 720: 2000,
              480: 1000, 360: 600, 240: 300, 144: 120}
MP3_KBPS = 320

NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

app = Flask(__name__, static_folder=None)

infos = {}      # token -> {"info", "url", "time", "json"}
jobs = {}       # job id -> অগ্রগতির তথ্য (পেজে দেখানোর জন্য)
procs = {}      # job id -> চলমান ffmpeg
part_src = {}   # গোপন id -> কোন ফরম্যাট নামাতে হবে (শুধু ffmpeg ব্যবহার করে)
lock = threading.Lock()


class _HidePolling(logging.Filter):
    """প্রতি সেকেন্ডের progress/live-reload/ভেতরের রিকোয়েস্টগুলো কনসোলে দেখাবে না"""
    def filter(self, record):
        msg = record.getMessage()
        return not any(s in msg for s in ("/__dev/version", "/api/progress/", "/api/thumb/", "/api/_part/"))


logging.getLogger("werkzeug").addFilter(_HidePolling())


def log_exc():
    if DEV:
        traceback.print_exc()


def base_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # YouTube-এর জন্য JavaScript রানটাইম দরকার (Node.js / Deno)
        "js_runtimes": {"deno": {}, "node": {}},
        "socket_timeout": 30,
        "format_sort": VIDEO_SORT,
    }
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts


def video_format_spec(h=None):
    limit = f"[height<={h}]" if h else ""
    # সবচেয়ে ভালো ভিডিও + সবচেয়ে ভালো অডিও (আলাদা থাকলে পরে জোড়া লাগানো হয়)
    return f"bv*{limit}+ba[ext=m4a]/bv*{limit}+ba/b{limit}/bv*+ba/b"


def pick(ydl, info, spec):
    """yt-dlp ডাউনলোডের সময় ঠিক যে ফরম্যাট বাছবে, সেটাই বের করে"""
    try:
        chosen = ydl._select_formats(info.get("formats") or [], ydl.build_format_selector(spec))
    except Exception:
        return None
    return chosen[0] if chosen else None


def size_of(f, duration):
    s = f.get("filesize") or f.get("filesize_approx")
    if not s and f.get("tbr") and duration:
        s = f["tbr"] * 1000 / 8 * duration  # bitrate × সময় দিয়ে আনুমানিক সাইজ
    return s or 0


def detect_platform(url, extractor=""):
    s = (url + " " + extractor).lower()
    if "youtu" in s:
        return "YouTube"
    if "instagram" in s:
        return "Instagram"
    if "facebook" in s or "fb.watch" in s or "fb.com" in s:
        return "Facebook"
    return extractor or "Other"


def fmt_size(n):
    if not n:
        return None
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def friendly_error(e):
    msg = str(e)
    low = msg.lower()
    if "login" in low or "cookies" in low or "sign in" in low or "private" in low:
        return ("এই ভিডিওটি দেখতে লগইন লাগবে (প্রাইভেট/রেস্ট্রিক্টেড)। "
                "README-তে দেওয়া নিয়মে cookies.txt ফাইল যোগ করে আবার চেষ্টা করুন।")
    if "unsupported url" in low:
        return "এই লিংকটি সাপোর্ট করে না। YouTube, Facebook বা Instagram-এর ভিডিও লিংক দিন।"
    if "unable to download webpage" in low or "timed out" in low or "getaddrinfo" in low:
        return "ইন্টারনেট সংযোগে সমস্যা। নেট চেক করে আবার চেষ্টা করুন।"
    if "requested format is not available" in low:
        return "এই রেজুলেশনটি পাওয়া যাচ্ছে না। অন্য একটি রেজুলেশন বেছে নিন।"
    return "সমস্যা হয়েছে: " + msg.replace("ERROR: ", "")[:400]


def drop_connection(sock):
    """সংযোগ হঠাৎ কেটে দেয়, যাতে অপর পক্ষ (ব্রাউজার/ffmpeg) বোঝে ফাইলটা অসম্পূর্ণ"""
    if sock:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def safe_filename(name, ext):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")[:150] or "video"
    return f"{name}.{ext}"


def content_disposition(filename):
    ascii_name = re.sub(r"[^A-Za-z0-9 ._()\[\]-]", "", filename).strip() or "video" + Path(filename).suffix
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(filename)}"


# ---------------------------------------------------------------- ভিডিওর তথ্য

def extract(url):
    with yt_dlp.YoutubeDL(base_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist" and info.get("entries"):
        info = next((x for x in info["entries"] if x), info)
    return info


def cleanup():
    now = time.time()
    with lock:
        for tok, e in list(infos.items()):
            if now - e["time"] > INFO_TTL * 2:
                if e.get("json"):
                    Path(e["json"]).unlink(missing_ok=True)
                del infos[tok]
        for jid, j in list(jobs.items()):
            if j["status"] in ACTIVE:
                continue
            # শেষ হওয়া ফাইল কিছুক্ষণ রাখি, যাতে ব্রাউজারে আটকে গেলে আবার সেভ করা যায়
            keep = KEEP_DONE if j["status"] == "done" else 0
            if now - j.get("finished", j["created"]) > keep:
                shutil.rmtree(JOBS_DIR / jid, ignore_errors=True)
                if not (JOBS_DIR / jid).exists():
                    del jobs[jid]


def keep_awake():
    """ডাউনলোড চলার সময় কম্পিউটার যেন sleep-এ না যায় (ফোন থেকে নামানোর সময় জরুরি)"""
    if sys.platform != "win32":
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    while True:
        active = any(j["status"] in ACTIVE for j in list(jobs.values()))
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if active else 0))
        time.sleep(15)


threading.Thread(target=keep_awake, daemon=True).start()


def remember(info, url):
    cleanup()
    token = uuid.uuid4().hex
    with lock:
        infos[token] = {"info": info, "url": url, "time": time.time(), "json": None}
    return token


def get_entry(token, url):
    """আগে আনা তথ্য থাকলে সেটা, মেয়াদ শেষ হলে নতুন করে আনে"""
    e = infos.get(token)
    if e and time.time() - e["time"] < INFO_TTL:
        return e
    if not url.startswith(("http://", "https://")):
        raise RuntimeError("ভিডিওর তথ্য পাওয়া যায়নি। পেজ রিফ্রেশ করে আবার চেষ্টা করুন।")
    info = extract(url)
    return infos[remember(info, url)]


def info_json(entry):
    """yt-dlp যাতে আবার পুরো তথ্য না আনে, তাই তথ্যটা ফাইলে রেখে দিই"""
    with lock:
        if not entry["json"]:
            p = CACHE_DIR / f"{uuid.uuid4().hex}.info.json"
            p.write_text(json.dumps(yt_dlp.YoutubeDL.sanitize_info(entry["info"])), encoding="utf-8")
            entry["json"] = str(p)
        return entry["json"]


# ---------------------------------------------------------------- pages

@app.get("/")
def index():
    if DEV:
        html = (APP_DIR / "static" / "index.html").read_text(encoding="utf-8")
        html = html.replace("</body>", LIVE_RELOAD_JS + "\n</body>")
        return html, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}
    return send_from_directory(APP_DIR / "static", "index.html")


@app.get("/manifest.webmanifest")
def manifest():
    # ফোনের হোম স্ক্রিনে অ্যাপের নাম, আইকন আর রং এখান থেকে নেয়
    return send_from_directory(APP_DIR / "static", "manifest.webmanifest", max_age=0)


@app.get("/icons/<path:name>")
def icons(name):
    return send_from_directory(APP_DIR / "static" / "icons", name, max_age=86400)


if DEV:
    @app.get("/__dev/version")
    def dev_version():
        files = [f for f in (APP_DIR / "static").rglob("*") if f.is_file()]
        mtime = max((f.stat().st_mtime for f in files), default=0)
        return f"{BOOT_ID}-{mtime}", 200, {"Cache-Control": "no-store"}


# ---------------------------------------------------------------- api

@app.post("/api/info")
def api_info():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify(error="সঠিক ভিডিও লিংক দিন (https:// দিয়ে শুরু)।"), 400

    try:
        info = extract(url)
        duration = info.get("duration") or 0
        formats = info.get("formats") or []
        native = []
        with yt_dlp.YoutubeDL(base_opts()) as ydl:
            heights = sorted({f["height"] for f in formats
                              if f.get("height") and f.get("vcodec") not in (None, "none")}, reverse=True)
            seen = set()
            for h in heights:
                c = pick(ydl, info, video_format_spec(h))
                if not c or not c.get("height") or c["height"] in seen:
                    continue
                seen.add(c["height"])
                parts = c.get("requested_formats") or [c]
                total = sum(size_of(f, duration) for f in parts)
                vpart = next((f for f in parts if f.get("vcodec") not in (None, "none")), c)
                native.append({
                    "height": c["height"],
                    # লম্বালম্বি (vertical) ভিডিওতে ছোট দিকটাই আসল "p" মান (যেমন 1080x1920 = 1080p)
                    "p": min(c["height"], c["width"]) if c.get("width") else c["height"],
                    "fps": vpart.get("fps"),
                    "size": fmt_size(total) if total else None,
                    "scale": None,
                })
    except Exception as e:
        log_exc()
        return jsonify(error=friendly_error(e)), 400

    # সাইটে নেই এমন ছোট রেজুলেশনগুলো (যেমন 240p, 144p) বড়টা থেকে ছোট করে বানানোর অপশন
    qualities = list(native)
    if native:
        max_p = max(q["p"] for q in native)
        for rung in LADDER:
            if rung >= max_p or any(abs(q["p"] - rung) <= rung * 0.13 for q in native):
                continue
            src = min((q for q in native if q["p"] >= rung), key=lambda q: q["p"])
            est = (SCALE_KBPS[rung] + 128) * 1000 / 8 * duration
            qualities.append({"height": src["height"], "p": rung, "fps": src["fps"],
                              "size": fmt_size(est), "scale": rung})
        qualities.sort(key=lambda q: q["p"], reverse=True)

    token = remember(info, url)
    thumb = f"/api/thumb/{token}" if info.get("thumbnail") else None

    return jsonify(
        token=token,
        title=info.get("title") or "ভিডিও",
        thumbnail=thumb,
        duration=duration,
        uploader=info.get("uploader") or info.get("channel"),
        platform=detect_platform(url, info.get("extractor_key", "")),
        url=info.get("webpage_url") or url,
        qualities=qualities,
        audio_size=fmt_size(MP3_KBPS * 1000 / 8 * duration) if duration else None,
    )


@app.get("/api/thumb/<token>")
def api_thumb(token):
    e = infos.get(token)
    url = e and e["info"].get("thumbnail")
    if not url:
        abort(404)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            ctype = r.headers.get("Content-Type", "image/jpeg")
            data = r.read(8_000_000)
    except Exception:
        abort(404)
    if not ctype.startswith("image/"):
        abort(404)
    return data, 200, {"Content-Type": ctype, "Cache-Control": "max-age=3600"}


def build_ffmpeg(entry, parts, mode, scale):
    """ভিডিও/অডিও অংশগুলো নিয়ে ffmpeg কমান্ড বানায়। অংশগুলো আসে /api/_part থেকে।"""
    json_path = info_json(entry)
    # -xerror: কোনো অংশ মাঝপথে কেটে গেলে ffmpeg থেমে যাবে, অর্ধেক ফাইল বানাবে না
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-xerror"]
    secrets, vi, ai = [], None, None
    for i, f in enumerate(parts):
        s = uuid.uuid4().hex
        part_src[s] = {"json": json_path, "format_id": f["format_id"]}
        secrets.append(s)
        cmd += ["-i", f"http://127.0.0.1:{PORT}/api/_part/{s}"]
        if vi is None and f.get("vcodec") != "none":
            vi = i
        if f.get("acodec") != "none":
            ai = i

    if mode == "audio":
        cmd += ["-map", f"{ai if ai is not None else 0}:a:0", "-vn",
                "-c:a", "libmp3lame", "-b:a", f"{MP3_KBPS}k", "-f", "mp3", "pipe:1"]
        return cmd, secrets

    cmd += ["-map", f"{vi if vi is not None else 0}:v:0"]
    if ai is not None:
        cmd += ["-map", f"{ai}:a:0?"]
    if scale:
        k = SCALE_KBPS[scale]
        cmd += ["-vf", f"scale='if(gt(iw,ih),-2,{scale})':'if(gt(iw,ih),{scale},-2)'",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-b:v", f"{k}k", "-maxrate", f"{int(k * 1.5)}k", "-bufsize", f"{k * 2}k"]
    else:
        cmd += ["-c:v", "copy"]
    acodec = (parts[ai].get("acodec") or "") if ai is not None else ""
    if acodec.startswith(("mp4a", "aac")) and not scale:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k" if scale else "192k"]
    # fragmented MP4: শুরু থেকেই ব্রাউজারে পাঠানো যায়, পুরোটা তৈরি হওয়ার অপেক্ষা লাগে না
    cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]
    return cmd, secrets


def start_job(job_id, a):
    """ডাউনলোডটা সার্ভারে আলাদা থ্রেডে চালু করে। ব্রাউজার/অ্যাপ বন্ধ হলেও এটা থামে না।"""
    mode = "audio" if a.get("mode") == "audio" else "video"
    height, scale = a.get("h") or None, a.get("s") or None
    if height and not height.isdigit():
        raise ValueError("ভুল রেজুলেশন")
    if scale and not (scale.isdigit() and int(scale) in SCALE_KBPS):
        raise ValueError("ভুল রেজুলেশন")
    scale = int(scale) if scale else None

    job = {"status": "starting", "created": time.time(), "bytes": 0, "percent": None, "mode": mode}
    jobs[job_id] = job
    try:
        entry = get_entry(a.get("t", ""), a.get("u", ""))
        info = entry["info"]
        duration = info.get("duration") or 0
        with yt_dlp.YoutubeDL(base_opts()) as ydl:
            chosen = pick(ydl, info, "bestaudio/best" if mode == "audio" else video_format_spec(int(height) if height else None))
        if not chosen:
            raise RuntimeError("Requested format is not available")
        parts = chosen.get("requested_formats") or [chosen]
        cmd, secrets = build_ffmpeg(entry, parts, mode, scale)
    except Exception as e:
        log_exc()
        job.update(status="error", error=friendly_error(e), finished=time.time())
        raise

    if mode == "audio":
        est = MP3_KBPS * 1000 / 8 * duration
        filename = safe_filename(info.get("title") or "audio", "mp3")
    else:
        if scale:
            est = (SCALE_KBPS[scale] + 128) * 1000 / 8 * duration
            p = scale
        else:
            est = sum(size_of(f, duration) for f in parts)
            p = min(chosen.get("height") or 0, chosen.get("width") or 10**6)
        filename = safe_filename(f"{info.get('title') or 'video'} ({p}p)", "mp4")

    # ফাইলটা আগে সার্ভারের temp ফোল্ডারে তৈরি হয়, ব্রাউজার সেখান থেকে সাথে সাথে পড়ে নেয়
    folder = JOBS_DIR / job_id
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("out.mp3" if mode == "audio" else "out.mp4")
    path.touch()
    job.update(est=est, total=fmt_size(est), filename=filename, path=str(path))
    threading.Thread(target=run_job, args=(job_id, cmd, secrets), daemon=True).start()
    return job


def run_job(job_id, cmd, secrets):
    job = jobs[job_id]
    path = Path(job["path"])
    est = job.get("est") or 0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
    procs[job_id] = proc
    err = []
    threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True).start()
    win = deque([(time.time(), 0)])
    job["status"] = "downloading"
    try:
        with open(path, "wb") as f:
            while True:
                chunk = proc.stdout.read1(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                f.flush()
                job["bytes"] += len(chunk)
                now = time.time()
                win.append((now, job["bytes"]))
                while len(win) > 2 and now - win[0][0] > 3:
                    win.popleft()
                dt = now - win[0][0]
                speed = (job["bytes"] - win[0][1]) / dt if dt > 0.3 else None
                job.update(
                    downloaded=fmt_size(job["bytes"]),
                    speed=(fmt_size(speed) + "/s") if speed else None,
                    percent=round(min(job["bytes"] / est * 100, 99), 1) if est else None,
                    eta=int(max(est - job["bytes"], 0) / speed) if speed and est else None,
                )
        rc = proc.wait()
        if job.get("cancel"):
            job.update(status="cancelled", finished=time.time())
        elif rc != 0:
            msg = (err[0] if err else b"").decode(errors="replace").strip()
            raise RuntimeError(msg or f"ffmpeg exit {rc}")
        else:
            job.update(status="done", percent=100, filesize=fmt_size(job["bytes"]), finished=time.time())
    except Exception as e:
        if job["status"] != "cancelled":
            log_exc()
            job.update(status="error", error=friendly_error(e), finished=time.time())
    finally:
        if proc.poll() is None:
            proc.kill()
        procs.pop(job_id, None)
        for s in secrets:
            part_src.pop(s, None)
        if job["status"] != "done":
            shutil.rmtree(path.parent, ignore_errors=True)


def tail(job, sock):
    """সার্ভারে যতটুকু তৈরি হয়েছে ততটুকু ব্রাউজারে পাঠায়, তারপর বাকিটার জন্য অপেক্ষা করে"""
    # খালি চাংক পাঠালে ব্রাউজার সাথে সাথে "ডাউনলোড শুরু" দেখায়
    yield b""
    try:
        with open(job["path"], "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if chunk:
                    yield chunk
                    continue
                status = job["status"]
                if status == "done":
                    while True:
                        chunk = f.read(CHUNK)
                        if not chunk:
                            return
                        yield chunk
                if status in ("error", "cancelled"):
                    # অসম্পূর্ণ ফাইলকে ব্রাউজার যেন "সম্পূর্ণ" না ধরে
                    drop_connection(sock)
                    return
                time.sleep(0.15)
    except OSError:
        drop_connection(sock)
    # ব্রাউজারের সংযোগ কেটে গেলে (অ্যাপ বন্ধ, নেট গেল) শুধু পাঠানো থামে; সার্ভারের ডাউনলোড চলতেই থাকে


@app.get("/api/stream")
def api_stream():
    a = request.args
    mode = "audio" if a.get("mode") == "audio" else "video"
    job_id = a.get("job", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", job_id):
        job_id = uuid.uuid4().hex
    sock = request.environ.get("werkzeug.socket")

    job = jobs.get(job_id)
    # একই ডাউনলোড আবার চাওয়া হলে (ব্রাউজারের Retry): শেষ হয়ে গেলে পুরো ফাইল সাথে সাথে দিই
    if job and job["status"] == "done" and Path(job["path"]).exists():
        return send_job_file(job)
    if not job or job["status"] in ("error", "cancelled"):
        cleanup()
        try:
            job = start_job(job_id, a)
        except Exception as e:
            return Response(friendly_error(e), status=400, mimetype="text/plain; charset=utf-8")

    headers = {
        "Content-Disposition": content_disposition(job["filename"]),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    return Response(tail(job, sock), mimetype="audio/mpeg" if mode == "audio" else "video/mp4", headers=headers)


def send_job_file(job):
    # পুরো সাইজ জানা থাকায় ব্রাউজার মাঝপথে থেমে গেলেও আবার যেখান থেকে থেমেছিল সেখান থেকে চালাতে পারে
    return send_file(job["path"], as_attachment=True, download_name=job["filename"], conditional=True,
                     mimetype="audio/mpeg" if job["mode"] == "audio" else "video/mp4", max_age=0)


@app.get("/api/file/<job_id>")
def api_file(job_id):
    """শেষ হওয়া ফাইল আবার সেভ করা (আবার নামাতে হয় না)"""
    job = jobs.get(job_id)
    if not job or job["status"] != "done" or not Path(job["path"]).exists():
        return Response("ফাইলটা আর সার্ভারে নেই। আবার ডাউনলোড করুন।", status=404, mimetype="text/plain; charset=utf-8")
    return send_job_file(job)


@app.get("/api/_part/<secret>")
def api_part(secret):
    """ffmpeg-এর জন্য একটা অংশ (ভিডিও বা অডিও) yt-dlp দিয়ে টুকরো টুকরো করে দ্রুত নামিয়ে দেয়"""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(403)
    src = part_src.get(secret)
    if not src:
        abort(404)
    cmd = [sys.executable, "-m", "yt_dlp", "--load-info-json", src["json"], "-f", src["format_id"],
           "-o", "-", "--quiet", "--no-warnings", "--no-progress",
           "--concurrent-fragments", "4", "--retries", "10", "--fragment-retries", "10"]
    # HLS-এর টুকরোগুলো yt-dlp চলতি ফোল্ডারে অস্থায়ীভাবে লেখে, তাই প্রতিটার জন্য আলাদা temp ফোল্ডার
    work = CACHE_DIR / f"part-{secret}"
    work.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=work, creationflags=NO_WINDOW)
    sock = request.environ.get("werkzeug.socket")

    def gen():
        try:
            while True:
                b = proc.stdout.read1(CHUNK)
                if not b:
                    break
                yield b
            if proc.wait() != 0:
                # মাঝপথে নামানো আটকে গেলে ffmpeg-কে জানাই, যাতে অর্ধেক ফাইল "সম্পূর্ণ" না ধরে
                drop_connection(sock)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            shutil.rmtree(work, ignore_errors=True)

    return Response(gen(), mimetype="application/octet-stream")


@app.get("/api/progress/<job_id>")
def api_progress(job_id):
    job = jobs.get(job_id)
    if not job:
        abort(404)
    # est (আনুমানিক মোট বাইট) পেজে গ্রাফ আর % টানা চালাতে লাগে
    return jsonify({k: v for k, v in job.items() if k not in ("cancel", "path")})


@app.post("/api/cancel/<job_id>")
def api_cancel(job_id):
    job = jobs.get(job_id)
    if not job:
        abort(404)
    job["cancel"] = True
    job["status"] = "cancelled"
    p = procs.get(job_id)
    if p and p.poll() is None:
        p.kill()
    return jsonify(ok=True)


# ---------------------------------------------------------------- main

if __name__ == "__main__":
    # DEV মোডে debugger চালু থাকে, যেটা দিয়ে কোড চালানো যায়, তাই শুধু এই কম্পিউটারেই খোলা থাকবে
    lan = "--lan" in sys.argv and not DEV
    host = "0.0.0.0" if lan else "127.0.0.1"

    # DEV মোডে Werkzeug রিলোডার স্ক্রিপ্টটা দ্বিতীয়বার চালায়, তখন ব্যানার/ব্রাউজার আবার খুলব না
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        app.run(host=host, port=PORT, threaded=True, debug=DEV)
        sys.exit()

    if DEV:
        print("\n  [DEV মোড] কোড বদলালে সার্ভার নিজে রিস্টার্ট হবে, ব্রাউজার নিজে রিফ্রেশ হবে।")
        print("  (রিস্টার্ট হলে চলমান ডাউনলোড বাতিল হয়ে যাবে)")
        if "--lan" in sys.argv:
            print("  নিরাপত্তার জন্য DEV মোডে --lan (ফোন থেকে খোলা) বন্ধ থাকে।")
    print("\n  ভিডিও ডাউনলোডার চালু হয়েছে!")
    print(f"  ব্রাউজারে খুলুন:  http://127.0.0.1:{PORT}")
    if lan:
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            print(f"  ফোন থেকে (একই WiFi):  http://{s.getsockname()[0]}:{PORT}")
            s.close()
        except OSError:
            pass
    print("  ভিডিও আপনার ব্রাউজারের Downloads ফোল্ডারে সেভ হবে।")
    print("  ফোন/ব্রাউজার বন্ধ করলেও ডাউনলোড চলবে, কিন্তু এই উইন্ডোটা খোলা রাখতে হবে।")
    print("  বন্ধ করতে এই উইন্ডো বন্ধ করুন বা Ctrl+C চাপুন।\n")
    if "--open" in sys.argv:
        import webbrowser
        threading.Timer(1.5, webbrowser.open, args=(f"http://127.0.0.1:{PORT}",)).start()
    app.run(host=host, port=PORT, threaded=True, debug=DEV)
