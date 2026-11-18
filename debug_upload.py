import os, json, time, sqlite3, subprocess, requests, sys
sys.path.insert(0, ".")
import hn_repost as h
TOK = os.environ["HN_ACCESS_TOKEN"]; IG = "17841451199740926"; G = "https://graph.facebook.com/v27.0"
def P(url, **d): d["access_token"] = TOK; r = requests.post(url, data=d, timeout=120); return r.status_code, r.text[:400]
def status(cid):
    for _ in range(12):
        j = requests.get(f"{G}/{cid}", params={"fields": "status_code,status,video_status", "access_token": TOK}, timeout=60).json()
        if j.get("status_code") in ("FINISHED", "ERROR"): break
        time.sleep(10)
    return json.dumps(j)[:500]
def resumable(path, uri_mode, extra={}):
    code, t = P(f"{G}/{IG}/media", media_type="REELS", upload_type="resumable", caption="debug", **extra)
    print("  container:", code, t)
    j = json.loads(t); cid = j.get("id")
    if not cid: return
    uri = j.get("uri") if uri_mode == "returned" else f"https://rupload.facebook.com/ig-api-upload/v27.0/{cid}"
    print("  uri:", uri)
    with open(path, "rb") as f:
        r = requests.post(uri, headers={"Authorization": f"OAuth {TOK}", "offset": "0", "file_size": str(os.path.getsize(path))}, data=f, timeout=600)
    print("  upload:", r.status_code, r.text[:300])
    print("  status:", status(cid))
def probe(p):
    print(subprocess.run(["ffprobe","-v","error","-show_entries","format=duration,size,bit_rate:stream=codec_name,profile,width,height,pix_fmt,r_frame_rate,sample_rate,channels","-of","compact",p],capture_output=True,text=True).stdout)
# synthetic test video
subprocess.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","testsrc2=s=1080x1920:r=30:d=8","-f","lavfi","-i","sine=d=8","-shortest","-c:v","libx264","-pix_fmt","yuv420p","-profile:v","high","-c:a","aac","-ar","48000","-movflags","+faststart","synth.mp4"],check=True)
# real processed reel
db = sqlite3.connect("state.db")
raw = json.loads(db.execute("select raw from items where permalink like '%DfmDOXZhGdk%'").fetchone()[0])
fresh = requests.get(f"{G}/{raw['id']}", params={"fields":"media_url","access_token":TOK}).json()
print("fresh media_url ok:", "media_url" in fresh)
src = h.download(fresh.get("media_url") or raw["media_url"], "raw.mp4")
probe("raw.mp4")
real = h.add_outro("raw.mp4", "assets/outro.mp4", "real.mp4", "libx264", dict(path="assets/badge.png", width=220, x=40, y=150, opacity=0.25))
probe("real.mp4")
def up(path, how):
    code, t = P(f"{G}/{IG}/media", media_type="REELS", upload_type="resumable", caption="debug")
    cid = json.loads(t)["id"]; uri = f"https://rupload.facebook.com/ig-api-upload/v27.0/{cid}"; size = os.path.getsize(path)
    data = open(path, "rb").read()
    if how == "bytes":
        r = requests.post(uri, headers={"Authorization": f"OAuth {TOK}", "offset": "0", "file_size": str(size)}, data=data, timeout=600)
        print("  bytes:", r.status_code, r.text[:200])
    elif how == "chunks":
        step = 4 * 1024 * 1024; off = 0
        while off < size:
            r = requests.post(uri, headers={"Authorization": f"OAuth {TOK}", "offset": str(off), "file_size": str(size)}, data=data[off:off+step], timeout=600)
            print("  chunk", off, r.status_code, r.text[:150]); off += step
            if r.status_code != 200: break
    print("  status:", status(cid))
subprocess.run(["ffmpeg","-y","-loglevel","error","-i","real.mp4","-c:v","libx264","-preset","veryfast","-b:v","900k","-maxrate","1M","-bufsize","2M","-c:a","aac","-b:a","96k","-movflags","+faststart","small.mp4"],check=True)
probe("small.mp4")
print("== REAL bytes"); up("real.mp4","bytes")
print("== REAL chunks"); up("real.mp4","chunks")
print("== SMALL bytes"); up("small.mp4","bytes")
