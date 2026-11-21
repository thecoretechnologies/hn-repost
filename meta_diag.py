import os, json, requests
T = os.environ["HN_ACCESS_TOKEN"]; V = "v27.0"; G = f"https://graph.facebook.com/{V}/"
IG, APP, PAGE = "17841451199740926", "1084388714287870", "1535789706276956"
def call(path, **p):
    p["access_token"] = T
    try:
        r = requests.get(G + path, params=p, timeout=30); j = r.json()
    except Exception as e:
        return f"EXC {e}"
    if isinstance(j, dict) and "error" in j:
        j["error"].pop("fbtrace_id", None)
    s = json.dumps(j, ensure_ascii=False)
    return s.replace(T, "<token>")[:1500]
checks = [
 ("debug_token", "debug_token", {"input_token": T}),
 ("me", "me", {"fields": "id,name"}),
 ("app", APP, {"fields": "name,link,category"}),
 ("page", PAGE, {"fields": "name,verification_status"}),
 ("store ig", IG, {"fields": "username,followers_count,media_count"}),
 ("store media", f"{IG}/media", {"fields": "id,timestamp", "limit": 2}),
 ("publish limit", f"{IG}/content_publishing_limit", {"fields": "config,quota_usage"}),
 ("discovery", IG, {"fields": "business_discovery.username(hoodanutrition_official){username,media_count}"}),
]
for name, path, p in checks:
    print(f"== {name}: {call(path, **p)}")
