# XViewer

Read-only viewer for public X posts with an X-style UI. Search users, hashtags, or paste a post link; download videos, photos, or post text.

## Deploy on Render (free)
1. Push this folder to a GitHub repo (`app.py`, `requirements.txt`, `render.yaml`, `templates/index.html` at the repo root).
2. Render dashboard -> New -> Blueprint -> pick the repo -> Apply. (Or New -> Web Service with build `pip install -r requirements.txt` and start `gunicorn app:app --workers 1 --threads 4 --timeout 60`, instance type Free.)
3. Optional: `PROXY_URL` (e.g. `http://user:pass@host:port`) sends X requests through a proxy if X blocks Render's IPs.
4. Optional, for hashtag search and as a backup for profiles: Environment -> add `NITTER_URLS` = comma-separated Nitter instance URLs that have RSS enabled.
5. To pick up yt-dlp fixes, use Manual Deploy -> Clear build cache & deploy.

Free services sleep after ~15 minutes idle; the next visit takes up to a minute to wake.
