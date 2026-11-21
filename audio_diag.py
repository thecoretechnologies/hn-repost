import os, json, subprocess, requests
T=os.environ["HN_ACCESS_TOKEN"]; G="https://graph.facebook.com/v27.0/"
f="business_discovery.username(hoodanutrition_official){media.limit(8){id,permalink,media_url,media_product_type}}"
j=requests.get(G+"17841451199740926",params={"fields":f,"access_token":T},timeout=60).json()
def probe(p):
    o=subprocess.run(["ffprobe","-v","error","-show_entries","stream=codec_type,codec_name,duration","-of","json",p],capture_output=True,text=True).stdout
    return [(s["codec_type"],s.get("codec_name")) for s in json.loads(o or "{}").get("streams",[])]
for m in j["business_discovery"]["media"]["data"]:
    if not m.get("media_url"): print(m["permalink"],"no media_url"); continue
    p="/tmp/a.mp4"; open(p,"wb").write(requests.get(m["media_url"],timeout=120).content)
    print("API", m["permalink"], probe(p))
for code in ["Df_-JglO4pG"]:
    subprocess.run(["yt-dlp","-q","-f","mp4/bestvideo*+bestaudio/best","--merge-output-format","mp4","-o","/tmp/y.mp4",f"https://www.instagram.com/reel/{code}/"])
    print("YTDLP", code, probe("/tmp/y.mp4") if os.path.exists("/tmp/y.mp4") else "download failed")
