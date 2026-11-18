import sqlite3, json, subprocess, sys, requests, os
db = sqlite3.connect("state.db")
mid = sys.argv[1].split()[0]
if mid == "latest":
    tok0 = os.environ.get("HN_ACCESS_TOKEN")
    media = requests.get("https://graph.facebook.com/v27.0/17841451199740926/media",
                         params={"fields": "id,media_type,media_product_type,thumbnail_url,media_url,permalink,timestamp",
                                 "limit": 25, "access_token": tok0}, timeout=30).json()
    for m in media.get("data", []):
        print("store media:", m.get("timestamp"), m.get("media_type"), m.get("permalink"))
    raw = next(m for m in media["data"] if m.get("media_type") == "VIDEO")
    mid = raw["id"]
    print("USING", raw.get("permalink"))
else:
    raw = json.loads(db.execute("SELECT raw FROM items WHERE media_id=?", (mid,)).fetchone()[0])
os.makedirs("out", exist_ok=True)
def get(url, dest):
    r = requests.get(url, timeout=60); r.raise_for_status(); open(dest, "wb").write(r.content); return dest
tok = os.environ.get("HN_ACCESS_TOKEN")
try:
    j = requests.get(f"https://graph.facebook.com/v27.0/{mid}", params={"fields":"thumbnail_url,media_url","access_token":tok}, timeout=30).json()
    print("fresh", list(j))
    raw.update({k:v for k,v in j.items() if k in ("thumbnail_url","media_url")})
except Exception as e:
    print("refresh failed", e)
cover = None
for key in ("thumbnail_url",):
    if raw.get(key):
        try: cover = get(raw[key], "cover.jpg")
        except Exception as e: print("thumb failed", e)
if not cover:
    get(raw["media_url"], "v.mp4")
    subprocess.run(["ffmpeg","-y","-loglevel","error","-ss","1","-i","v.mp4","-frames:v","1","cover.jpg"], check=True)
    cover = "cover.jpg"
base = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
frame = None
try:
    get(raw["media_url"], "v.mp4")
    subprocess.run(["ffmpeg","-y","-loglevel","error","-ss","5","-i","v.mp4","-frames:v","1","frame.jpg"], check=True)
    frame = "frame.jpg"
except Exception as e:
    print("video frame failed, using cover:", e)
    frame = cover
jobs = [("thumbnail_option3", cover, 220, 1.0, 290),     # 290: inside the 3:4 profile-grid crop (top 240px is cut)
        ("video_frame_watermark", frame, 220, 0.25, 150)]
for name, src, w, op, ypos in jobs:
    fc = f"[0:v]{base}[b];[1:v]scale={w}:-1,format=rgba,colorchannelmixer=aa={op}[l];[b][l]overlay=40:{ypos}"
    subprocess.run(["ffmpeg","-y","-loglevel","error","-i",src,"-i","assets/badge_dark_un.png","-filter_complex",fc,"-frames:v","1",f"out/{name}.jpg"], check=True)
print("done", frame)
