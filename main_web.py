from flask import Flask, render_template, request, send_file, jsonify, Response
import yt_dlp
import os
import shutil
import logging
from datetime import datetime
import threading
import time
import uuid
import json
from urllib.parse import urlparse
import tempfile
import subprocess
import sys

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# -------------------- CONFIGURATION --------------------

# ffmpeg path
FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "ffmpeg")

# Optional API key protection
API_KEY = os.environ.get("INSTAWEB_API_KEY")

# Load Instagram cookies (from Render Environment Variables)
INSTAGRAM_COOKIES = os.environ.get("INSTAGRAM_COOKIES")

# Rate limiting
RATE_LIMIT_COUNT = int(os.environ.get("RATE_LIMIT_COUNT", "5"))
RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", str(60 * 60)))  # 1 hour
RATE_LIMIT_CONCURRENT = int(os.environ.get("RATE_LIMIT_CONCURRENT", "3"))

# Auto-update yt-dlp (optional)
YTDLP_AUTO_UPDATE = os.environ.get("YTDLP_AUTO_UPDATE", "false").lower() in ("1", "true", "yes")
YTDLP_UPDATE_INTERVAL_MIN = int(os.environ.get("YTDLP_UPDATE_INTERVAL_MIN", "60"))

# Temp download directory
BASE_DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(BASE_DOWNLOAD_DIR, exist_ok=True)

# In-memory job and rate store
jobs = {}
jobs_lock = threading.Lock()
rate_limit = {}

# -------------------- UTILS --------------------

def get_client_ip():
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def check_rate_limit(ip):
    now = time.time()
    times = rate_limit.get(ip, [])
    times = [t for t in times if now - t < RATE_LIMIT_WINDOW]
    if len(times) >= RATE_LIMIT_COUNT:
        return False
    times.append(now)
    rate_limit[ip] = times
    return True


def is_valid_instagram_url(url):
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.netloc.lower()
        if not (host == "instagram.com" or host.endswith(".instagram.com")):
            return False
        path = parsed.path.lower()
        if any(x in path for x in ("/reel/", "/reels/", "/p/", "/tv/")):
            return True
        return False
    except Exception:
        return False


def yt_progress_hook(job_id):
    def hook(d):
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes", 0)
                try:
                    percent = (downloaded / total) * 100 if total else None
                except Exception:
                    percent = None
                job["progress"] = {
                    "status": "downloading",
                    "downloaded": downloaded,
                    "total": total,
                    "percent": percent,
                    "eta": d.get("eta"),
                }
            elif status == "finished":
                job["progress"] = {"status": "finished"}
    return hook


def background_cleaner():
    """Delete old temp files every 30 minutes."""
    while True:
        now = time.time()
        cutoff = now - (30 * 60)
        try:
            for name in os.listdir(BASE_DOWNLOAD_DIR):
                path = os.path.join(BASE_DOWNLOAD_DIR, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        if os.path.isdir(path):
                            shutil.rmtree(path, ignore_errors=True)
                        else:
                            os.remove(path)
                        with jobs_lock:
                            for jid, j in list(jobs.items()):
                                if j.get("temp_dir") == path:
                                    jobs.pop(jid, None)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(60)


threading.Thread(target=background_cleaner, daemon=True).start()


def ytdlp_auto_updater():
    while True:
        try:
            logging.info("Checking yt-dlp updates...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
                check=True,
            )
            logging.info("yt-dlp updated successfully")
        except Exception:
            logging.exception("yt-dlp update failed")
        time.sleep(max(1, YTDLP_UPDATE_INTERVAL_MIN) * 60)


if YTDLP_AUTO_UPDATE:
    threading.Thread(target=ytdlp_auto_updater, daemon=True).start()


# -------------------- DOWNLOAD LOGIC --------------------

def run_download_job(job_id, url, fmt, cookies=None, proxy=None):
    temp_dir = os.path.join(BASE_DOWNLOAD_DIR, job_id)
    os.makedirs(temp_dir, exist_ok=True)
    with jobs_lock:
        jobs[job_id]["temp_dir"] = temp_dir
        jobs[job_id]["status"] = "running"
        jobs[job_id]["progress"] = {"status": "started"}

    # If no cookies passed in request, use environment cookies
    if not cookies and INSTAGRAM_COOKIES:
        try:
            cookiefile_path = os.path.join(temp_dir, "env_cookies.txt")
            with open(cookiefile_path, "w", encoding="utf-8") as cf:
                cf.write(INSTAGRAM_COOKIES)
            cookies = cookiefile_path
            logging.info(f"Using environment cookies for job {job_id}")
        except Exception as e:
            logging.error(f"Failed to write environment cookies: {e}")

    try:
        outtmpl = os.path.join(temp_dir, "%(id)s.%(ext)s")
        ydl_opts = {
            "outtmpl": outtmpl,
            "noplaylist": True,
            "progress_hooks": [yt_progress_hook(job_id)],
            "ffmpeg_location": FFMPEG_PATH,
            "quiet": True,
        }

        if cookies:
            ydl_opts["cookiefile"] = cookies
        if proxy:
            ydl_opts["proxy"] = proxy

        if fmt == "audio":
            ydl_opts["format"] = "bestaudio/best"
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
            final_ext = "mp3"
        else:
            ydl_opts["format"] = "bestvideo+bestaudio/best"
            final_ext = "mp4"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        produced = None
        for root, _, files in os.walk(temp_dir):
            for f in files:
                if f.lower().endswith(final_ext):
                    produced = os.path.join(root, f)
                    break
            if produced:
                break

        if not produced:
            raise FileNotFoundError("Output file not found after download.")

        with jobs_lock:
            jobs[job_id]["status"] = "ready"
            jobs[job_id]["filepath"] = produced
            jobs[job_id]["filename"] = os.path.basename(produced)
            jobs[job_id]["size"] = os.path.getsize(produced) if os.path.exists(produced) else None

    except yt_dlp.utils.DownloadError:
        logging.exception("yt-dlp download error")
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = "Failed to download media. The URL may be private or invalid."
    except FileNotFoundError:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = "ffmpeg not found or output missing."
    except Exception:
        logging.exception("Unexpected error in run_download_job")
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = "Unexpected server error during download."


# -------------------- ROUTES --------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json() or {}
    url = data.get("url")
    fmt = data.get("format", "mp4")

    if API_KEY:
        key = request.headers.get("X-API-Key") or data.get("api_key")
        if not key or key != API_KEY:
            return jsonify({"error": "Invalid or missing API key"}), 401

    if not url or not is_valid_instagram_url(url):
        return jsonify({"error": "Invalid Instagram URL"}), 400

    ip = get_client_ip()
    if not check_rate_limit(ip):
        return jsonify({"error": "Rate limit exceeded"}), 429

    job_id = uuid.uuid4().hex
    cookies = data.get("cookies")
    proxy = data.get("proxy")

    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "progress": {"status": "queued"},
            "created_at": time.time(),
            "format": fmt,
            "url": url,
            "ip": ip,
            "cookies": bool(cookies),
            "proxy": bool(proxy),
        }

    threading.Thread(target=run_download_job, args=(job_id, url, fmt, cookies, proxy), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/events/<job_id>")
def events(job_id):
    def gen():
        last = None
        while True:
            with jobs_lock:
                job = jobs.get(job_id)
                if not job:
                    payload = {"status": "unknown"}
                else:
                    payload = {
                        "status": job.get("status"),
                        "progress": job.get("progress"),
                        "error": job.get("error") if job.get("status") == "error" else None,
                        "filename": job.get("filename"),
                        "size": job.get("size"),
                    }
            s = json.dumps(payload)
            if s != last:
                yield f"data: {s}\n\n"
                last = s
            if payload.get("status") in ("ready", "error", "unknown"):
                break
            time.sleep(0.5)
    return Response(gen(), mimetype="text/event-stream")


@app.route("/download/<job_id>")
def download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return "Job not found", 404
        if job.get("status") != "ready" or not job.get("filepath"):
            return "File not ready", 400
        filepath = job["filepath"]
        filename = job.get("filename") or os.path.basename(filepath)

    try:
        return send_file(filepath, as_attachment=True, download_name=filename)
    except TypeError:
        return send_file(filepath, as_attachment=True)


# -------------------- MAIN --------------------

if __name__ == "__main__":
    app.run(debug=True, threaded=True)
