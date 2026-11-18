#!/usr/bin/env python3
"""
Hooda Nutrition Uttam Nagar - Instagram repost bot

Watches the main brand accounts, copies every new post/reel to the store page:
  * swaps phone numbers and address in the caption for the store's details
  * adds the 3-second store outro to the end of every reel
  * sends each item to Telegram for Approve / Edit caption / Reject
  * publishes approved items through the official Instagram Graph API

Run:  python3 hn_repost.py            (normal loop)
      python3 hn_repost.py --once     (one check, then exit - for testing)
      python3 hn_repost.py --test-caption "text"   (show caption rewrite)
"""
import json, os, re, sys, time, sqlite3, subprocess, shutil, logging, hashlib, argparse
from datetime import datetime, timezone
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("hn")

# ----------------------------------------------------------------- config / db
def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    for k in ("outro_path", "data_dir"):
        if not os.path.isabs(cfg[k]):
            cfg[k] = os.path.join(BASE, cfg[k])
    os.makedirs(cfg["data_dir"], exist_ok=True)
    os.makedirs(os.path.join(cfg["data_dir"], "media"), exist_ok=True)
    # secrets can come from environment variables (GitHub Actions secrets)
    for key, env in (("access_token", "HN_ACCESS_TOKEN"), ("telegram_bot_token", "HN_TELEGRAM_TOKEN"),
                     ("telegram_chat_id", "HN_TELEGRAM_CHAT_ID"), ("spare_ig_username", "HN_SPARE_IG_USERNAME"),
                     ("proxy", "HN_PROXY")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    if os.environ.get("HN_IG_SESSION_B64"):
        import base64
        p = os.path.join(cfg["data_dir"], "ig_session")
        with open(p, "wb") as f:
            f.write(base64.b64decode(os.environ["HN_IG_SESSION_B64"]))
        cfg["spare_ig_session_file"] = p
    return cfg


def save_token(cfg_path, token):
    with open(cfg_path) as f:
        raw = json.load(f)
    raw["access_token"] = token
    tmp = cfg_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(raw, f, indent=2)
    os.replace(tmp, cfg_path)


def open_db(cfg):
    db = sqlite3.connect(os.path.join(cfg["data_dir"], "state.db"))
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS items(
        media_id TEXT PRIMARY KEY,
        fingerprint TEXT,
        source TEXT,
        media_type TEXT,
        permalink TEXT,
        original_caption TEXT,
        caption TEXT,
        status TEXT,            -- seen | pending | approved | posted | rejected | failed
        files TEXT,             -- json list of local files / urls
        tg_message_id INTEGER,
        created REAL,
        approved_at REAL,
        posted_at REAL,
        posted_id TEXT,
        error TEXT);
    CREATE INDEX IF NOT EXISTS ix_fp ON items(fingerprint);
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
    """)
    cols = [r[1] for r in db.execute("PRAGMA table_info(items)")]
    if "raw" not in cols:
        db.execute("ALTER TABLE items ADD COLUMN raw TEXT"); db.commit()
    return db


def kv_get(db, k, default=None):
    r = db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def kv_set(db, k, v):
    db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, str(v)))
    db.commit()

# ----------------------------------------------------------------- caption
MOBILE_RE = re.compile(r"(?<!\d)(?:\+?91[\s\-]?|0)?[6-9]\d{4}[\s\-]?\d{5}(?!\d)")
# runs of several numbers joined by , / & | "and" etc.
def _num_run_re(num_pat):
    return re.compile(r"(?:%s)(?:\s*(?:,|/|&|\||and|or|-)?\s*(?:%s))*" % (num_pat, num_pat), re.I)


def rewrite_caption(text, cfg):
    if not text:
        return text or ""
    out = text
    mine = cfg["my_phone_text"]

    # 1) phone numbers -> store numbers (a run of 2+ numbers becomes one copy)
    if cfg.get("replace_any_indian_mobile", True):
        num_pat = MOBILE_RE.pattern
    else:
        nums = [re.escape(n[:5]) + r"[\s\-]?" + re.escape(n[5:]) for n in cfg["their_phone_numbers"]]
        num_pat = "(?:" + "|".join(nums) + ")" if nums else r"(?!x)x"
    out = _num_run_re(num_pat).sub(mine, out)

    # 2) address lines -> store address
    kws = [k.lower() for k in cfg.get("address_keywords", [])]
    cont = [k.lower() for k in cfg.get("address_continuation_keywords", [])]
    lines, new_lines, addr_done, prev_addr, prev_comma = out.split("\n"), [], False, False, False
    for ln in lines:
        low = ln.lower()
        is_addr = any(k in low for k in kws) or (
            prev_addr and any(k in low for k in cont) and (prev_comma or re.search(r"\b\d{6}\b", ln)))
        prev_addr, prev_comma = is_addr, ln.rstrip().endswith(",")
        if is_addr:
            if not addr_done:
                prefix = re.match(r"^\s*(?:[^\w\s]{1,3}\s*)?", ln).group(0)  # keep a leading emoji like 📍
                new_lines.append(prefix + cfg["my_address_text"])
                addr_done = True
            # drop further lines of the same address block
            continue
        new_lines.append(ln)
    out = "\n".join(new_lines)

    # 3) simple word swaps
    for a, b in cfg.get("extra_replacements", {}).items():
        out = re.sub(re.escape(a), b, out, flags=re.I)
    return out

def caption_key(text):
    """Comparable core of a caption: no hashtags, footer, numbers, emoji or punctuation."""
    t = (text or "").split("📍 Hooda Nutrition")[0]
    t = HASHTAG_RE.sub(" ", t)
    t = re.sub(r"\d", " ", t)
    t = re.sub(r"[^\w\u0900-\u097F]+", " ", t, flags=re.UNICODE)
    return " ".join(t.lower().split())[:120]


def image_hash(path):
    """64-bit difference hash; similar images give similar hashes. Ignores the top-left badge area."""
    from PIL import Image
    im = Image.open(path).convert("L")
    w, h = im.size
    im = im.crop((int(w * 0.3), int(h * 0.15), w, int(h * 0.85)))   # skip the badge corner and top/bottom bars
    im = im.resize((9, 8))
    px = list(im.getdata())
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | (px[r * 9 + c] > px[r * 9 + c + 1])
    return bits


HASHTAG_RE = re.compile(r"(?<![\w#])#[\w\u0900-\u097F]+", re.UNICODE)
MAX_TAGS, MAX_CAPTION = 30, 2200          # Instagram limits - going over makes the post fail


def finalize_caption(text, cfg):
    """Add the store footer + store hashtags, keep the original hashtags, stay under Instagram's limits.
    Safe to run more than once on the same caption."""
    text = (text or "").rstrip()
    marker = cfg.get("footer_marker", "📍 Hooda Nutrition, Uttam Nagar")
    footer_lines = cfg.get("caption_footer", [])
    if marker in text:                      # already finalized
        return text
    # 1) hashtags: originals first, then store tags, max 30 in total
    found, seen = [], set()
    for t in HASHTAG_RE.findall(text):
        if t.lower() not in seen:
            seen.add(t.lower()); found.append(t)
    original_count = len(found)
    if original_count > MAX_TAGS:           # trim extra original tags from the text itself
        extra = {t.lower() for t in found[MAX_TAGS:]}
        text = HASHTAG_RE.sub(lambda m: "" if m.group(0).lower() in extra else m.group(0), text)
        text = re.sub(r"[ \t]{2,}", " ", text).rstrip()
        found = found[:MAX_TAGS]
    add = []
    for t in cfg.get("store_hashtags", []):
        t = t if t.startswith("#") else "#" + t
        if len(found) + len(add) >= MAX_TAGS:
            break
        if t.lower() not in seen:
            seen.add(t.lower()); add.append(t)
    # 2) footer (skip the phone line if the caption already shows the store numbers)
    lines = [l for l in footer_lines if not (cfg["my_phone_text"].split(",")[0].strip() in text and "📞" in l)]
    footer = "\n".join([marker] + lines)
    parts = [text] if text else []
    parts.append(footer)
    if add:
        parts.append(" ".join(add))
    out = "\n\n".join(parts)
    # 3) length limit: shorten the original text, never the footer/tags
    if len(out) > MAX_CAPTION:
        tail = "\n\n" + "\n\n".join(parts[1:])
        room = MAX_CAPTION - len(tail) - 1
        out = text[:max(room, 0)].rstrip() + "…" + tail
    return out

# ----------------------------------------------------------------- graph api
class IG:
    def __init__(self, cfg, cfg_path):
        self.cfg, self.cfg_path = cfg, cfg_path
        self.v = cfg["graph_api_version"]
        self.g = f"https://graph.facebook.com/{self.v}"

    @property
    def tok(self):
        return self.cfg["access_token"]

    def _check(self, r):
        try:
            j = r.json()
        except Exception:
            r.raise_for_status(); raise
        if "error" in j:
            raise RuntimeError(f"Graph API: {j['error'].get('message')} (code {j['error'].get('code')})")
        return j

    def get(self, path, **params):
        params["access_token"] = self.tok
        return self._check(requests.get(f"{self.g}/{path}", params=params, timeout=60))

    def post(self, path, **params):
        params["access_token"] = self.tok
        return self._check(requests.post(f"{self.g}/{path}", data=params, timeout=120))

    def recent_media(self, username, limit=12, stats=False):
        extra = ",like_count,comments_count,view_count" if stats else ""
        fields = ("business_discovery.username(%s){media.limit(%d){id,caption,media_type,media_product_type,"
                  "media_url,thumbnail_url,permalink,timestamp%s,children{media_type,media_url}}}" % (username, limit, extra))
        try:
            j = self.get(self.cfg["ig_user_id"], fields=fields)
        except RuntimeError:
            if not stats:
                raise
            j = self.get(self.cfg["ig_user_id"], fields=fields.replace(",view_count", ""))
        return j.get("business_discovery", {}).get("media", {}).get("data", [])

    def own_media(self, limit=100):
        out, url, params = [], f"{self.g}/{self.cfg['ig_user_id']}/media", {
            "fields": "id,caption,media_type,thumbnail_url,media_url,timestamp,permalink",
            "limit": 50, "access_token": self.tok}
        while url and len(out) < limit:
            j = self._check(requests.get(url, params=params, timeout=60))
            out += j.get("data", [])
            url, params = j.get("paging", {}).get("next"), None
        return out[:limit]

    def refresh_token(self):
        j = self._check(requests.get(f"{self.g}/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": self.cfg["fb_app_id"],
            "client_secret": self.cfg["fb_app_secret"], "fb_exchange_token": self.tok}, timeout=60))
        self.cfg["access_token"] = j["access_token"]
        save_token(self.cfg_path, j["access_token"])
        log.info("access token refreshed")

    # ---- publishing
    def _wait(self, cid, max_wait=600):
        t0, last = time.time(), {}
        while time.time() - t0 < max_wait:
            last = self.get(cid, fields="status_code,status,video_status")
            st = last.get("status_code")
            if st == "FINISHED":
                return
            if st in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"Instagram rejected the video: {last.get('status')}")
            up = (last.get("video_status") or {}).get("uploading_phase") or {}
            if up.get("status") == "error":
                raise RuntimeError(f"upload error: {up.get('errors')}")
            time.sleep(8)
        vs = last.get("video_status") or {}
        raise RuntimeError("timed out waiting for Instagram to process the video "
                           f"(status: {last.get('status')}; upload: {(vs.get('uploading_phase') or {}).get('status')}, "
                           f"bytes {(vs.get('uploading_phase') or {}).get('bytes_transferred')}; "
                           f"processing: {(vs.get('processing_phase') or {}).get('status')})")

    def _container(self, media_type, caption, carousel_item, **extra):
        params = dict(media_type=media_type, **{k: v for k, v in extra.items() if v})
        if caption is not None:
            params["caption"] = caption
        if media_type == "REELS":
            params["share_to_feed"] = "true"
        if carousel_item:
            params["is_carousel_item"] = "true"
        return self.post(f"{self.cfg['ig_user_id']}/media", **params)

    def _upload_video(self, path, media_type, caption=None, carousel_item=False, extra=None):
        extra = extra or {}
        """1) resumable upload of the file (twice)  2) fallback: host the file publicly and let Instagram fetch it."""
        errors = []
        for attempt in (1, 2):
            try:
                j = self._container(media_type, caption, carousel_item, upload_type="resumable", **extra)
                cid = j["id"]
                uri = j.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{self.v}/{cid}"
                size = os.path.getsize(path)
                with open(path, "rb") as f:
                    r = requests.post(uri, headers={"Authorization": f"OAuth {self.tok}", "offset": "0",
                                                    "file_size": str(size)}, data=f, timeout=900)
                log.info("upload try %d %s (%d bytes) -> HTTP %s %s", attempt, cid, size, r.status_code, r.text[:300])
                if r.status_code != 200 or '"success":true' not in r.text.replace(" ", ""):
                    raise RuntimeError(f"upload HTTP {r.status_code}: {r.text[:200]}")
                self._wait(cid)
                return cid
            except Exception as e:
                errors.append(f"try {attempt}: {e}")
                log.warning("resumable upload failed (%s)", e)
                time.sleep(15)
        url = public_host(path)
        if url:
            try:
                log.info("falling back to hosted URL upload")
                cid = self._container(media_type, caption, carousel_item, video_url=url, **extra)["id"]
                self._wait(cid)
                return cid
            except Exception as e:
                errors.append(f"hosted: {e}")
        raise RuntimeError("video upload failed - " + " | ".join(errors))

    def _image(self, url, caption=None, carousel_item=False):
        params = dict(image_url=url)
        if caption is not None:
            params["caption"] = caption
        if carousel_item:
            params["is_carousel_item"] = "true"
        cid = self.post(f"{self.cfg['ig_user_id']}/media", **params)["id"]
        self._wait(cid, 120)
        return cid

    def publish(self, cid):
        return self.post(f"{self.cfg['ig_user_id']}/media_publish", creation_id=cid)["id"]

    def publish_item(self, kind, files, caption):
        if kind == "REEL":
            extra = {}
            cov = files[0].get("cover")
            if cov and os.path.exists(cov):
                url = public_host(cov)
                if url:
                    extra["cover_url"] = url
            cid = self._upload_video(files[0]["path"], "REELS", caption, extra=extra)
        elif kind == "IMAGE":
            cid = self._image(files[0]["url"], caption)
        elif kind == "STORY":
            cid = self._upload_video(files[0]["path"], "STORIES")
        elif kind == "CAROUSEL":
            kids = []
            for f in files[:10]:
                if f["type"] == "VIDEO":
                    kids.append(self._upload_video(f["path"], "VIDEO", carousel_item=True))
                else:
                    kids.append(self._image(f["url"], carousel_item=True))
            cid = self.post(f"{self.cfg['ig_user_id']}/media", media_type="CAROUSEL",
                            children=",".join(kids), caption=caption)["id"]
            self._wait(cid, 300)
        else:
            raise RuntimeError(f"unknown kind {kind}")
        return self.publish(cid)

# ----------------------------------------------------------------- media
def run(cmd, **kw):
    log.debug("run: %s", " ".join(cmd))
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def download(url, dest, proxy=False):
    px = {"http": PROXY, "https": PROXY} if (proxy and PROXY) else None
    with requests.get(url, stream=True, timeout=120, proxies=px) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return dest


START_TIME = time.time()
PROXY = None      # set from config "proxy" (e.g. a residential proxy) - used only for unofficial downloads
STORY_WATCHER = None


def download_reel(item, dest):
    """1) official API link  2) spare-account session (instaloader)  3) yt-dlp."""
    if item.get("media_url"):
        try:
            return download(item["media_url"], dest)
        except Exception as e:
            log.warning("api download failed: %s", e)
    if STORY_WATCHER:
        try:
            url = STORY_WATCHER.reel_url(item["permalink"])
            return download(url, dest, proxy=True)
        except Exception as e:
            log.warning("spare-session reel download failed: %s", e)
    ytdlp = shutil.which("yt-dlp") or "yt-dlp"
    cmd = [ytdlp, "-q", "--no-warnings", "-f", "mp4/bestvideo*+bestaudio/best", "--merge-output-format", "mp4",
           "-o", dest, item["permalink"]]
    if PROXY:
        cmd[1:1] = ["--proxy", PROXY]
    run(cmd, timeout=600)
    return dest


def probe(path):
    j = json.loads(run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                        "-of", "json", path]).stdout)
    has_audio = any(s.get("codec_type") == "audio" for s in j.get("streams", []))
    return float(j["format"]["duration"]), has_audio


def add_outro(src, outro, dest, encoder="libx264", watermark=None):
    """watermark = dict(path=..., width=220, x=40, y=150, opacity=0.25) - faint logo on the main video (not the outro)"""
    dur, has_audio = probe(src)
    _, outro_audio = probe(outro)
    fade_st = max(dur - 0.45, 0)
    norm = ("scale=1080:1920:force_original_aspect_ratio=decrease,"
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,fps=30,setsar=1")
    afmt = "aformat=sample_rates=48000:channel_layouts=stereo"
    inputs = ["-i", src, "-i", outro]
    n = 2
    fc = []
    if watermark and os.path.exists(watermark["path"]):
        inputs += ["-i", watermark["path"]]
        fc.append(f"[0:v]{norm}[m0]")
        fc.append(f"[{n}:v]scale={watermark.get('width', 220)}:-1,format=rgba,"
                  f"colorchannelmixer=aa={watermark.get('opacity', 0.25)}[wm]")
        fc.append(f"[m0][wm]overlay={watermark.get('x', 40)}:{watermark.get('y', 150)},format=yuv420p[v0]")
        n += 1
    else:
        fc.append(f"[0:v]{norm},format=yuv420p[v0]")
    fc.append(f"[1:v]{norm},format=yuv420p[v1]")
    if has_audio:
        fc.append(f"[0:a]{afmt},afade=t=out:st={fade_st:.2f}:d=0.45[a0]")
    else:
        inputs += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        fc.append(f"[{n}:a]{afmt}[a0]"); n += 1
    if outro_audio:
        fc.append(f"[1:a]{afmt}[a1]")
    else:
        inputs += ["-f", "lavfi", "-t", "3", "-i", "anullsrc=r=48000:cl=stereo"]
        fc.append(f"[{n}:a]{afmt}[a1]"); n += 1
    fc.append("[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]")
    venc = ["-c:v", encoder]
    venc += ["-b:v", "6M"] if "videotoolbox" in encoder else ["-preset", "veryfast", "-crf", "21", "-maxrate", "6M", "-bufsize", "12M"]
    run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(fc),
         "-map", "[v]", "-map", "[a]", *venc, "-pix_fmt", "yuv420p", "-profile:v", "high",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", dest], timeout=1800)
    return dest


def make_cover(cfg, m, video, dest):
    """Reel cover: original thumbnail (or a frame of the video) at 1080x1920 with the store badge."""
    src = dest + "_src.jpg"
    got = False
    if m.get("thumbnail_url"):
        try:
            download(m["thumbnail_url"], src); got = True
        except Exception as e:
            log.warning("thumbnail download failed: %s", e)
    if not got:
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", video, "-frames:v", "1", src])
    c = cfg.get("cover", {})
    badge = c.get("badge", os.path.join(BASE, "assets", "badge.png"))
    if not os.path.isabs(badge):
        badge = os.path.join(BASE, badge)
    fc = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920[b];"
          f"[1:v]scale={c.get('width', 220)}:-1[l];[b][l]overlay={c.get('x', 40)}:{c.get('y', 290)}")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-i", badge, "-filter_complex", fc,
         "-frames:v", "1", "-q:v", "2", dest])
    os.remove(src)
    return dest


def public_host(path):
    """Upload the file as an asset of a 'media' release in this GitHub repo and return a public URL.
    Only works inside GitHub Actions (needs GH_TOKEN + GITHUB_REPOSITORY)."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (repo and os.environ.get("GH_TOKEN") and shutil.which("gh")):
        return None
    try:
        subprocess.run(["gh", "release", "view", "media", "-R", repo], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        subprocess.run(["gh", "release", "create", "media", "-R", repo, "-t", "media (temporary uploads)",
                        "-n", "Temporary video files for Instagram uploads"], check=True, capture_output=True)
    name = f"{int(time.time())}_{os.path.basename(path)}"
    tmp = os.path.join(os.path.dirname(path), name)
    shutil.copy(path, tmp)
    subprocess.run(["gh", "release", "upload", "media", tmp, "-R", repo, "--clobber"], check=True, capture_output=True)
    os.remove(tmp)
    return f"https://github.com/{repo}/releases/download/media/{name}"


def image_to_story_video(img, dest, seconds=6):
    """Turn a photo story into a short 1080x1920 video so it can be uploaded directly (no public URL needed)."""
    vf = ("scale=1080:1920:force_original_aspect_ratio=decrease,"
          "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,fps=30,format=yuv420p")
    run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-t", str(seconds), "-i", img,
         "-f", "lavfi", "-t", str(seconds), "-i", "anullsrc=r=48000:cl=stereo",
         "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-shortest",
         "-movflags", "+faststart", dest], timeout=300)
    return dest


class StoryWatcher:
    """Reads the main accounts' stories through a SPARE Instagram account (instaloader session).
    Instagram's official API cannot read other accounts' stories."""
    def __init__(self, cfg):
        import instaloader
        self.cfg = cfg
        self.L = instaloader.Instaloader(quiet=True, download_pictures=False, download_videos=False,
                                         save_metadata=False, compress_json=False)
        user = cfg["spare_ig_username"]
        sess = cfg.get("spare_ig_session_file") or None
        self.L.load_session_from_file(user, sess)
        if cfg.get("proxy"):
            self.L.context._session.proxies.update({"http": cfg["proxy"], "https": cfg["proxy"]})
        self._ids = {}

    def reel_url(self, permalink):
        import instaloader
        code = permalink.rstrip("/").split("/")[-1]
        post = instaloader.Post.from_shortcode(self.L.context, code)
        if not post.is_video:
            raise RuntimeError("not a video")
        return post.video_url

    def stories(self, username):
        import instaloader
        if username not in self._ids:
            self._ids[username] = instaloader.Profile.from_username(self.L.context, username).userid
        out = []
        for st in self.L.get_stories(userids=[self._ids[username]]):
            for it in st.get_items():
                out.append({"id": f"story_{it.mediaid}", "is_video": it.is_video,
                            "url": it.video_url if it.is_video else it.url,
                            "timestamp": it.date_utc.isoformat(),
                            "permalink": f"https://www.instagram.com/stories/{username}/{it.mediaid}/"})
        return out

# ----------------------------------------------------------------- telegram
class TG:
    def __init__(self, cfg):
        self.u = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}"
        self.chat = str(cfg["telegram_chat_id"])
        self.offset = 0

    def call(self, method, files=None, http_timeout=60, **data):
        r = requests.post(f"{self.u}/{method}", data=data, files=files, timeout=http_timeout)
        j = r.json()
        if not j.get("ok"):
            raise RuntimeError(f"Telegram {method}: {j.get('description')}")
        return j["result"]

    def text(self, msg, **kw):
        return self.call("sendMessage", chat_id=self.chat, text=msg[:4000], **kw)

    @staticmethod
    def kb(mid):
        return json.dumps({"inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"ok:{mid}"},
            {"text": "✏️ Edit caption", "callback_data": f"ed:{mid}"},
            {"text": "❌ Reject", "callback_data": f"no:{mid}"}]]})

    def send_review(self, row, files):
        cap = row["caption"] or ("(stories have no caption)" if row["media_type"] == "STORY" else "(no caption)")
        head = f"🆕 {row['media_type']} from @{row['source']}\n{row['permalink']}\n\n📝 New caption:\n"
        body = (head + cap)[:1000]
        kb = self.kb(row["media_id"])
        first = files[0]
        try:
            vpath = first.get("path")
            if vpath and os.path.getsize(vpath) >= 49_000_000:
                small = vpath.rsplit(".", 1)[0] + "_preview.mp4"
                run(["ffmpeg", "-y", "-loglevel", "error", "-i", vpath, "-vf", "scale=540:-2", "-c:v", "libx264",
                     "-preset", "veryfast", "-crf", "30", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", small],
                    timeout=600)
                vpath = small
            if vpath and os.path.getsize(vpath) < 49_000_000:
                with open(vpath, "rb") as f:
                    m = self.call("sendVideo", files={"video": f}, http_timeout=300, chat_id=self.chat,
                                  caption=body, reply_markup=kb, supports_streaming="true")
            elif first.get("url"):
                m = self.call("sendPhoto", chat_id=self.chat, photo=first["url"], caption=body, reply_markup=kb)
            else:
                raise RuntimeError("no preview")
        except Exception as e:
            log.warning("preview send failed (%s), sending text only", e)
            m = self.text(body + f"\n\n(preview unavailable: {e})", reply_markup=kb)
        if first.get("cover") and os.path.exists(first["cover"]):
            try:
                with open(first["cover"], "rb") as f:
                    self.call("sendPhoto", files={"photo": f}, http_timeout=120, chat_id=self.chat,
                              caption="🖼 Cover (thumbnail) for this reel")
            except Exception as e:
                log.warning("cover preview failed: %s", e)
        if len(files) > 1:
            self.text(f"ℹ️ Carousel with {len(files)} items - only the first is previewed.")
        return m["message_id"]

    def updates(self, wait=20):
        try:
            res = self.call("getUpdates", http_timeout=wait + 15, offset=self.offset, timeout=wait,
                            allowed_updates=json.dumps(["message", "callback_query"]))
        except Exception as e:
            log.warning("telegram poll error: %s", e); time.sleep(5); return []
        if res:
            self.offset = res[-1]["update_id"] + 1
        return res

# ----------------------------------------------------------------- bot
class Bot:
    def __init__(self, cfg_path):
        self.cfg_path = cfg_path
        self.cfg = load_config(cfg_path)
        self.db = open_db(self.cfg)
        self.ig = IG(self.cfg, cfg_path)
        self.tg = TG(self.cfg)
        self.editing = None   # media_id waiting for a new caption
        global PROXY, STORY_WATCHER
        PROXY = self.cfg.get("proxy") or None
        self.stories = None
        if self.cfg.get("stories_enabled") or self.cfg.get("spare_ig_username", "").find("PUT_") != 0:
            try:
                STORY_WATCHER = StoryWatcher(self.cfg)
                self.stories = STORY_WATCHER if self.cfg.get("stories_enabled") else None
            except Exception as e:
                log.error("story watcher disabled: %s", e)
                self.tg.text(f"⚠️ Stories are switched off - couldn't load the spare Instagram session.\n{e}")

    # ---- detection
    @staticmethod
    def kind_of(m):
        if m.get("media_type") == "CAROUSEL_ALBUM":
            return "CAROUSEL"
        if m.get("media_type") == "VIDEO":
            return "REEL"
        return "IMAGE"

    @staticmethod
    def fingerprint(m):
        base = (m.get("caption") or "").strip()[:200] + "|" + (m.get("timestamp") or "")[:16]
        return hashlib.sha1(base.encode()).hexdigest()

    def check_sources(self):
        first_run = kv_get(self.db, "initialised") is None
        found = 0
        for user in self.cfg["source_accounts"]:
            try:
                media = self.ig.recent_media(user)
            except Exception as e:
                log.error("could not read @%s: %s", user, e)
                continue
            for m in reversed(media):           # oldest first
                fp = self.fingerprint(m)
                if self.db.execute("SELECT 1 FROM items WHERE media_id=? OR fingerprint=?", (m["id"], fp)).fetchone():
                    continue                     # already handled (also catches collab posts on both accounts)
                status = "seen" if (first_run and self.cfg.get("skip_existing_on_first_run", True)) else "new"
                self.db.execute("INSERT INTO items(media_id,fingerprint,source,media_type,permalink,original_caption,"
                                "status,created) VALUES(?,?,?,?,?,?,?,?)",
                                (m["id"], fp, user, self.kind_of(m), m.get("permalink"), m.get("caption"), status, time.time()))
                self.db.commit()
                if status == "new":
                    if self.already_on_store(m):
                        self.db.execute("UPDATE items SET status='on_store' WHERE media_id=?", (m["id"],))
                        self.db.commit(); continue
                    found += 1
                    self.prepare(m, user)
        if first_run:
            kv_set(self.db, "initialised", datetime.now(timezone.utc).isoformat())
            self.tg.text("✅ Repost bot is running. Existing posts were skipped; new ones will come here for approval.")
        if found:
            log.info("%d new item(s)", found)

    def check_stories(self):
        if not self.stories:
            return
        first_run = kv_get(self.db, "stories_initialised") is None
        for user in self.cfg.get("story_source_accounts", self.cfg["source_accounts"]):
            try:
                items = self.stories.stories(user)
            except Exception as e:
                log.error("could not read stories of @%s: %s", user, e)
                continue
            for it in items:
                if self.db.execute("SELECT 1 FROM items WHERE media_id=?", (it["id"],)).fetchone():
                    continue
                status = "seen" if first_run else "new"
                self.db.execute("INSERT INTO items(media_id,fingerprint,source,media_type,permalink,original_caption,"
                                "status,created) VALUES(?,?,?,?,?,?,?,?)",
                                (it["id"], it["id"], user, "STORY", it["permalink"], "", status, time.time()))
                self.db.commit()
                if status == "new":
                    self.prepare_story(it, user)
            time.sleep(5)   # be gentle with the spare account
        if first_run:
            kv_set(self.db, "stories_initialised", 1)

    def build_story_files(self, it):
        mid = it["id"]; mdir = os.path.join(self.cfg["data_dir"], "media")
        if it["is_video"]:
            raw = download(it["url"], os.path.join(mdir, f"{mid}_raw.mp4"), proxy=True)
            dur, _ = probe(raw)
            final = os.path.join(mdir, f"{mid}.mp4")
            if self.cfg.get("outro_on_stories", True) and dur + 3 <= 60:
                add_outro(raw, self.cfg["outro_path"], final, self.cfg["video_encoder"])
            else:
                run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-c:v", "libx264", "-preset", "veryfast",
                     "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", final], timeout=600)
            os.remove(raw)
        else:
            img = download(it["url"], os.path.join(mdir, f"{mid}.jpg"), proxy=True)
            final = image_to_story_video(img, os.path.join(mdir, f"{mid}.mp4"))
            os.remove(img)
        return [{"type": "VIDEO", "path": final}]

    def prepare_story(self, it, user):
        mid = it["id"]; mdir = os.path.join(self.cfg["data_dir"], "media")
        try:
            files = self.build_story_files(it)
            self.db.execute("UPDATE items SET caption='', files=?, raw=?, status='pending' WHERE media_id=?",
                            (json.dumps(files), json.dumps(it), mid)); self.db.commit()
            msg_id = self.tg.send_review(self.row(mid), files)
            self.db.execute("UPDATE items SET tg_message_id=? WHERE media_id=?", (msg_id, mid)); self.db.commit()
        except Exception as e:
            log.exception("story prepare failed")
            self.db.execute("UPDATE items SET status='failed', error=? WHERE media_id=?", (str(e), mid)); self.db.commit()
            self.tg.text(f"⚠️ Couldn't prepare a story from @{user}: {e}")

    # ---- already on the store account?
    def load_store_posts(self):
        """Captions + thumbnail hashes of everything already on the store account (cached for 30 min)."""
        if getattr(self, "_store_cache", None) and time.time() - self._store_cache[0] < 1800:
            return self._store_cache[1]
        posts = []
        cache = json.loads(kv_get(self.db, "store_hashes", "{}"))
        mdir = os.path.join(self.cfg["data_dir"], "media")
        for p in self.ig.own_media(150):
            h = cache.get(p["id"])
            thumb = p.get("thumbnail_url") or (p.get("media_url") if p.get("media_type") == "IMAGE" else None)
            if h is None and thumb:
                try:
                    tmp = download(thumb, os.path.join(mdir, f"own_{p['id']}.jpg"))
                    h = image_hash(tmp); os.remove(tmp); cache[p["id"]] = h
                except Exception as e:
                    log.warning("hash failed for own post %s: %s", p["id"], e)
            posts.append({"key": caption_key(p.get("caption")), "hash": h})
        kv_set(self.db, "store_hashes", json.dumps(cache))
        self._store_cache = (time.time(), posts)
        return posts

    def already_on_store(self, m):
        """True if this source post already appears on the store account (by caption or by thumbnail)."""
        import difflib
        try:
            posts = self.load_store_posts()
        except Exception as e:
            log.warning("could not read store posts: %s", e); return False
        key = caption_key(rewrite_caption(m.get("caption"), self.cfg))
        if len(key) >= 12:
            for p in posts:
                if p["key"] and (p["key"].startswith(key[:60]) or
                                 difflib.SequenceMatcher(None, key, p["key"]).ratio() > 0.85):
                    return True
        thumb = m.get("thumbnail_url") or m.get("media_url")
        if thumb and any(p["hash"] is not None for p in posts):
            try:
                tmp = download(thumb, os.path.join(self.cfg["data_dir"], "media", f"cand_{m['id']}.jpg"))
                h = image_hash(tmp); os.remove(tmp)
                for p in posts:
                    if p["hash"] is not None and bin(h ^ p["hash"]).count("1") <= 8:
                        return True
            except Exception as e:
                log.warning("thumbnail check failed: %s", e)
        return False

    # ---- "best of recent" picks
    @staticmethod
    def score(m):
        views = m.get("view_count") or 0
        likes = m.get("like_count") or 0
        comments = m.get("comments_count") or 0
        return (views if views else likes * 20) + comments * 50

    def pick_best(self, count=None, manual=False):
        count = count or self.cfg.get("best_per_day", 2)
        days = self.cfg.get("best_lookback_days", 30)
        min_age = self.cfg.get("best_min_age_days", 1)
        now = datetime.now(timezone.utc)
        cands = []
        for user in self.cfg["source_accounts"]:
            try:
                media = self.ig.recent_media(user, limit=self.cfg.get("best_scan_limit", 60), stats=True)
            except Exception as e:
                log.error("best: could not read @%s: %s", user, e); continue
            for m in media:
                if self.kind_of(m) != "REEL":
                    continue
                ts = datetime.strptime(m["timestamp"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                age = (now - ts).days
                if age < min_age or age > days:
                    continue
                r = self.db.execute("SELECT status FROM items WHERE media_id=? OR fingerprint=?",
                                    (m["id"], self.fingerprint(m))).fetchone()
                if r and r["status"] != "seen":
                    continue          # already offered, posted or rejected
                cands.append((self.score(m), age, user, m))
        cands.sort(key=lambda c: c[0], reverse=True)
        picks, skipped = [], 0
        for c in cands:
            if len(picks) >= count:
                break
            m = c[3]
            if self.already_on_store(m):
                skipped += 1
                if not self.db.execute("SELECT 1 FROM items WHERE media_id=?", (m["id"],)).fetchone():
                    self.db.execute("INSERT INTO items(media_id,fingerprint,source,media_type,permalink,status,created)"
                                    " VALUES(?,?,?,?,?,?,?)", (m["id"], self.fingerprint(m), c[2], "REEL",
                                                              m.get("permalink"), "on_store", time.time()))
                else:
                    self.db.execute("UPDATE items SET status='on_store' WHERE media_id=?", (m["id"],))
                self.db.commit()
                continue
            picks.append(c)
        if skipped:
            self.tg.text(f"ℹ️ Skipped {skipped} top reel(s) that are already on your account.")
        if not picks:
            if manual:
                self.tg.text(f"🔎 No new top reels found in the last {days} days.")
            return
        self.tg.text(f"🔥 {len(picks)} top-performing reel(s) from the last {days} days, for your approval:")
        for sc, age, user, m in picks:
            if not self.db.execute("SELECT 1 FROM items WHERE media_id=?", (m["id"],)).fetchone():
                self.db.execute("INSERT INTO items(media_id,fingerprint,source,media_type,permalink,original_caption,"
                                "status,created) VALUES(?,?,?,?,?,?,?,?)",
                                (m["id"], self.fingerprint(m), user, "REEL", m.get("permalink"), m.get("caption"),
                                 "new", time.time()))
            else:
                self.db.execute("UPDATE items SET status='new', created=? WHERE media_id=?", (time.time(), m["id"]))
            self.db.commit()
            stats = []
            if m.get("view_count"): stats.append(f"{m['view_count']:,} views")
            if m.get("like_count") is not None: stats.append(f"{m['like_count']:,} likes")
            if m.get("comments_count") is not None: stats.append(f"{m['comments_count']:,} comments")
            self.tg.text(f"🏆 @{user} · posted {age} days ago · " + " · ".join(stats))
            self.prepare(m, user)

    def maybe_pick_best(self):
        if not self.cfg.get("best_enabled", True):
            return
        ist = datetime.now(timezone.utc).timestamp() + 5.5 * 3600
        ist_dt = datetime.fromtimestamp(ist, timezone.utc)
        today = ist_dt.strftime("%Y-%m-%d")
        if ist_dt.hour >= self.cfg.get("best_hour_ist", 11) and kv_get(self.db, "best_date") != today:
            kv_set(self.db, "best_date", today)
            self.pick_best()

    def build_post_files(self, m):
        mid, kind = m["id"], self.kind_of(m)
        mdir = os.path.join(self.cfg["data_dir"], "media")
        files = []
        if kind == "REEL":
            raw = download_reel(m, os.path.join(mdir, f"{mid}_raw.mp4"))
            wm = None
            w = self.cfg.get("watermark", {})
            if w.get("enabled", True):
                badge = w.get("badge", "assets/badge.png")
                wm = dict(path=badge if os.path.isabs(badge) else os.path.join(BASE, badge), width=w.get("width", 220),
                          x=w.get("x", 40), y=w.get("y", 150), opacity=w.get("opacity", 0.25))
            final = add_outro(raw, self.cfg["outro_path"], os.path.join(mdir, f"{mid}.mp4"), self.cfg["video_encoder"], wm)
            f = {"type": "VIDEO", "path": final}
            if self.cfg.get("cover", {}).get("enabled", True):
                try:   # cover is made from the original (un-watermarked) video/thumbnail
                    f["cover"] = make_cover(self.cfg, m, raw, os.path.join(mdir, f"{mid}_cover.jpg"))
                except Exception as e:
                    log.warning("cover failed: %s", e)
            os.remove(raw)
            files.append(f)
        elif kind == "IMAGE":
            files.append({"type": "IMAGE", "url": m["media_url"]})
        else:
            for i, c in enumerate(m.get("children", {}).get("data", [])):
                if c["media_type"] == "VIDEO":
                    if not c.get("media_url"):
                        raise RuntimeError("a video inside this carousel can't be downloaded")
                    files.append({"type": "VIDEO", "path": download(c["media_url"], os.path.join(mdir, f"{mid}_{i}.mp4"))})
                else:
                    files.append({"type": "IMAGE", "url": c["media_url"]})
        return files

    def prepare(self, m, user):
        mid, kind = m["id"], self.kind_of(m)
        mdir = os.path.join(self.cfg["data_dir"], "media")
        try:
            files = self.build_post_files(m)
            caption = finalize_caption(rewrite_caption(m.get("caption"), self.cfg), self.cfg)
            self.db.execute("UPDATE items SET caption=?, files=?, raw=?, status='pending' WHERE media_id=?",
                            (caption, json.dumps(files), json.dumps(m), mid))
            self.db.commit()
            row = self.row(mid)
            msg_id = self.tg.send_review(row, files)
            self.db.execute("UPDATE items SET tg_message_id=? WHERE media_id=?", (msg_id, mid)); self.db.commit()
        except Exception as e:
            log.exception("prepare failed for %s", mid)
            self.db.execute("UPDATE items SET status='failed', error=? WHERE media_id=?", (str(e), mid)); self.db.commit()
            self.tg.text(f"⚠️ Couldn't prepare a {kind} from @{user}:\n{m.get('permalink')}\nReason: {e}")

    def row(self, mid):
        return self.db.execute("SELECT * FROM items WHERE media_id=?", (mid,)).fetchone()

    # ---- telegram handling
    def handle(self, upd):
        if "callback_query" in upd:
            q = upd["callback_query"]
            if str(q["message"]["chat"]["id"]) != self.tg.chat:
                return
            act, mid = q["data"].split(":", 1)
            row = self.row(mid)
            note = ""
            if not row or row["status"] != "pending":
                note = "Already handled"
            elif act == "ok":
                self.db.execute("UPDATE items SET status='approved', approved_at=? WHERE media_id=?", (time.time(), mid))
                note = "Approved - queued for posting"
            elif act == "no":
                self.db.execute("UPDATE items SET status='rejected' WHERE media_id=?", (mid,))
                note = "Rejected"
            elif act == "ed":
                self.editing = mid
                self.tg.text("✏️ Send the new caption as your next message.\nCurrent caption:\n\n" + (row["caption"] or ""))
                note = "Waiting for new caption"
            self.db.commit()
            try:
                self.tg.call("answerCallbackQuery", callback_query_id=q["id"], text=note)
                if act in ("ok", "no") and note != "Already handled":
                    self.tg.call("editMessageReplyMarkup", chat_id=self.tg.chat, message_id=q["message"]["message_id"],
                                 reply_markup=json.dumps({"inline_keyboard": [[{"text": note, "callback_data": "noop:0"}]]}))
            except Exception:
                pass
        elif "message" in upd:
            msg = upd["message"]
            txt = msg.get("text", "")
            if self.tg.chat.startswith("PUT_"):
                if txt.strip() in ("/start", "/id"):
                    self.tg.chat = str(msg["chat"]["id"])
                    self.tg.text(f"Your chat id is {msg['chat']['id']}\nPut it in config.json as telegram_chat_id and restart the bot.")
                    self.tg.chat = "PUT_"
                return
            if str(msg["chat"]["id"]) != self.tg.chat:
                return
            if txt.strip() in ("/start", "/id", "/help"):
                self.tg.text("Commands:\n/best - top-performing reels from the last 30 days\n/best 5 - top 5\n"
                             "/status - counts\n/check - check the main accounts now\n/retry - re-post failed items"); return
            if txt.strip() == "/status":
                c = dict(self.db.execute("SELECT status, COUNT(*) n FROM items GROUP BY status").fetchall())
                self.tg.text("📊 " + ", ".join(f"{k}: {v}" for k, v in c.items())); return
            if txt.strip().startswith("/best"):
                parts = txt.split()
                n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
                self.tg.text("🔎 Looking for top-performing reels...")
                self.pick_best(count=n, manual=True); return
            if txt.strip() == "/retry":
                n = self.db.execute("UPDATE items SET status='approved', approved_at=?, error=NULL "
                                    "WHERE status='failed' AND files IS NOT NULL", (time.time(),)).rowcount
                self.db.commit(); kv_set(self.db, "last_post", 0)
                self.tg.text(f"🔁 Retrying {n} failed item(s) now."); return
            if txt.strip() == "/check":
                self.tg.text("🔎 Checking now..."); kv_set(self.db, "last_poll", 0); return
            if self.editing and txt:
                mid, self.editing = self.editing, None
                self.db.execute("UPDATE items SET caption=? WHERE media_id=?", (txt, mid)); self.db.commit()
                self.tg.text("Caption updated. Approve it?", reply_markup=self.tg.kb(mid))

    # ---- posting
    def maybe_post(self):
        gap = self.cfg["min_minutes_between_posts"] * 60
        last = float(kv_get(self.db, "last_post", 0))
        if time.time() - last < gap:
            return
        auto = self.cfg.get("auto_post_after_minutes", 0)
        if auto:
            self.db.execute("UPDATE items SET status='approved', approved_at=? WHERE status='pending' AND created<?",
                            (time.time(), time.time() - auto * 60)); self.db.commit()
        row = self.db.execute("SELECT * FROM items WHERE status='approved' ORDER BY approved_at LIMIT 1").fetchone()
        if not row:
            return
        try:
            files = json.loads(row["files"])
            if any(f.get("path") and not os.path.exists(f["path"]) for f in files) and row["raw"]:
                raw = json.loads(row["raw"])
                log.info("rebuilding media for %s", row["media_id"])
                if row["media_type"] == "STORY":
                    files = self.build_story_files(raw)
                else:
                    files = self.build_post_files(raw)
                self.db.execute("UPDATE items SET files=? WHERE media_id=?", (json.dumps(files), row["media_id"]))
            cap = row["caption"] or ""
            if row["media_type"] != "STORY" and self.cfg.get("add_footer_to_edited", True):
                cap = finalize_caption(cap, self.cfg)
            pid = self.ig.publish_item(row["media_type"], files, cap)
            link = self.ig.get(pid, fields="permalink").get("permalink", "")
            self.db.execute("UPDATE items SET status='posted', posted_at=?, posted_id=? WHERE media_id=?",
                            (time.time(), pid, row["media_id"]))
            kv_set(self.db, "last_post", time.time())
            self.tg.text(f"📤 Posted: {link}")
            for f in files:
                for k in ("path", "cover"):
                    if f.get(k) and os.path.exists(f[k]):
                        os.remove(f[k])
        except Exception as e:
            log.exception("publish failed")
            self.db.execute("UPDATE items SET status='failed', error=? WHERE media_id=?", (str(e), row["media_id"]))
            kv_set(self.db, "last_post", time.time() - gap + 300)   # retry others after 5 min
            self.tg.text(f"⚠️ Posting failed for {row['permalink']}\n{e}\n\nSend /retry to try again.")
        self.db.commit()

    def maybe_refresh(self):
        last = float(kv_get(self.db, "token_refreshed", 0))
        secret = self.cfg.get("fb_app_secret", "")
        if time.time() - last > 7 * 86400 and secret and not secret.startswith(("PUT_", "NOT_NEEDED")):
            try:
                self.ig.refresh_token(); kv_set(self.db, "token_refreshed", time.time())
            except Exception as e:
                log.error("token refresh failed: %s", e)
                self.tg.text(f"⚠️ Instagram token refresh failed - posting may stop soon.\n{e}")
                kv_set(self.db, "token_refreshed", time.time() - 6 * 86400)  # try again tomorrow

    def loop(self, once=False, run_minutes=0):
        log.info("bot started; watching %s", ", ".join(self.cfg["source_accounts"]))
        self.tg.offset = int(kv_get(self.db, "tg_offset", 0))
        stop_at = START_TIME + run_minutes * 60 if run_minutes else None
        setup_mode = self.tg.chat.startswith("PUT_")
        if setup_mode:
            log.warning("telegram_chat_id not set - send /id to your bot in Telegram to get it")
        while True:
            if not setup_mode and time.time() - float(kv_get(self.db, "last_poll", 0)) > self.cfg["poll_minutes"] * 60:
                kv_set(self.db, "last_poll", time.time())
                self.maybe_refresh()
                self.check_sources()
                self.maybe_pick_best()
            if (not setup_mode and self.stories and
                    time.time() - float(kv_get(self.db, "last_story_poll", 0)) > self.cfg.get("story_poll_minutes", 45) * 60):
                kv_set(self.db, "last_story_poll", time.time())
                self.check_stories()
            for u in self.tg.updates(wait=1 if once else 20):
                try:
                    self.handle(u)
                except Exception:
                    log.exception("update handling failed")
            kv_set(self.db, "tg_offset", self.tg.offset)
            if not setup_mode:
                self.maybe_post()
            if once or (stop_at and time.time() > stop_at):
                break


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(BASE, "config.json"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--run-minutes", type=float, default=0, help="stop after N minutes (for GitHub Actions)")
    ap.add_argument("--best-now", type=int, default=0, help="send N top-performing reels right away")
    ap.add_argument("--test-caption")
    ap.add_argument("--test-outro", nargs=2, metavar=("IN", "OUT"))
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if a.test_caption is not None:
        print(rewrite_caption(a.test_caption.replace("\\n", "\n"), load_config(a.config))); return
    if a.test_outro:
        cfg = load_config(a.config)
        print(add_outro(a.test_outro[0], cfg["outro_path"], a.test_outro[1], cfg["video_encoder"])); return
    bot = Bot(a.config)
    if a.best_now:
        if kv_get(bot.db, "initialised") is None:
            bot.check_sources()          # mark existing posts as seen first
        bot.pick_best(count=a.best_now, manual=True)
    bot.loop(once=a.once, run_minutes=a.run_minutes)


if __name__ == "__main__":
    main()
