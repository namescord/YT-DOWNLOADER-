# Video Downloader (Flask + yt-dlp)

Personal-use YouTube downloader. Paste a single video URL, pick a resolution or
"Audio only (MP3)", and the file streams to your browser. Progress is polled.

## Files
- `app.py` — Flask backend (info / download / progress / file endpoints)
- `templates/index.html` — single-page frontend (HTML + CSS + JS inline)
- `requirements.txt`, `Procfile`, `render.yaml` — native Render deploy
- `Dockerfile` — optional Docker deploy (system ffmpeg)

## Run locally
```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
# open http://localhost:5000
```
`imageio-ffmpeg` bundles a static ffmpeg via pip, so MP3 extraction works with no
separate ffmpeg install. If you already have ffmpeg on your PATH it uses that.

## Deploy to Render (native Python runtime)
1. Push this folder to a GitHub repo.
2. Render → New → Web Service → connect the repo.
3. Runtime: **Python**. Build: `pip install -r requirements.txt`.
4. **Start command (exact):**
   ```
   gunicorn app:app --workers 1 --threads 4 --worker-class gthread --timeout 300 --bind 0.0.0.0:$PORT
   ```
5. Health check path: `/healthz`.

### ffmpeg on Render
You do **not** need a buildpack or apt config on the native runtime — ffmpeg comes
from the `imageio-ffmpeg` pip package. If you'd rather use a real system ffmpeg,
deploy with the included `Dockerfile` (choose "Docker" instead of "Python"); it runs
`apt-get install ffmpeg`.

## Why the start command is shaped this way
The app keeps job state (progress, file paths) **in process memory**. That forces:
- `--workers 1` — multiple workers = separate memory, so `/api/progress/<id>` would
  randomly 404. Do **not** raise the worker count.
- `--threads 4 --worker-class gthread` — so progress polling stays responsive while
  a download runs in a background thread.
- `--timeout 300` — the download itself runs in a background thread, so requests stay
  short; the raised timeout is just a safety margin.

---

## Free-tier reality — read this

**What breaks on Render Free (512 MB RAM / ephemeral disk / spin-down):**
1. **Memory.** Merging 1080p video + audio with ffmpeg on a 512 MB instance can spike
   and OOM-kill the worker on longer videos. `MAX_DURATION` is capped at 90 min in
   `app.py` as a guard — lower it if you see crashes. 4K (2160p) will likely OOM; it's
   offered only if the source has it.
2. **Disk.** Free disk is small and ephemeral. Files are deleted right after they're
   served (`call_on_close`) and a janitor purges anything older than 10 min. Keep
   `MAX_CONCURRENT = 1` — parallel downloads will fill the disk.
3. **Spin-down.** Free services sleep after ~15 min idle. First request after sleep
   takes 30–60 s to cold-start. Not a bug, just slow.
4. **Single worker.** One process = one download at a time by design.

**The bigger problem (this is the honest part):**
5. **YouTube blocks datacenter IPs.** Render runs on cloud IPs. YouTube increasingly
   answers those with *"Sign in to confirm you're not a bot."* A downloader hosted on
   Render will fail on many/most videos — sometimes immediately, sometimes after a few.
   The same code runs fine from your home/phone IP. There's no clean fix on free hosting
   (cookies/proxies are fragile and against YouTube ToS). The app surfaces this as a
   clear error, but you can't code your way around it on Render.
6. **Keep yt-dlp fresh.** YouTube changes constantly; an old `yt-dlp` breaks. Redeploy
   periodically to pull the latest.

**Bottom line:** this is built and safe to run **locally / on your own machine** (open
`localhost:5000` from your phone on the same Wi-Fi). Deploying it as a public Render
service is unreliable *and* risky — see the note the assistant gave you about Render's
account policy on public downloaders.
