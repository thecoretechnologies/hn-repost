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
from datetime import datetime, timedelta, timezone
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
    if os.environ.get("HN_IG_COOKIES_B64"):
        import base64
        p = os.path.join(cfg["data_dir"], "ig_cookies.txt")
        with open(p, "wb") as f:
            f.write(base64.b64decode(os.environ["HN_IG_COOKIES_B64"]))
        cfg["ig_cookies_file"] = p
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

    # ---- protection against Meta rate limits / blocks
    kv_get = kv_set = None          # set by Bot so limits survive between runs
    _last_call = 0.0
    RATE_CODES = {4, 17, 32, 613, 80001, 80002}                 # "too many calls"
    BLOCK_CODES = {368}                                         # temporarily blocked for policy/spam
    BLOCK_SUBCODES = {5258005, 2207051, 2207050, 2207053}       # API blocked / action blocked / restricted

    def _guard(self):
        g = self.cfg.get("api_safety", {})
        if self.kv_get:
            until = float(self.kv_get("api_cooldown_until") or 0)
            if time.time() < until:
                raise RuntimeError("Protective pause: not calling Instagram until "
                                   + datetime.fromtimestamp(until, IST).strftime("%d %b %I:%M %p")
                                   + f" ({self.kv_get('api_cooldown_reason') or 'Meta warning'})")
            day = ist_now().strftime("%Y-%m-%d")
            key = f"api_calls:{day}"
            n = int(self.kv_get(key) or 0) + 1
            if n > g.get("daily_call_cap", 200):
                raise RuntimeError(f"Daily safety cap of {g.get('daily_call_cap', 200)} Instagram API calls reached - "
                                   "resuming tomorrow")
            self.kv_set(key, n)
        wait = g.get("min_seconds_between_calls", 3) - (time.time() - IG._last_call)
        if wait > 0:
            time.sleep(wait)
        IG._last_call = time.time()

    def _cooldown(self, hours, reason):
        if not self.kv_set:
            return
        until = time.time() + hours * 3600
        if until > float(self.kv_get("api_cooldown_until") or 0):
            self.kv_set("api_cooldown_until", until)
            self.kv_set("api_cooldown_reason", reason)
            self.kv_set("api_cooldown_notify", 1)

    def _usage(self, r):
        """Meta reports how close the app is to its limits in response headers - slow down early."""
        pct = 0
        for h in ("x-app-usage", "x-business-use-case-usage", "x-page-usage"):
            v = r.headers.get(h)
            if not v:
                continue
            try:
                j = json.loads(v)
                vals = [j] if "call_count" in j else [x for lst in j.values() for x in lst]
                for x in vals:
                    pct = max(pct, *(int(x.get(k, 0) or 0) for k in ("call_count", "total_cputime", "total_time")))
            except Exception:
                pass
        if pct >= 90:
            self._cooldown(6, f"Meta usage at {pct}% of the limit")
        elif pct >= 75:
            self._cooldown(1, f"Meta usage at {pct}% of the limit")

    def _check(self, r):
        self._usage(r)
        try:
            j = r.json()
        except Exception:
            r.raise_for_status(); raise
        if "error" in j:
            e = j["error"]
            code, sub = e.get("code"), e.get("error_subcode")
            if code in self.RATE_CODES:
                self._cooldown(self.cfg.get("api_safety", {}).get("rate_limit_pause_hours", 2), "Meta rate limit")
            elif code in self.BLOCK_CODES or sub in self.BLOCK_SUBCODES:
                self._cooldown(self.cfg.get("api_safety", {}).get("block_pause_hours", 24),
                               f"Meta blocked an action (code {code}/{sub})")
            detail = " - ".join(x for x in (e.get("error_user_title"), e.get("error_user_msg")) if x)
            raise RuntimeError(f"Graph API: {e.get('message')} (code {e.get('code')}/{e.get('error_subcode')})"
                               + (f": {detail}" if detail else ""))
        return j

    def get(self, path, **params):
        self._guard()
        params["access_token"] = self.tok
        return self._check(requests.get(f"{self.g}/{path}", params=params, timeout=60))

    def post(self, path, **params):
        self._guard()
        params["access_token"] = self.tok
        return self._check(requests.post(f"{self.g}/{path}", data=params, timeout=120))

    def recent_media(self, username, limit=8, stats=False):
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

    def find_by_code(self, username, code, max_items=100):
        """Look through an account's posts (business/creator accounts) for the post with this shortcode."""
        after, seen = None, 0
        base = ("id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp,"
                "like_count,comments_count,children{media_type,media_url}")
        while seen < max_items:
            page = f"media.limit(50){'.after(' + after + ')' if after else ''}{{{base}}}"
            j = self.get(self.cfg["ig_user_id"], fields=f"business_discovery.username({username}){{{page}}}")
            media = j.get("business_discovery", {}).get("media", {})
            for m in media.get("data", []):
                if f"/{code}" in (m.get("permalink") or ""):
                    return m
            seen += len(media.get("data", []))
            after = media.get("paging", {}).get("cursors", {}).get("after")
            if not after or not media.get("data"):
                return None
        return None

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
        fields = "status_code,status,video_status"
        while time.time() - t0 < max_wait:
            try:
                last = self.get(cid, fields=fields)
            except RuntimeError as e:
                if "video_status" in str(e) and "video_status" in fields:
                    fields = "status_code,status"        # not available for URL-fetched videos
                    continue
                raise
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
        if carousel_item:
            params["is_carousel_item"] = "true"
        return self.post(f"{self.cfg['ig_user_id']}/media", **params)

    def _upload_video(self, path, media_type, caption=None, carousel_item=False, extra=None):
        """1) host the file publicly and let Instagram fetch it (reliable for any length)
           2) fallback: direct (resumable) upload - Instagram rejects this for longer videos"""
        extra = dict(extra or {})
        errors = []
        url = public_host(path)
        if url:
            for attempt in (1, 2, 3):
                try:
                    cid = self._container(media_type, caption, carousel_item, video_url=url, **extra)["id"]
                    self._wait(cid)
                    return cid
                except Exception as e:
                    errors.append(f"hosted try {attempt}: {e}")
                    log.warning("hosted upload failed: %s", e)
                    if attempt == 2:
                        extra.pop("cover_url", None)      # last try without the custom cover
                    time.sleep(30)
        try:
            j = self._container(media_type, caption, carousel_item, upload_type="resumable", **extra)
            cid = j["id"]
            uri = j.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{self.v}/{cid}"
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                r = requests.post(uri, headers={"Authorization": f"OAuth {self.tok}", "offset": "0",
                                                "file_size": str(size)}, data=f, timeout=900)
            log.info("direct upload %s (%d bytes) -> HTTP %s %s", cid, size, r.status_code, r.text[:300])
            if r.status_code != 200:
                raise RuntimeError(f"direct upload HTTP {r.status_code}: {r.text[:200]}")
            self._wait(cid)
            return cid
        except Exception as e:
            errors.append(f"direct: {e}")
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
        try:
            return self.publish(cid)
        finally:
            cleanup_hosted()

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
IST = timezone(timedelta(hours=5, minutes=30))


def ist_now():
    return datetime.now(IST)


def is_active_hours(cfg):
    a, b = cfg.get("active_hours_ist", [6, 22])
    return a <= ist_now().hour < b


def last_check_slot(cfg):
    """Timestamp of the most recent scheduled check time (IST) that has already passed, or None."""
    now = ist_now()
    slots = []
    for t in cfg.get("check_times_ist", ["06:00", "09:00", "12:00", "15:00", "18:00", "21:00"]):
        h, m = map(int, t.split(":"))
        slots.append(now.replace(hour=h, minute=m, second=0, microsecond=0))
    past = [x for x in slots if x <= now]
    return max(past).timestamp() if past else None


def fail_hint(err):
    e = err.lower()
    if "access blocked" in e or "5258005" in e:
        return ("Meta has blocked this app's API access, so Instagram refused the upload. It won't work until Meta lifts "
                "the block - use 📥 Send files to post it yourself.")
    if "processingfailed" in e or "timed out" in e or "processing" in e:
        return "Instagram couldn't process the video. A retry usually works."
    if "limit" in e or "rate" in e or "too many" in e:
        return "Instagram's posting limit was hit. Retry after a few hours."
    if "oauth" in e or "token" in e or "(code 190" in e:
        return "The Instagram access token was rejected - send this report to Claude."
    if "yt-dlp" in e or "download" in e or "404" in e or "not found" in e:
        return "The original reel couldn't be downloaded - it may have been deleted or made private."
    if "telegram" in e or "sendDocument" in e:
        return "Telegram didn't accept the file. A retry usually works."
    return "Unexpected error. Retry once; if it keeps failing, send this report to Claude."
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
    HOSTED.append(name)
    url = f"https://github.com/{repo}/releases/download/media/{name}"
    for _ in range(20):                       # wait until the file is actually downloadable
        try:
            if requests.head(url, allow_redirects=True, timeout=30).status_code == 200:
                break
        except Exception:
            pass
        time.sleep(3)
    return url


HOSTED = []


def cleanup_hosted():
    """Delete the temporary files from the GitHub release once Instagram has them."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    while HOSTED and repo:
        name = HOSTED.pop()
        subprocess.run(["gh", "release", "delete-asset", "media", name, "-R", repo, "-y"], capture_output=True)


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

class YtdlpStoryWatcher:
    """Reads stories with yt-dlp using the spare account's browser cookies (cookies.txt)."""
    def __init__(self, cfg):
        self.cookies = cfg["ig_cookies_file"]
        self.ytdlp = shutil.which("yt-dlp") or "yt-dlp"

    def _json(self, url):
        cmd = [self.ytdlp, "-J", "--no-warnings", "--cookies", self.cookies, url]
        if PROXY:
            cmd[1:1] = ["--proxy", PROXY]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[-300:] or "yt-dlp failed")
        return json.loads(r.stdout)

    def stories(self, username):
        j = self._json(f"https://www.instagram.com/stories/{username}/")
        out = []
        for e in j.get("entries") or [j]:
            vids = [f for f in (e.get("formats") or []) if f.get("vcodec") not in (None, "none") and f.get("url")]
            if vids:
                best = max(vids, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
                url, is_video = best["url"], True
            else:
                thumbs = [t for t in (e.get("thumbnails") or []) if t.get("url")]
                if not thumbs:
                    continue
                best = max(thumbs, key=lambda t: (t.get("width") or 0))
                url, is_video = best["url"], False
            ts = e.get("timestamp")
            out.append({"id": f"story_{e.get('id')}", "is_video": is_video, "url": url,
                        "timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else "",
                        "permalink": f"https://www.instagram.com/stories/{username}/{e.get('id')}/"})
        return out

    def reel_url(self, permalink):
        j = self._json(permalink)
        vids = [f for f in (j.get("formats") or []) if f.get("vcodec") not in (None, "none") and f.get("url")]
        if not vids:
            raise RuntimeError("no video found")
        return max(vids, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))["url"]


class OfficialStoryWatcher:
    """Reads stories through the official Graph API for Instagram accounts that head office has shared
    with our business (partner access). config: "official_story_accounts": {"username": "<ig user id>"}"""
    def __init__(self, ig, accounts):
        self.ig, self.accounts = ig, accounts

    def stories(self, username):
        ig_id = self.accounts[username]
        j = self.ig.get(f"{ig_id}/stories", fields="id,media_type,media_url,thumbnail_url,timestamp,permalink")
        out = []
        for m in j.get("data", []):
            if not m.get("media_url"):
                continue
            out.append({"id": f"story_{m['id']}", "is_video": m.get("media_type") == "VIDEO",
                        "url": m["media_url"], "timestamp": m.get("timestamp", ""),
                        "permalink": m.get("permalink") or f"https://www.instagram.com/stories/{username}/"})
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

    def document(self, path, caption=""):
        with open(path, "rb") as f:
            return self.call("sendDocument", files={"document": f}, http_timeout=600, chat_id=self.chat,
                             caption=caption[:1000])

    def text(self, msg, **kw):
        return self.call("sendMessage", chat_id=self.chat, text=msg[:4000], **kw)

    manual = False

    @classmethod
    def kb(cls, mid):
        if cls.manual:
            return json.dumps({"inline_keyboard": [
                [{"text": "📥 Send files to post", "callback_data": f"now:{mid}"}],
                [{"text": "✏️ Edit caption", "callback_data": f"ed:{mid}"},
                 {"text": "❌ Reject", "callback_data": f"no:{mid}"}]]})
        return json.dumps({"inline_keyboard": [
            [{"text": "⚡ Post now", "callback_data": f"now:{mid}"},
             {"text": "📥 Send files", "callback_data": f"mf:{mid}"}],
            [{"text": "✏️ Edit caption", "callback_data": f"ed:{mid}"},
             {"text": "❌ Reject", "callback_data": f"no:{mid}"}]]})

    @staticmethod
    def kb_auto(mid):
        return json.dumps({"inline_keyboard": [[{"text": "⏸ Hold - don't auto-post", "callback_data": f"hold:{mid}"}]]})

    @staticmethod
    def kb_failed(mid):
        return json.dumps({"inline_keyboard": [
            [{"text": "🔁 Retry this reel", "callback_data": f"rt:{mid}"},
             {"text": "📥 Send files instead", "callback_data": f"mf:{mid}"}],
            [{"text": "❌ Skip it", "callback_data": f"rm:{mid}"}]]})

    def send_review(self, row, files, auto=False):
        cap = row["caption"] or ("(stories have no caption)" if row["media_type"] == "STORY" else "(no caption)")
        head = f"🆕 {row['media_type']} from @{row['source']}\n{row['permalink']}\n\n📝 New caption:\n"
        if auto:
            head = "🤖 Posting this automatically now\n" + head
        first = files[0]
        if first.get("note"):
            head = first["note"] + "\n" + head
        body = (head + cap)[:1000]
        kb = self.kb_auto(row["media_id"]) if auto else self.kb(row["media_id"])
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
        self.ig.kv_get = lambda k: kv_get(self.db, k)
        self.ig.kv_set = lambda k, v: kv_set(self.db, k, v)
        self.tg = TG(self.cfg)
        self.editing = None   # media_id waiting for a new caption
        if self.cfg.get("manual_mode"):
            TG.manual = True
            if kv_get(self.db, "manual_init") != "1":
                kv_set(self.db, "manual_init", "1"); kv_set(self.db, "paused", "0")
                self.tg.text("📥 Manual posting mode is on: tap “Send files to post” and the bot sends you the "
                             "finished video, cover and caption to post yourself.")
        global PROXY, STORY_WATCHER
        PROXY = self.cfg.get("proxy") or None
        self.stories = None
        if self.cfg.get("official_story_accounts"):
            self.stories = OfficialStoryWatcher(self.ig, self.cfg["official_story_accounts"])
            self.cfg["story_source_accounts"] = list(self.cfg["official_story_accounts"])
            self.cfg["stories_enabled"] = True
        elif self.cfg.get("ig_cookies_file"):
            self.cfg["stories_enabled"] = True
            try:
                from ig_web import IGWeb
                STORY_WATCHER = IGWeb(self.cfg["ig_cookies_file"], self.cfg.get("proxy") or None)
                self.stories = STORY_WATCHER
            except Exception as e:
                log.error("cookie story watcher failed: %s", e)
        elif os.environ.get("HN_IG_SESSION_B64") and os.environ.get("HN_SPARE_IG_USERNAME"):
            self.cfg["stories_enabled"] = True
        if self.stories is None and (self.cfg.get("stories_enabled") and not self.cfg.get("ig_cookies_file")
                                      and self.cfg.get("spare_ig_username", "").find("PUT_") != 0):
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
                today = ist_now().strftime("%Y-%m-%d")
                if kv_get(self.db, "check_alert") != today:
                    kv_set(self.db, "check_alert", today)
                    self.tg.text(f"⚠️ Couldn't check @{user} for new reels:\n{str(e)[:300]}\n💡 {fail_hint(str(e))}\n"
                                 "(I'll keep trying at the scheduled times; this notice is sent once a day.)")
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
                    try:   # pinned or resurfaced old reels: remember them silently, don't bother you
                        age_d = (datetime.now(timezone.utc) - datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z")).days
                        if age_d > self.cfg.get("ignore_older_than_days", 3):
                            self.db.execute("UPDATE items SET status='old_skipped' WHERE media_id=?", (m["id"],))
                            self.db.commit(); continue
                    except (KeyError, ValueError):
                        pass
                    found += 1
                    auto = self.cfg.get("auto_post_new", False)
                    try:   # only auto-post fresh reels; older backlog comes for review instead
                        age_h = (datetime.now(timezone.utc) - datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z")).total_seconds() / 3600
                        if age_h > self.cfg.get("auto_post_max_age_hours", 6):
                            auto = False
                    except Exception:
                        pass
                    self.prepare(m, user, auto=auto)
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
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if kv_get(self.db, "story_alert") != today:
                    kv_set(self.db, "story_alert", today)
                    self.tg.text(f"⚠️ Couldn't read stories (the spare account may be logged out):\n{str(e)[:300]}")
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

    def watermark_cfg(self):
        w = self.cfg.get("watermark", {})
        if not w.get("enabled", True):
            return None
        badge = w.get("badge", "assets/badge.png")
        return dict(path=badge if os.path.isabs(badge) else os.path.join(BASE, badge), width=w.get("width", 220),
                    x=w.get("x", 40), y=w.get("y", 150), opacity=w.get("opacity", 0.25))

    def build_story_files(self, it):
        mid = it["id"]; mdir = os.path.join(self.cfg["data_dir"], "media")
        wm = self.watermark_cfg()
        if not it["is_video"]:
            img = download(it["url"], os.path.join(mdir, f"{mid}.jpg"), proxy=True)
            raw = image_to_story_video(img, os.path.join(mdir, f"{mid}_raw.mp4"))
            os.remove(img)
        else:
            raw = download(it["url"], os.path.join(mdir, f"{mid}_raw.mp4"), proxy=True)
        clean, note = self.clean_onscreen(raw, mid)
        if clean != raw:
            os.remove(raw); raw = clean
        dur, _ = probe(raw)
        final = os.path.join(mdir, f"{mid}.mp4")
        if self.cfg.get("outro_on_stories", True) and dur + 3 <= 60:
            add_outro(raw, self.cfg["outro_path"], final, self.cfg["video_encoder"], wm)
        else:   # too long for the outro: watermark only
            vf = ("scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,"
                  "fps=30,setsar=1")
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", raw]
            if wm and os.path.exists(wm["path"]):
                cmd += ["-i", wm["path"], "-filter_complex",
                        f"[0:v]{vf}[b];[1:v]scale={wm['width']}:-1,format=rgba,colorchannelmixer=aa={wm['opacity']}[l];"
                        f"[b][l]overlay={wm['x']}:{wm['y']},format=yuv420p[v]", "-map", "[v]", "-map", "0:a?"]
            else:
                cmd += ["-vf", vf + ",format=yuv420p"]
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-movflags", "+faststart", final]
            run(cmd, timeout=900)
        os.remove(raw)
        return [{"type": "VIDEO", "path": final, **({"note": note} if note else {})}]

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

    def fresh_raw(self, row):
        """Re-read the source post so download links are fresh (Instagram links expire)."""
        raw = json.loads(row["raw"]) if row["raw"] else {}
        try:
            for m in self.ig.recent_media(row["source"], limit=60):
                if m["id"] == row["media_id"]:
                    raw.update(m); break
        except Exception as e:
            log.warning("refresh failed for %s: %s", row["media_id"], e)
        return raw

    def report_failure(self, row, stage, err):
        mid = row["media_id"]
        n = int(kv_get(self.db, f"attempts:{mid}", 0)) + 1
        kv_set(self.db, f"attempts:{mid}", n)
        e = str(err)
        kind = {"REEL": "Reel", "STORY": "Story", "IMAGE": "Post", "CAROUSEL": "Carousel"}.get(row["media_type"], "Post")
        msg = (f"⚠️ {kind} upload FAILED\n"
               f"👤 From: @{row['source']}\n"
               f"🔗 {row['permalink']}\n"
               f"🕒 {ist_now().strftime('%d %b, %I:%M %p')}\n"
               f"🧩 Failed while: {stage}\n"
               f"🔁 Attempt: {n}\n"
               f"❗ Error: {e[:700]}\n\n"
               f"💡 {fail_hint(e)}")
        try:
            self.tg.text(msg, reply_markup=self.tg.kb_failed(mid))
        except Exception:
            self.tg.text(msg + "\n\nSend /failed to retry.")

    def fit_for_telegram(self, path):
        """Telegram bots can send files up to 50 MB; re-encode bigger videos to fit."""
        if os.path.getsize(path) < 48_000_000:
            return path
        dur, _ = probe(path)
        kbps = int(44_000_000 * 8 / max(dur, 1) / 1000) - 128
        small = path.rsplit(".", 1)[0] + "_tg.mp4"
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-c:v", "libx264", "-preset", "veryfast",
             "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{2 * kbps}k", "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", small], timeout=1800)
        return small

    def send_manual(self, row):
        """Manual posting: send the finished video, cover, caption and original link as separate messages."""
        log.info("preparing manual package for %s", row["media_id"])
        files = json.loads(row["files"]) if row["files"] else []
        if not files or any(f.get("path") and not os.path.exists(f["path"]) for f in files):
            if not row["raw"]:
                raise RuntimeError("no saved details to rebuild this item - paste the reel link to the bot again")
            raw = self.fresh_raw(row) if row["media_type"] != "STORY" else json.loads(row["raw"] or "{}")
            files = self.build_story_files(raw) if row["media_type"] == "STORY" else self.build_post_files(raw)
        kind = {"REEL": "Reel", "STORY": "Story", "IMAGE": "Post", "CAROUSEL": "Carousel"}.get(row["media_type"], "Post")
        self.tg.text(f"📦 Ready to post manually - {kind} from @{row['source']}")
        for i, f in enumerate(files, 1):
            if f.get("path"):
                self.tg.document(self.fit_for_telegram(f["path"]), f"🎬 {kind} video{f' {i}' if len(files) > 1 else ''}")
            elif f.get("url"):
                p = download(f["url"], os.path.join(self.cfg["data_dir"], "media", f"{row['media_id']}_{i}.jpg"))
                self.tg.document(p, f"🖼 Photo {i}")
            if f.get("cover") and os.path.exists(f["cover"]):
                self.tg.document(f["cover"], "🖼 Cover image (Edit cover → Add from camera roll)")
        if row["media_type"] != "STORY":
            self.tg.text("👇 Caption - long-press to copy:")
            self.tg.text(finalize_caption(row["caption"] or "", self.cfg))
        self.tg.text(f"🔗 Original: {row['permalink']}")
        for f in files:
            for k in ("path", "cover"):
                if f.get(k) and os.path.exists(f[k]):
                    os.remove(f[k])
        self.db.execute("UPDATE items SET status='manual_sent', posted_at=? WHERE media_id=?",
                        (time.time(), row["media_id"])); self.db.commit()

    def show_queue(self):
        gap = self.cfg["min_minutes_between_posts"] * 60
        nxt = max(time.time(), float(kv_get(self.db, "last_post", 0)) + gap)
        q = self.db.execute("SELECT * FROM items WHERE status='approved' ORDER BY approved_at").fetchall()
        pend = self.db.execute("SELECT COUNT(*) FROM items WHERE status='pending'").fetchone()[0]
        failed = self.db.execute("SELECT COUNT(*) FROM items WHERE status='failed'").fetchone()[0]
        if not q:
            self.tg.text(f"📭 Queue is empty.\n⏳ Waiting for your approval: {pend}\n⚠️ Failed: {failed}"
                         + ("  (send /retry)" if failed else "")); return
        self.tg.text(f"🗓 {len(q)} approved item(s) in the queue (one every {gap // 60} min):")
        for i, r in enumerate(q):
            eta = datetime.fromtimestamp(nxt + i * gap, timezone.utc) + timedelta(hours=5, minutes=30)
            first = (r["caption"] or "").split("\n")[0][:80] or "(no caption)"
            self.tg.text(f"{i + 1}. {r['media_type'].title()} from @{r['source']} · ~{eta:%I:%M %p} IST\n{first}\n{r['permalink']}",
                         reply_markup=json.dumps({"inline_keyboard": [[
                             {"text": "⚡ Post now", "callback_data": f"now:{r['media_id']}"},
                             {"text": "🗑 Remove", "callback_data": f"rm:{r['media_id']}"}]]}))
        self.tg.text(f"⏳ Waiting for your approval: {pend}" + (f"\n⚠️ Failed: {failed} (send /retry)" if failed else ""))

    def resend_pending(self):
        rows = self.db.execute("SELECT * FROM items WHERE status='pending' ORDER BY created").fetchall()
        if not rows:
            self.tg.text("Nothing is waiting for approval."); return
        self.tg.text(f"📬 Re-sending {len(rows)} post(s) waiting for your approval:")
        for row in rows:
            try:
                raw = self.fresh_raw(row)
                files = (self.build_story_files(raw) if row["media_type"] == "STORY" else self.build_post_files(raw))
                cap = row["caption"] if row["media_type"] == "STORY" else finalize_caption(row["caption"], self.cfg)
                self.db.execute("UPDATE items SET files=?, raw=?, caption=? WHERE media_id=?",
                                (json.dumps(files), json.dumps(raw), cap, row["media_id"])); self.db.commit()
                msg_id = self.tg.send_review(self.row(row["media_id"]), files)
                self.db.execute("UPDATE items SET tg_message_id=? WHERE media_id=?", (msg_id, row["media_id"]))
                self.db.commit()
            except Exception as e:
                log.exception("resend failed")
                self.tg.text(f"⚠️ Couldn't re-send {row['permalink']}: {e}")

    LINK_RE = re.compile(r"instagram\.com/(?:([\w.]+)/)?(reel|reels|p|tv)/([\w-]+)")

    def handle_link(self, text):
        mm = self.LINK_RE.search(text)
        code = mm.group(3)
        user = mm.group(1) if mm.group(1) not in (None, "stories") else None
        um = re.search(r"@([\w.]+)", text.replace(mm.group(0), ""))
        if um:
            user = um.group(1)
        self.tg.text(f"🔎 Fetching {code}...")
        if not user:
            user = self.resolve_owner(f"https://www.instagram.com/{mm.group(2)}/{code}/", code)
            if user:
                log.info("link %s belongs to @%s", code, user)
        m, src = None, None
        for u in ([user] if user else []) + [a for a in self.cfg["source_accounts"] if a != user]:
            try:
                m = self.ig.find_by_code(u, code, 300 if u == user else 150)
            except Exception as e:
                log.warning("lookup in @%s failed: %s", u, e); m = None
            if m:
                src = u; break
        if not m:
            m, src = self.link_via_ytdlp(code)
            if m and user:
                src = user          # show the account name instead of a numeric id
        if not m:
            self.tg.text("❌ Couldn't fetch that post" + (f" from @{user}" if user else "") + ". It may be from a "
                         "private/personal account, or Instagram's API is blocked right now - try /retry later.")
            return
        row = self.db.execute("SELECT status FROM items WHERE media_id=?", (m["id"],)).fetchone()
        if row and row["status"] == "posted":
            self.tg.text("ℹ️ This one is already posted on your page."); return
        if not row:
            self.db.execute("INSERT INTO items(media_id,fingerprint,source,media_type,permalink,original_caption,"
                            "status,created) VALUES(?,?,?,?,?,?,?,?)",
                            (m["id"], self.fingerprint(m), src, self.kind_of(m), m.get("permalink"),
                             m.get("caption"), "new", time.time()))
        else:
            self.db.execute("UPDATE items SET status='new' WHERE media_id=?", (m["id"],))
        self.db.commit()
        if self.already_on_store(m):
            self.tg.text("⚠️ Looks like this is already on your page - sending it anyway, reject it if it's a repeat.")
        self.prepare(m, src)

    def resolve_owner(self, url, code):
        """Work out which account posted a link: Meta's oEmbed endpoint first, then the public post page."""
        try:
            if not self.cfg.get("use_oembed", False):   # needs Meta app review; skip to save API calls
                raise RuntimeError("oEmbed disabled")
            j = self.ig.get("instagram_oembed", url=url, omitscript="true")
            name = j.get("author_name") or ""
            if re.fullmatch(r"[\w.]+", name):
                return name
            blob = f'{j.get("author_url", "")} {j.get("html", "")}'
            for m in re.finditer(r"instagram\.com/([\w.]+)/?[\"?\s]|\(@([\w.]+)\)", blob):
                u = m.group(1) or m.group(2)
                if u not in ("p", "reel", "reels", "tv", "explore"):
                    return u
        except Exception as e:
            log.warning("oembed lookup failed: %s", e)
        try:
            r = requests.get(f"https://www.instagram.com/reel/{code}/", timeout=30, headers={
                "User-Agent": "Mozilla/5.0 (compatible; facebookexternalhit/1.1)", "Accept-Language": "en"})
            html = r.text
            for pat in (r'"username":"([\w.]+)"', r'property="og:title" content="[^"]*?\(@([\w.]+)\)',
                        r'content="[^"]*? - ([\w.]+) on [^"]*?"', r'@([\w.]+) on Instagram'):
                m = re.search(pat, html)
                if m:
                    return m.group(1)
        except Exception as e:
            log.warning("public page lookup failed: %s", e)
        return None

    def link_via_ytdlp(self, code):
        """Fallback for accounts the API can't read: get the post with yt-dlp."""
        url = f"https://www.instagram.com/reel/{code}/"
        try:
            ytdlp = shutil.which("yt-dlp") or "yt-dlp"
            cmd = [ytdlp, "-j", "--no-warnings", url]
            if PROXY:
                cmd[1:1] = ["--proxy", PROXY]
            info = json.loads(run(cmd, timeout=120).stdout)
            return ({"id": f"link_{code}", "caption": info.get("description") or "", "media_type": "VIDEO",
                     "permalink": url, "thumbnail_url": info.get("thumbnail"),
                     "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")},
                    info.get("uploader_id") or info.get("channel") or "link")
        except Exception as e:
            log.warning("yt-dlp lookup failed: %s", e)
            return None, None

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

    def clean_onscreen(self, raw, mid):
        """Cover other branches' phone numbers/addresses shown inside the video. Returns (video, note)."""
        oc = self.cfg.get("onscreen_replace", {})
        if not oc.get("enabled", True):
            return raw, None
        try:
            from onscreen import replace_contacts
        except ImportError as e:
            log.warning("on-screen replacement unavailable: %s", e); return raw, None
        ours = {re.sub(r"\D", "", p)[-10:] for p in self.cfg.get("store_phones", ["8527150060", "8527150040"])}
        dest = os.path.join(self.cfg["data_dir"], "media", f"{mid}_clean.mp4")
        return replace_contacts(raw, dest, oc, ours)

    def build_post_files(self, m):
        mid, kind = m["id"], self.kind_of(m)
        mdir = os.path.join(self.cfg["data_dir"], "media")
        files = []
        if kind == "REEL":
            raw = download_reel(m, os.path.join(mdir, f"{mid}_raw.mp4"))
            silent = not probe(raw)[1]
            if silent:   # Instagram strips licensed music from API copies - try the public reel instead
                log.warning("%s has no audio from the API; trying the public reel", mid)
                try:
                    alt = download_reel({k: v for k, v in m.items() if k != "media_url"},
                                        os.path.join(mdir, f"{mid}_alt.mp4"))
                    if os.path.exists(alt) and probe(alt)[1]:
                        os.replace(alt, raw); silent = False
                except Exception as e:
                    log.warning("public reel download failed: %s", e)
            clean, note = self.clean_onscreen(raw, mid)
            wm = self.watermark_cfg()
            final = add_outro(clean, self.cfg["outro_path"], os.path.join(mdir, f"{mid}.mp4"), self.cfg["video_encoder"], wm)
            f = {"type": "VIDEO", "path": final}
            if note:
                f["note"] = note
            if silent:
                f["silent"] = True
                f["note"] = ("🔇 NO MUSIC: Instagram removed this reel's licensed song from the copy the bot gets, so it "
                             "was NOT auto-posted. To post with music: tap 📥 Send files, then in Instagram open the "
                             "original reel → tap the song name → Use audio → pick the saved video."
                             + ("\n" + note if note else ""))
            if self.cfg.get("cover", {}).get("enabled", True):
                try:   # cover from the original thumbnail - or from the cleaned video if contacts were replaced
                    src_m = {k: v for k, v in m.items() if k != "thumbnail_url"} if clean != raw else m
                    f["cover"] = make_cover(self.cfg, src_m, clean, os.path.join(mdir, f"{mid}_cover.jpg"))
                except Exception as e:
                    log.warning("cover failed: %s", e)
            for p in {raw, clean}:
                if os.path.exists(p):
                    os.remove(p)
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

    def prepare(self, m, user, auto=False):
        mid, kind = m["id"], self.kind_of(m)
        mdir = os.path.join(self.cfg["data_dir"], "media")
        self.db.execute("UPDATE items SET raw=? WHERE media_id=?", (json.dumps(m), mid)); self.db.commit()
        try:
            files = self.build_post_files(m)
            caption = finalize_caption(rewrite_caption(m.get("caption"), self.cfg), self.cfg)
            self.db.execute("UPDATE items SET caption=?, files=?, raw=?, status=? WHERE media_id=?",
                            (caption, json.dumps(files), json.dumps(m), "approved" if auto else "pending", mid))
            if auto and any(f.get("silent") for f in files):
                auto = False
                self.db.execute("UPDATE items SET status='pending' WHERE media_id=?", (mid,))
            if auto:
                self.db.execute("UPDATE items SET approved_at=? WHERE media_id=?", (time.time(), mid))
            self.db.commit()
            row = self.row(mid)
            msg_id = self.tg.send_review(row, files, auto=auto)
            self.db.execute("UPDATE items SET tg_message_id=? WHERE media_id=?", (msg_id, mid)); self.db.commit()
        except Exception as e:
            log.exception("prepare failed for %s", mid)
            self.db.execute("UPDATE items SET status='failed', error=? WHERE media_id=?", (str(e), mid)); self.db.commit()
            self.report_failure(self.row(mid), "downloading / editing the video", e)

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
            log.info("button %s on %s (status %s)", act, mid, row["status"] if row else None)
            note = ""
            if act == "rt" and row and int(kv_get(self.db, f"attempts:{mid}", 0)) >= self.cfg.get("max_attempts_per_reel", 4):
                note = "Tried too many times - use 📥 Send files to post it yourself"
            elif act == "rt" and row and row["status"] not in ("posted",):
                self.db.execute("UPDATE items SET status='approved', approved_at=0, error=NULL WHERE media_id=?", (mid,))
                kv_set(self.db, "last_post", 0)
                note = "Retrying now - you'll get the result in a few minutes"
            elif act == "mf" and row and row["status"] not in ("posted",):
                self.db.execute("UPDATE items SET status='manual_req', error=NULL WHERE media_id=?", (mid,))
                note = "Preparing files - they'll arrive here in a few minutes"
            elif act == "hold" and row and row["status"] == "approved":
                self.db.execute("UPDATE items SET status='pending' WHERE media_id=?", (mid,))
                self.db.commit()
                try:
                    self.tg.call("answerCallbackQuery", callback_query_id=q["id"], text="Held - choose what to do")
                    self.tg.call("editMessageReplyMarkup", chat_id=self.tg.chat, message_id=q["message"]["message_id"],
                                 reply_markup=self.tg.kb(mid))
                except Exception:
                    pass
                return
            elif act == "rm" and row and row["status"] in ("approved", "pending", "failed"):
                self.db.execute("UPDATE items SET status='rejected' WHERE media_id=?", (mid,))
                note = "Removed from the queue"
            elif act == "now" and row and row["status"] == "pending" and '"silent": true' in (row["files"] or "") \
                    and kv_get(self.db, f"silent_ok:{mid}") != "1":
                kv_set(self.db, f"silent_ok:{mid}", "1")
                note = "⚠️ This reel has no music. Tap ⚡ Post now again to post it silent anyway."
            elif act == "now" and row and row["status"] in ("pending", "approved", "failed"):
                # jump the queue: front of the line and no waiting gap
                self.db.execute("UPDATE items SET status='approved', approved_at=0, error=NULL WHERE media_id=?", (mid,))
                kv_set(self.db, "last_post", 0)
                note = ("Preparing files - they'll arrive here in a few minutes" if self.cfg.get("manual_mode")
                        else "Posting now - you'll get the link in a few minutes")
            elif not row or row["status"] != "pending":
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
                if act in ("ok", "no", "now", "rm", "rt", "mf") and note != "Already handled" and not note.startswith("⚠️"):
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
                             "Paste any Instagram reel/post link to prepare it for posting\n/queue - what's approved and when it will post\n/pending - re-send everything waiting for approval\n/status - counts\n/check - check the main accounts now\n/failed - list failed reels with a Retry button for each\n/retry - retry all failed items\n\nNew reels are checked at 6am, 9am, 12pm, 3pm, 6pm and 9pm and posted automatically. The bot sleeps 10pm-6am."); return
            if txt.strip() == "/status":
                c = dict(self.db.execute("SELECT status, COUNT(*) n FROM items GROUP BY status").fetchall())
                self.tg.text("📊 " + ", ".join(f"{k}: {v}" for k, v in c.items())); return
            if txt.strip().startswith("/best"):
                parts = txt.split()
                n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
                self.tg.text("🔎 Looking for top-performing reels...")
                self.pick_best(count=n, manual=True); return
            if txt.strip() == "/pause":
                kv_set(self.db, "paused", "1"); self.tg.text("⏸ Bot paused - no checking or posting until /resume."); return
            if txt.strip() == "/resume":
                kv_set(self.db, "paused", "0"); self.tg.text("▶️ Bot resumed."); return
            if txt.strip() in ("/queue", "/q"):
                self.show_queue(); return
            if txt.strip() == "/accounts":
                try:
                    j = self.ig.get("me/accounts", fields="name,instagram_business_account{id,username}")
                    lines = [f"• {p['instagram_business_account']['username']} = {p['instagram_business_account']['id']}"
                             for p in j.get("data", []) if p.get("instagram_business_account")]
                    self.tg.text("Instagram accounts the bot can access:\n" + ("\n".join(lines) or "(none)"))
                except Exception as e:
                    self.tg.text(f"⚠️ {e}")
                return
            if txt.strip() == "/pending":
                self.resend_pending(); return
            if txt.strip() == "/failed":
                rows = self.db.execute("SELECT * FROM items WHERE status='failed' ORDER BY created DESC LIMIT 10").fetchall()
                if not rows:
                    self.tg.text("✅ Nothing has failed."); return
                for r in rows:
                    self.tg.text(f"⚠️ @{r['source']} {r['media_type']}\n{r['permalink']}\n❗ {(r['error'] or '')[:300]}",
                                 reply_markup=self.tg.kb_failed(r["media_id"]))
                return
            if txt.strip() == "/retry":
                n = self.db.execute("UPDATE items SET status='approved', approved_at=?, error=NULL "
                                    "WHERE status='failed' AND files IS NOT NULL", (time.time(),)).rowcount
                self.db.commit(); kv_set(self.db, "last_post", 0)
                self.tg.text(f"🔁 Retrying {n} failed item(s) now."); return
            if txt.strip() == "/check":
                self.tg.text("🔎 Checking now..."); kv_set(self.db, "last_poll", 0); return
            if not self.editing and self.LINK_RE.search(txt or ""):
                self.handle_link(txt); return
            if self.editing and txt:
                mid, self.editing = self.editing, None
                self.db.execute("UPDATE items SET caption=? WHERE media_id=?", (txt, mid)); self.db.commit()
                self.tg.text("Caption updated. Approve it?", reply_markup=self.tg.kb(mid))

    # ---- posting
    def maybe_post(self):
        for row in self.db.execute("SELECT * FROM items WHERE status='manual_req'").fetchall():
            try:
                self.send_manual(row)
            except Exception as e:
                log.exception("manual package failed")
                self.db.execute("UPDATE items SET status='failed', error=? WHERE media_id=?", (str(e), row["media_id"]))
                self.db.commit()
                self.report_failure(row, "preparing files to send you", e)
        if self.cfg.get("manual_mode"):
            for row in self.db.execute("SELECT * FROM items WHERE status='approved' ORDER BY approved_at").fetchall():
                try:
                    self.send_manual(row)
                except Exception as e:
                    log.exception("manual package failed")
                    self.db.execute("UPDATE items SET status='failed', error=? WHERE media_id=?", (str(e), row["media_id"]))
                    self.db.commit()
                    self.report_failure(row, "preparing files to send you", e)
            return
        gap = self.cfg["min_minutes_between_posts"] * 60
        last = float(kv_get(self.db, "last_post", 0))
        if time.time() - last < gap:
            return
        day_start = ist_now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        posted_today = self.db.execute("SELECT COUNT(*) FROM items WHERE status='posted' AND posted_at>=?",
                                       (day_start,)).fetchone()[0]
        if posted_today >= self.cfg.get("max_posts_per_day", 8):
            if kv_get(self.db, "cap_note") != ist_now().strftime("%Y-%m-%d"):
                kv_set(self.db, "cap_note", ist_now().strftime("%Y-%m-%d"))
                self.tg.text(f"🛡 Daily limit of {self.cfg.get('max_posts_per_day', 8)} posts reached - remaining "
                             "reels will post tomorrow (or tap 📥 Send files to post them yourself).")
            return
        auto = self.cfg.get("auto_post_after_minutes", 0)
        if auto:
            self.db.execute("UPDATE items SET status='approved', approved_at=? WHERE status='pending' AND created<?",
                            (time.time(), time.time() - auto * 60)); self.db.commit()
        row = self.db.execute("SELECT * FROM items WHERE status='approved' ORDER BY approved_at LIMIT 1").fetchone()
        if not row:
            return
        stage = "preparing the video"
        try:
            files = json.loads(row["files"]) if row["files"] else []
            if (not files or any(f.get("path") and not os.path.exists(f["path"]) for f in files)) and row["raw"]:
                stage = "downloading / editing the video"
                raw = self.fresh_raw(row) if row["media_type"] != "STORY" else json.loads(row["raw"])
                log.info("rebuilding media for %s", row["media_id"])
                if row["media_type"] == "STORY":
                    files = self.build_story_files(raw)
                else:
                    files = self.build_post_files(raw)
                self.db.execute("UPDATE items SET files=? WHERE media_id=?", (json.dumps(files), row["media_id"]))
            cap = row["caption"] or ""
            if row["media_type"] != "STORY" and self.cfg.get("add_footer_to_edited", True):
                cap = finalize_caption(cap, self.cfg)
            if not files:
                raise RuntimeError("no video files and no saved details to rebuild them")
            stage = "uploading to Instagram"
            pid = self.ig.publish_item(row["media_type"], files, cap)
            stage = "getting the new post's link (it may already be posted - check the store page)"
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
            kv_set(self.db, "last_post", time.time() - gap + 120)   # next item after 2 min
            self.db.commit()
            self.report_failure(row, stage, e)
        self.db.commit()

    def maybe_reauth_reminder(self):
        """Meta's data access for the login lasts ~90 days; remind a week before it runs out."""
        by = self.cfg.get("reauth_by")
        if not by:
            return
        days = (datetime.strptime(by, "%Y-%m-%d").replace(tzinfo=IST) - ist_now()).days
        today = ist_now().strftime("%Y-%m-%d")
        if days <= 7 and kv_get(self.db, "reauth_alert") != today:
            kv_set(self.db, "reauth_alert", today)
            self.tg.text(f"🔑 Instagram access needs a quick re-login within {max(days, 0)} day(s) (by {by}). "
                         "Ask Claude to \"renew the Meta login\" - it takes 2 minutes.")

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
        # one-off retries requested through config ("retry_codes": ["DfUm2Gty6AG", ...])
        done = set(json.loads(kv_get(self.db, "retry_done", "[]")))
        for code in self.cfg.get("retry_codes", []):
            if code in done:
                continue
            short = code.split("#")[0]           # "CODE#2" lets the same reel be retried again later
            n = self.db.execute("UPDATE items SET status='approved', approved_at=?, error=NULL "
                                "WHERE permalink LIKE ? AND status IN ('failed','pending','approved')",
                                (time.time(), f"%{short}%")).rowcount
            self.db.commit(); done.add(code)
            if n:
                kv_set(self.db, "last_post", 0)
                self.tg.text(f"🔁 Retrying https://www.instagram.com/reel/{short}/ now.")
        kv_set(self.db, "retry_done", json.dumps(sorted(done)))
        ctok = self.cfg.get("check_now_token")
        if ctok and kv_get(self.db, "check_done") != ctok:
            kv_set(self.db, "check_done", ctok); kv_set(self.db, "last_poll", 0)
            self.tg.text("🔎 Checking the main accounts for new posts now...")
        tok = self.cfg.get("resend_pending_token")
        if tok and kv_get(self.db, "resend_done") != tok:
            kv_set(self.db, "resend_done", tok)
            self.maybe_post()          # post anything approved first
            self.resend_pending()
        stop_at = START_TIME + run_minutes * 60 if run_minutes else None
        setup_mode = self.tg.chat.startswith("PUT_")
        if setup_mode:
            log.warning("telegram_chat_id not set - send /id to your bot in Telegram to get it")
        if run_minutes and not is_active_hours(self.cfg):
            log.info("night time (IST %s) - bot is off until %s:00", ist_now().strftime("%H:%M"),
                     self.cfg.get("active_hours_ist", [6, 22])[0])
            return
        while True:
            if kv_get(self.db, "api_cooldown_notify") == "1":
                kv_set(self.db, "api_cooldown_notify", 0)
                until = datetime.fromtimestamp(float(kv_get(self.db, "api_cooldown_until", 0)), IST)
                self.tg.text(f"🛡 Safety pause: {kv_get(self.db, 'api_cooldown_reason')}. To avoid a block, the bot "
                             f"won't call Instagram until {until.strftime('%d %b %I:%M %p')}. Telegram and pasted links "
                             "(review/send files) keep working; posting resumes automatically.")
            slot = last_check_slot(self.cfg)
            if slot:   # spread checks a few minutes after the hour so they don't look robotic
                slot += int(hashlib.md5(str(int(slot)).encode()).hexdigest(), 16) % (self.cfg.get("check_jitter_minutes", 15) * 60)
                if slot > time.time():
                    slot = None
            if (not setup_mode and not (self.cfg.get("paused") or kv_get(self.db, "paused") == "1")
                    and slot and float(kv_get(self.db, "last_poll", 0)) < slot):
                kv_set(self.db, "last_poll", time.time())
                self.maybe_refresh()
                self.maybe_reauth_reminder()
                self.check_sources()
                self.maybe_pick_best()
            paused = self.cfg.get("paused") or kv_get(self.db, "paused") == "1"
            if paused:
                for u in self.tg.updates(wait=1 if once else 20):
                    try:
                        self.handle(u)
                    except Exception:
                        log.exception("update handling failed")
                kv_set(self.db, "tg_offset", self.tg.offset)
                if once or (stop_at and time.time() > stop_at):
                    break
                continue
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
            if once or (stop_at and time.time() > stop_at) or (run_minutes and not is_active_hours(self.cfg)):
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
