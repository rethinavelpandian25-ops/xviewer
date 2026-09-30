"""
XViewer - a read-only viewer for public X (Twitter) data with an Instagram-style UI.

Data sources (no paid API, no login):
  * Profiles / timelines : X's public embed ("syndication") timeline endpoint
  * Single tweet media   : yt-dlp
  * Hashtags             : optional Nitter RSS instance(s) (set NITTER_URLS); X itself
                           requires a logged-in session for search.
  * Downloads            : streamed through /download (allow-listed to X's CDNs)

Run locally :  python app.py
Production  :  gunicorn app:app --workers 2 --threads 4 --timeout 60
"""
import base64
import html as htmllib
import json
import logging
import math
import random
import os
import re
import time
from urllib.parse import parse_qs, quote, unquote, urlparse
from xml.etree import ElementTree as ET

import requests
import yt_dlp
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("xviewer")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
TIMEOUT = 15
CACHE_TTL = int(os.environ.get("CACHE_TTL", "600"))
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
NITTER_URLS = [
    u.strip().rstrip("/")
    for u in (os.environ.get("NITTER_URLS") or os.environ.get("NITTER_URL", "")).split(",")
    if u.strip()
]
ALLOWED_DL_HOSTS = {"pbs.twimg.com", "video.twimg.com", "ton.twimg.com"}
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
TAG_RE = re.compile(r"^[\w]{1,100}$", re.UNICODE)
TWEET_URL_RE = re.compile(
    r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/[A-Za-z0-9_]+/status/\d+", re.I
)

_cache = {}


class ViewerError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- utils
def cached(key, producer):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        value = producer()
    except ViewerError:
        if hit:  # X is failing right now; an older copy beats an error page
            return hit[1]
        raise
    _cache[key] = (now, value)
    if len(_cache) > 500:  # keep memory bounded on free tiers
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
            _cache.pop(k, None)
    return value


def http_get(url, proxy=False, retries=2, **kw):
    """GET with a short retry on 429. proxy=True routes through PROXY_URL if configured."""
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT,
                             proxies=PROXIES if proxy else None, **kw)
        except requests.Timeout:
            raise ViewerError("X took too long to respond. Try again in a moment.", 504)
        except requests.RequestException:
            raise ViewerError("Couldn't reach X. Check the connection and try again.", 502)
        if r.status_code == 429 and attempt < retries:
            time.sleep(1.5 * (attempt + 1) + random.random())
            continue
        break
    if r.status_code == 429:
        raise ViewerError("X is rate-limiting this server. Wait a minute and retry.", 429)
    if r.status_code == 404:
        raise ViewerError("That account or page wasn't found.", 404)
    if r.status_code in (401, 403):
        raise ViewerError("X blocked this request. The account may be private or suspended.", 403)
    if r.status_code >= 400:
        raise ViewerError(f"X returned an error ({r.status_code}).", 502)
    return r


def clean_handle(raw):
    raw = (raw or "").strip()
    m = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]+)", raw, re.I)
    if m:
        raw = m.group(1)
    raw = raw.lstrip("@")
    if not HANDLE_RE.match(raw):
        raise ViewerError("Enter a valid username (letters, numbers, underscore; max 15).", 400)
    return raw


def big_avatar(url):
    return (url or "").replace("_normal.", "_400x400.")


# ---------------------------------------------------------------- tweet normalising
def normalize_tweet(t):
    retweeted_by = None
    if t.get("retweeted_status"):
        retweeted_by = (t.get("user") or {}).get("screen_name")
        t = t["retweeted_status"]

    user = t.get("user") or {}
    ents = t.get("entities") or {}
    text = t.get("full_text") or t.get("text") or ""
    for m in ents.get("media") or []:
        text = text.replace(m.get("url", ""), "")
    for u in ents.get("urls") or []:
        text = text.replace(u.get("url", ""), u.get("expanded_url") or u.get("url", ""))

    media = []
    raw_media = (t.get("extended_entities") or {}).get("media") or ents.get("media") or []
    for m in raw_media:
        kind = m.get("type")
        base = m.get("media_url_https") or ""
        if kind == "photo" and base:
            media.append({
                "type": "photo",
                "thumb": f"{base}?name=large",
                "src": f"{base}?name=large",
                "download": f"{base}?name=orig",
            })
        elif kind in ("video", "animated_gif"):
            variants = [
                v for v in (m.get("video_info") or {}).get("variants", [])
                if v.get("content_type") == "video/mp4" and v.get("url")
            ]
            if variants:
                best = max(variants, key=lambda v: v.get("bitrate") or 0)
                media.append({
                    "type": "video",
                    "thumb": base,
                    "src": best["url"],
                    "download": best["url"],
                })

    screen = user.get("screen_name", "")
    tid = t.get("id_str") or str(t.get("id", ""))
    return {
        "id": tid,
        "url": f"https://x.com/{screen}/status/{tid}" if screen and tid else "",
        "text": text.strip(),
        "created_at": t.get("created_at", ""),
        "likes": t.get("favorite_count", 0),
        "replies": t.get("reply_count", 0),
        "reposts": t.get("retweet_count", 0),
        "retweeted_by": retweeted_by,
        "media": media,
        "user": {
            "name": user.get("name", screen),
            "screen_name": screen,
            "avatar": big_avatar(user.get("profile_image_url_https", "")),
            "verified": bool(user.get("verified") or user.get("is_blue_verified")),
        },
    }


# ------------------------------------------------------------------------- profile
def fetch_profile_syndication(handle):
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{quote(handle)}"
    html = http_get(url, proxy=True).text
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise ViewerError(
            "X didn't return a public timeline for this account. It may be private, "
            "suspended, or X is limiting anonymous access right now.", 404)
    try:
        page = json.loads(m.group(1))["props"]["pageProps"]
    except (ValueError, KeyError):
        raise ViewerError("Couldn't read X's response. The format may have changed.", 502)

    entries = (page.get("timeline") or {}).get("entries") or []
    raw_tweets = [
        e["content"]["tweet"] for e in entries
        if e.get("type") == "tweet" and (e.get("content") or {}).get("tweet")
    ]
    posts = [normalize_tweet(t) for t in raw_tweets]

    header = page.get("headerProps") or {}
    owner = next((t.get("user") for t in raw_tweets
                  if (t.get("user") or {}).get("screen_name", "").lower() == handle.lower()), {}) or {}
    if not header and not owner and not posts:
        raise ViewerError("No public posts found for this account.", 404)

    def pick(*keys, default=None):
        for src in (header, owner):
            for k in keys:
                if src.get(k) not in (None, ""):
                    return src[k]
        return default

    profile = {
        "screen_name": pick("screenName", "screen_name", default=handle),
        "name": pick("name", default=handle),
        "avatar": big_avatar(pick("profileImageUrl", "profile_image_url_https", default="")),
        "bio": pick("description", default=""),
        "posts": pick("statusesCount", "statuses_count", default=None),
        "followers": pick("followersCount", "followers_count", default=None),
        "following": pick("friendsCount", "friends_count", default=None),
        "verified": bool(pick("verified", "is_blue_verified", default=False)),
    }
    return {"profile": profile, "posts": posts}


# ------------------------------------------------------------------------- hashtag

def nitter_media(src):
    """Turn a Nitter-proxied image URL into (kind, direct pbs.twimg.com URL)."""
    path = urlparse(src).path
    m = re.match(r"^/pic/(?:orig/)?(.+)$", path)
    if not m:
        return None, ""
    real = unquote(m.group(1))
    if real.startswith("enc/"):
        try:
            blob = real[4:]
            real = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4)).decode()
        except Exception:
            return None, ""
    real = real.split("?")[0].lstrip("/")
    if real.startswith("pbs.twimg.com/"):
        return "avatar", "https://" + real
    if real.startswith("media/"):
        return "photo", f"https://pbs.twimg.com/{real}"
    if "video_thumb" in real:
        return "video", f"https://pbs.twimg.com/{real}"
    return None, ""


def parse_nitter(root):
    ns = {"dc": "http://purl.org/dc/elements/1.1/"}
    posts = []
    for item in root.iterfind(".//item"):
        link = item.findtext("link", "")
        m = re.search(r"/([A-Za-z0-9_]+)/status/(\d+)", link)
        if not m:
            continue
        handle, tid = m.group(1), m.group(2)
        post_url = f"https://x.com/{handle}/status/{tid}"
        desc = item.findtext("description", "")
        media = []
        for img in re.findall(r'<img[^>]+src="([^"]+)"', desc):
            kind, direct = nitter_media(htmllib.unescape(img))
            if kind == "photo":
                media.append({"type": "photo", "thumb": f"{direct}?name=large",
                              "src": f"{direct}?name=large", "download": f"{direct}?name=orig"})
            elif kind == "video":
                media.append({"type": "video", "thumb": direct, "src": "",
                              "download": f"tweet:{post_url}:0"})
        text = re.sub(r"<br\s*/?>", "\n", desc)
        text = htmllib.unescape(re.sub(r"<[^>]+>", " ", text))
        text = re.sub(r"[ \t]+", " ", text).strip()
        creator = (item.findtext("dc:creator", "", ns) or "").lstrip("@")
        posts.append({
            "id": tid, "url": post_url, "text": text,
            "created_at": item.findtext("pubDate", ""),
            "likes": 0, "replies": 0, "reposts": 0, "retweeted_by": None,
            "media": media,
            "user": {"name": creator or handle, "screen_name": handle,
                     "avatar": "", "verified": False},
        })
    return posts


def fetch_hashtag(tag):
    if not NITTER_URLS:
        raise ViewerError(
            "Hashtag search isn't set up on this server. X requires login for search, so add "
            "a working Nitter instance to the NITTER_URLS environment variable.", 501)
    for base in NITTER_URLS:
        try:
            r = http_get(f"{base}/search/rss", params={"f": "tweets", "q": f"#{tag}"})
            root = ET.fromstring(r.content)
        except (ViewerError, ET.ParseError) as e:
            log.warning("Nitter source %s failed: %s", base, e)
            continue
        return {"tag": tag, "posts": parse_nitter(root)}
    raise ViewerError("The hashtag search sources didn't respond. Try again shortly.", 502)


def fetch_profile_nitter(handle):
    for base in NITTER_URLS:
        try:
            r = http_get(f"{base}/{quote(handle)}/rss")
            root = ET.fromstring(r.content)
        except (ViewerError, ET.ParseError):
            continue
        ch = root.find("channel")
        if ch is None:
            continue
        name = (ch.findtext("title", "") or "").split(" / @")[0].strip() or handle
        avatar = ""
        img = ch.find("image/url")
        if img is not None and img.text:
            kind, direct = nitter_media(htmllib.unescape(img.text))
            avatar = direct if kind == "avatar" else ""
        posts = parse_nitter(root)
        for p in posts:
            if p["user"]["screen_name"].lower() == handle.lower():
                p["user"]["name"], p["user"]["avatar"] = name, avatar
        return {"profile": {"screen_name": handle, "name": name, "avatar": avatar, "bio": "",
                            "posts": None, "followers": None, "following": None,
                            "verified": False}, "posts": posts}
    raise ViewerError("The backup source didn't respond.", 502)


def fetch_profile(handle):
    try:
        return fetch_profile_syndication(handle)
    except ViewerError as e:
        if e.status in (429, 403, 502, 504) and NITTER_URLS:
            try:
                return fetch_profile_nitter(handle)
            except ViewerError:
                pass
        raise


# ------------------------------------------------------------- yt-dlp (tweet media)
def ytdlp_info(tweet_url):
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "socket_timeout": TIMEOUT, "noplaylist": False}
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(tweet_url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).lower()
        if "429" in msg or "rate" in msg:
            raise ViewerError("X is rate-limiting this server. Wait a minute and retry.", 429)
        if "no video" in msg:
            raise ViewerError("This post has no video. yt-dlp can only read video posts.", 422)
        if "protected" in msg or "private" in msg or "login" in msg or "age" in msg:
            raise ViewerError("This post isn't publicly viewable.", 403)
        raise ViewerError("Couldn't read this post. It may be deleted or restricted.", 404)
    except Exception:
        log.exception("yt-dlp failure")
        raise ViewerError("Unexpected error while reading this post.", 500)


def best_video_url(entry):
    if entry.get("url", "").startswith("http") and entry.get("ext"):
        direct = entry["url"]
    else:
        direct = None
    fmts = [f for f in entry.get("formats", [])
            if f.get("protocol") in ("https", "http") and f.get("ext") == "mp4" and f.get("url")]
    if fmts:
        return max(fmts, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))["url"]
    return direct


def syn_token(tid):
    """Token used by X's public tweet-result endpoint (mirrors the embed script)."""
    x = int(tid) / 1e15 * math.pi
    ip, frac = int(x), x - int(x)
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    s, n = "", ip
    while n:
        s, n = alphabet[n % 36] + s, n // 36
    for _ in range(12):
        frac *= 36
        d = int(frac)
        s += alphabet[d]
        frac -= d
    return re.sub(r"0+", "", s)


def fetch_tweet_syndication(tweet_url):
    tid = re.search(r"/status/(\d+)", tweet_url).group(1)
    r = http_get("https://cdn.syndication.twimg.com/tweet-result",
                 params={"id": tid, "lang": "en", "token": syn_token(tid)})
    try:
        t = r.json()
    except ValueError:
        raise ViewerError("Couldn't read this post.", 502)
    if not t.get("id_str") or t.get("__typename") == "TweetTombstone":
        raise ViewerError("This post isn't available publicly.", 404)
    t["extended_entities"] = {"media": t.get("mediaDetails") or []}
    return {"posts": [normalize_tweet(t)]}


def fetch_tweet(tweet_url):
    try:
        return fetch_tweet_ytdlp(tweet_url)
    except ViewerError as first:
        if first.status not in (404, 422):
            raise
        try:
            return fetch_tweet_syndication(tweet_url)
        except ViewerError:
            raise first


def fetch_tweet_ytdlp(tweet_url):
    info = ytdlp_info(tweet_url)
    entries = info.get("entries") or [info]
    media = []
    for i, e in enumerate(x for x in entries if x):
        src = best_video_url(e)
        if src:
            media.append({
                "type": "video", "thumb": e.get("thumbnail", ""), "src": src,
                "download": f"tweet:{tweet_url}:{i}",
            })
    handle = info.get("uploader_id") or ""
    ts = info.get("timestamp")
    return {"posts": [{
        "id": str(info.get("id", "")),
        "url": tweet_url,
        "text": (info.get("description") or info.get("title") or "").strip(),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else "",
        "likes": info.get("like_count") or 0,
        "replies": info.get("comment_count") or 0,
        "reposts": info.get("repost_count") or 0,
        "retweeted_by": None,
        "media": media,
        "user": {"name": info.get("uploader") or handle, "screen_name": handle,
                 "avatar": "", "verified": False},
    }]}


# --------------------------------------------------------------------------- routes
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return jsonify(ok=True)


def api(producer):
    try:
        return jsonify(producer())
    except ViewerError as e:
        return jsonify(error=e.message), e.status
    except Exception:
        log.exception("Unhandled error")
        return jsonify(error="Something went wrong on our side. Try again."), 500


@app.get("/api/profile")
def api_profile():
    def run():
        handle = clean_handle(request.args.get("u"))
        return cached(("p", handle.lower()), lambda: fetch_profile(handle))
    return api(run)


@app.get("/api/hashtag")
def api_hashtag():
    def run():
        tag = (request.args.get("q") or "").strip().lstrip("#")
        if not TAG_RE.match(tag):
            raise ViewerError("Enter a valid hashtag (letters, numbers, underscore).", 400)
        return cached(("h", tag.lower()), lambda: fetch_hashtag(tag))
    return api(run)


@app.get("/api/tweet")
def api_tweet():
    def run():
        url = (request.args.get("url") or "").strip()
        if not TWEET_URL_RE.match(url):
            raise ViewerError("That doesn't look like a post link.", 400)
        return cached(("t", url), lambda: fetch_tweet(url))
    return api(run)


def safe_filename(url, content_type):
    name = os.path.basename(urlparse(url).path) or "media"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:80]
    if "." not in name:
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "video/mp4": ".mp4"}.get(
            (content_type or "").split(";")[0], "")
        name += ext
    q = parse_qs(urlparse(url).query)
    if name.lower().endswith(("jpg", "png")) is False and q.get("format"):
        name += "." + re.sub(r"\W", "", q["format"][0])
    return name


@app.get("/download")
def download():
    """Stream media through the server. Accepts ?url=<twimg URL> or ?tweet=<post URL>&i=<n>."""
    try:
        target = request.args.get("url", "")
        tweet = request.args.get("tweet", "")
        if tweet:
            if not TWEET_URL_RE.match(tweet):
                raise ViewerError("Invalid post link.", 400)
            info = ytdlp_info(tweet)
            entries = [e for e in (info.get("entries") or [info]) if e]
            idx = min(max(int(request.args.get("i", 0) or 0), 0), len(entries) - 1)
            target = best_video_url(entries[idx]) or ""
        p = urlparse(target)
        if p.scheme != "https" or p.hostname not in ALLOWED_DL_HOSTS:
            raise ViewerError("Only media hosted by X can be downloaded.", 400)

        upstream = requests.get(target, headers=HEADERS, stream=True, timeout=TIMEOUT,
                                allow_redirects=False)
        if upstream.status_code != 200:
            raise ViewerError("The media is no longer available.", 404)
        ctype = upstream.headers.get("Content-Type", "application/octet-stream")
        name = request.args.get("name") or safe_filename(target, ctype)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:100]
        headers = {"Content-Type": ctype,
                   "Content-Disposition": f'attachment; filename="{name}"',
                   "Cache-Control": "private, max-age=300"}
        if upstream.headers.get("Content-Length"):
            headers["Content-Length"] = upstream.headers["Content-Length"]
        return Response(stream_with_context(upstream.iter_content(65536)), headers=headers)
    except ViewerError as e:
        return jsonify(error=e.message), e.status
    except requests.RequestException:
        return jsonify(error="Download failed. Try again."), 502
    except Exception:
        log.exception("download failure")
        return jsonify(error="Download failed. Try again."), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
