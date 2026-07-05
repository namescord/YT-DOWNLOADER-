"""
YouTube Downloader — Flask + yt-dlp backend (personal use).

Architecture (deliberate, so it survives a single small server):
  POST /api/info      -> validate URL, fetch metadata + build a clean format list
  POST /api/download  -> start a background job, return job_id
  GET  /api/progress  -> poll job status/percentage
  GET  /api/file      -> stream the finished file, then delete it on connection close

Only 1 download runs at a time, files live in a temp dir and are purged after
they're served (or after a TTL). See README.md for Render deployment + free-tier caveats.
"""

import os
import re
import time
import shutil
import tempfile
import threading
from pathlib import Path

from flask import (
    Flask, request, jsonify, send_file, render_template,
)
import yt_dlp

# --- ffmpeg location -----------------------------------------------------------
# imageio-ffmpeg ships a static ffmpeg binary via pip, so we don't need a system
# install on Render's native runtime. If it's missing we fall back to PATH.
try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_PATH = None

app = Flask(__name__)

# --- config --------------------------------------------------------------------
DOWNLOAD_ROOT = Path(tempfile.gettempdir()) / "ytdl_jobs"
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

JOB_TTL = 600          # seconds a finished/failed job survives before purge
IP_COOLDOWN = 15       # seconds a single IP must wait between downloads
MAX_CONCURRENT = 1     # keep at 1 on a small instance — see README
MAX_DURATION = 60 * 90  # refuse videos longer than 90 min (memory/disk guard)

# Standard resolution ladder we offer if the video has them.
RESOLUTION_LADDER = [2160, 1440, 1080, 720, 480, 360]

# --- shared state (single process, gunicorn --workers 1) -----------------------
jobs = {}                       # job_id -> dict
jobs_lock = threading.Lock()
active_count = {"n": 0}
last_request_by_ip = {}         # ip -> timestamp
state_lock = threading.Lock()

# --- URL validation ------------------------------------------------------------
VIDEO_ID_RE = re.compile(r"[\w-]{11}")
WATCH_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/watch\?", re.IGNORECASE
)
SHORT_RE = re.compile(
    r"^(?:https?://)?youtu\.be/([\w-]{11})", re.IGNORECASE
)
SHORTS_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/shorts/([\w-]{11})", re.IGNORECASE
)
PLAYLIST_CHANNEL_RE = re.compile(
    r"youtube\.com/(?:playlist|channel/|c/|user/|@)", re.IGNORECASE
)


def normalize_url(raw: str):
    """
    Return a clean single-video watch URL, or raise ValueError with a clear message.
    Rejects playlists / channels. Accepts watch, youtu.be, and /shorts/ links.
    """
    if not raw or not raw.strip():
        raise ValueError("Please paste a YouTube link.")
    url = raw.strip()

    # Reject obvious playlist / channel pages up front.
    if PLAYLIST_CHANNEL_RE.search(url):
        raise ValueError("Playlists and channels aren't supported — paste a single video link.")

    m = SHORT_RE.match(url)
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"

    m = SHORTS_RE.match(url)
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"

    if WATCH_RE.match(url):
        # pull the v= param, drop everything else (including &list=)
        m = re.search(r"[?&]v=([\w-]{11})", url)
        if not m:
            raise ValueError("Couldn't find a video ID in that link.")
        return f"https://www.youtube.com/watch?v={m.group(1)}"

    raise ValueError("That doesn't look like a valid YouTube video link.")


def classify_error(msg: str) -> str:
    """Map noisy yt-dlp errors to something a human can act on."""
    m = msg.lower()
    if "private" in m:
        return "This video is private and can't be downloaded."
    if "age" in m and ("restrict" in m or "confirm" in m or "sign in" in m):
        return "This video is age-restricted and requires sign-in."
    if "sign in to confirm" in m or "not a bot" in m:
        return "YouTube is blocking this server (bot check). See README about datacenter IPs."
    if "geo" in m or "not available in your country" in m or "region" in m:
        return "This video is region-locked and unavailable from this server's location."
    if "removed" in m or "no longer available" in m or "unavailable" in m:
        return "This video is unavailable or has been removed."
    if "copyright" in m:
        return "This video is blocked (copyright)."
    if "members-only" in m or "join this channel" in m:
        return "This is members-only content and can't be downloaded."
    return "Couldn't process this video. It may be unavailable, restricted, or unsupported."


# --- yt-dlp helpers ------------------------------------------------------------
def _base_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": False,
    }
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH
    return opts


def fetch_info(url: str) -> dict:
    """Metadata only (no download). Returns a trimmed dict for the frontend."""
    opts = _base_opts()
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    duration = info.get("duration") or 0
    if duration and duration > MAX_DURATION:
        raise ValueError(
            f"Video is {duration // 60} min long. Limit is {MAX_DURATION // 60} min "
            f"to stay within this server's memory/disk."
        )

    # Which heights are actually available?
    heights = set()
    for f in info.get("formats", []):
        h = f.get("height")
        if h and f.get("vcodec") not in (None, "none"):
            heights.add(h)

    options = []
    for target in RESOLUTION_LADDER:
        # offer a rung if the video has something at or below it that we can reach
        if any(h >= target for h in heights) or any(h == target for h in heights):
            if any(abs(h - target) <= 0 for h in heights) or any(h >= target for h in heights):
                options.append({"value": str(target), "label": f"{target}p (MP4)"})
    # de-dupe while preserving ladder order
    seen, video_opts = set(), []
    for o in options:
        if o["value"] not in seen:
            seen.add(o["value"])
            video_opts.append(o)

    # Always offer audio-only mp3
    video_opts.append({"value": "mp3", "label": "Audio only (MP3, 192kbps)"})

    return {
        "title": info.get("title", "Untitled"),
        "uploader": info.get("uploader", ""),
        "duration": duration,
        "thumbnail": info.get("thumbnail", ""),
        "formats": video_opts,
    }


def make_progress_hook(job_id):
    def hook(d):
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes", 0)
                if total:
                    job["progress"] = round(min(done / total * 100, 99.0), 1)
                job["status"] = "downloading"
                job["speed"] = d.get("speed")
                job["eta"] = d.get("eta")
            elif status == "finished":
                # a stream finished; merge / mp3 conversion may still run
                job["status"] = "processing"
                job["progress"] = 99.0
    return hook


def run_download(job_id, url, choice):
    """Runs in a background thread. choice is 'mp3' or a height like '1080'."""
    try:
        job_dir = DOWNLOAD_ROOT / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        outtmpl = str(job_dir / "%(title).80s.%(ext)s")

        opts = _base_opts()
        opts.update({
            "outtmpl": outtmpl,
            "progress_hooks": [make_progress_hook(job_id)],
            "retries": 3,
            "fragment_retries": 3,
        })

        if choice == "mp3":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        else:
            h = int(choice)
            # Prefer muxable mp4/m4a up to target height; fall back progressively.
            opts["format"] = (
                f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={h}]+bestaudio/"
                f"best[height<={h}]/best"
            )
            opts["merge_output_format"] = "mp4"

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if choice == "mp3":
                filename = str(Path(filename).with_suffix(".mp3"))
            else:
                p = Path(filename)
                if p.suffix.lower() != ".mp4" and p.with_suffix(".mp4").exists():
                    filename = str(p.with_suffix(".mp4"))

        final = Path(filename)
        if not final.exists():
            # name resolution can drift after postprocessing — grab the biggest file
            candidates = [f for f in job_dir.iterdir() if f.is_file()]
            if not candidates:
                raise RuntimeError("Download produced no file.")
            final = max(candidates, key=lambda f: f.stat().st_size)

        with jobs_lock:
            jobs[job_id].update(
                status="finished", progress=100.0,
                filepath=str(final), filename=final.name,
                finished_at=time.time(),
            )
    except yt_dlp.utils.DownloadError as e:
        with jobs_lock:
            jobs[job_id].update(
                status="error", error=classify_error(str(e)), finished_at=time.time()
            )
    except ValueError as e:
        with jobs_lock:
            jobs[job_id].update(status="error", error=str(e), finished_at=time.time())
    except Exception as e:
        with jobs_lock:
            jobs[job_id].update(
                status="error", error=f"Unexpected error: {e}", finished_at=time.time()
            )
    finally:
        with state_lock:
            active_count["n"] = max(0, active_count["n"] - 1)


# --- routes --------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def api_info():
    data = request.get_json(silent=True) or {}
    try:
        url = normalize_url(data.get("url", ""))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    try:
        return jsonify(fetch_info(url))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except yt_dlp.utils.DownloadError as e:
        return jsonify(error=classify_error(str(e))), 400
    except Exception as e:
        return jsonify(error=f"Couldn't fetch info: {e}"), 500


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True) or {}
    try:
        url = normalize_url(data.get("url", ""))
    except ValueError as e:
        return jsonify(error=str(e)), 400

    choice = str(data.get("format", "")).strip()
    if choice != "mp3" and not choice.isdigit():
        return jsonify(error="Pick a valid format."), 400

    # per-IP cooldown
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
    ip = ip.split(",")[0].strip()
    now = time.time()
    with state_lock:
        last = last_request_by_ip.get(ip, 0)
        if now - last < IP_COOLDOWN:
            wait = int(IP_COOLDOWN - (now - last)) + 1
            return jsonify(error=f"Please wait {wait}s before starting another download."), 429
        if active_count["n"] >= MAX_CONCURRENT:
            return jsonify(error="Server busy — another download is in progress. Try again shortly."), 429
        active_count["n"] += 1
        last_request_by_ip[ip] = now

    job_id = os.urandom(8).hex()
    with jobs_lock:
        jobs[job_id] = {"status": "starting", "progress": 0.0, "created_at": now}

    threading.Thread(target=run_download, args=(job_id, url, choice), daemon=True).start()
    return jsonify(job_id=job_id)


@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify(error="Unknown or expired job."), 404
        return jsonify({
            "status": job.get("status"),
            "progress": job.get("progress", 0.0),
            "error": job.get("error"),
            "filename": job.get("filename"),
            "eta": job.get("eta"),
        })


@app.route("/api/file/<job_id>")
def api_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job.get("status") != "finished":
        return jsonify(error="File isn't ready."), 404
    path = job.get("filepath")
    if not path or not os.path.exists(path):
        return jsonify(error="File expired or was already downloaded."), 410

    resp = send_file(path, as_attachment=True, download_name=job["filename"])

    @resp.call_on_close
    def _cleanup():
        shutil.rmtree(DOWNLOAD_ROOT / job_id, ignore_errors=True)
        with jobs_lock:
            jobs.pop(job_id, None)

    return resp


@app.route("/healthz")
def healthz():
    return "ok", 200


# --- janitor: purge stale jobs/files so disk never fills ----------------------
def janitor():
    while True:
        time.sleep(120)
        now = time.time()
        stale = []
        with jobs_lock:
            for jid, job in list(jobs.items()):
                ref = job.get("finished_at") or job.get("created_at", now)
                if now - ref > JOB_TTL:
                    stale.append(jid)
                    jobs.pop(jid, None)
        for jid in stale:
            shutil.rmtree(DOWNLOAD_ROOT / jid, ignore_errors=True)
        # also sweep orphaned dirs
        for d in DOWNLOAD_ROOT.iterdir() if DOWNLOAD_ROOT.exists() else []:
            try:
                if d.is_dir() and now - d.stat().st_mtime > JOB_TTL:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


threading.Thread(target=janitor, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
