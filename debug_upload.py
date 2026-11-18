import os, json, sqlite3, sys, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO)
import hn_repost as h
cfg = h.load_config("config.json")
cfg["access_token"] = os.environ["HN_ACCESS_TOKEN"]; cfg["telegram_chat_id"] = "PUT_"
db = sqlite3.connect("state.db"); db.row_factory = sqlite3.Row
row = db.execute("select * from items where permalink like '%Df3xNRVt-Q7%'").fetchone()
raw = json.loads(row["raw"]); cap = h.finalize_caption(row["caption"], cfg)
print("CAPTION", len(cap), repr(cap[:400]))
print("TAGS", len(h.HASHTAG_RE.findall(cap)))
ig = h.IG(cfg, "config.json")
fresh = h.requests.get(f"https://graph.facebook.com/v27.0/{raw['id']}", params={"fields": "media_url,thumbnail_url", "access_token": cfg["access_token"]}).json()
print("fresh keys", list(fresh)); raw.update({k: v for k, v in fresh.items() if k in ("media_url", "thumbnail_url")})
class B: pass
b = h.Bot.__new__(h.Bot); b.cfg = cfg
files = h.Bot.build_post_files(b, raw)
print("FILES", files)
url = h.public_host(files[0]["path"]); print("URL", url)
import requests, time
time.sleep(5)
r = requests.head(url, allow_redirects=True, timeout=60); print("HEAD", r.status_code, r.headers.get("content-type"), r.headers.get("content-length"))
for label, params in (("no caption", dict(media_type="REELS", video_url=url)),
                      ("caption", dict(media_type="REELS", video_url=url, caption=cap))):
    try:
        cid = ig.post(f"{cfg['ig_user_id']}/media", **params)["id"]; print(label, "container OK", cid)
    except Exception as e:
        print(label, "FAILED", e)
h.cleanup_hosted()
