"""Minimal Instagram web reader using a logged-in browser cookie file (cookies.txt).
Used only for things the official API can't do: reading other accounts' stories."""
import requests, sys

APP_ID = "936619743392459"   # Instagram web app id (public, used by instagram.com itself)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


class IGWeb:
    def __init__(self, cookie_file, proxy=None):
        self.s = requests.Session()
        names = set()
        for line in open(cookie_file, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7 or "instagram.com" not in parts[0]:
                continue
            name, value = parts[5], parts[6]
            self.s.cookies.set(name, value, domain=".instagram.com", path="/")
            names.add(name)
        if "sessionid" not in names:
            raise RuntimeError("the cookie file has no 'sessionid' - the account isn't logged in in that browser")
        self.s.headers.update({"User-Agent": UA, "X-IG-App-ID": APP_ID, "Referer": "https://www.instagram.com/",
                               "X-CSRFToken": self.s.cookies.get("csrftoken", "") or "",
                               "X-ASBD-ID": "129477", "X-IG-WWW-Claim": "0",
                               "X-Requested-With": "XMLHttpRequest", "Accept": "*/*"})
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        self._ids = {}

    HOSTS = ("https://www.instagram.com", "https://i.instagram.com")

    def _get(self, path, **params):
        """Try the web host first, then the i.instagram.com host."""
        errs = []
        for host in self.HOSTS:
            r = self.s.get(host + path, params=params, timeout=60, allow_redirects=False)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
                return r.json()
            logged_out = "not-logged-in" in r.text[:3000] or "login" in r.headers.get("location", "")
            errs.append(f"{host.split('//')[1]} HTTP {r.status_code}{' (logged out)' if logged_out else ''}")
        raise RuntimeError("Instagram refused: " + "; ".join(errs))

    def user_id(self, username):
        if username not in self._ids:
            j = self._get("/api/v1/users/web_profile_info/", username=username)
            self._ids[username] = j["data"]["user"]["id"]
        return self._ids[username]

    def stories(self, username):
        uid = self.user_id(username)
        j = self._get("/api/v1/feed/reels_media/", reel_ids=uid)
        reel = (j.get("reels") or {}).get(str(uid)) or {}
        out = []
        for it in reel.get("items", []):
            vids = it.get("video_versions") or []
            if vids:
                url, is_video = vids[0]["url"], True
            else:
                cands = (it.get("image_versions2") or {}).get("candidates") or []
                if not cands:
                    continue
                url, is_video = cands[0]["url"], False
            pk = str(it.get("pk") or it.get("id", "")).split("_")[0]
            out.append({"id": f"story_{pk}", "is_video": is_video, "url": url, "timestamp": it.get("taken_at"),
                        "permalink": f"https://www.instagram.com/stories/{username}/{pk}/"})
        return out

    def reel_url(self, permalink):
        raise RuntimeError("not supported")


if __name__ == "__main__":
    # quick test:  python3 ig_web.py cookies.txt hoodanutrition_official
    w = IGWeb(sys.argv[1])
    print("cookies found:", sorted(w.s.cookies.keys()))
    for u in sys.argv[2:] or ["hoodanutrition_official"]:
        st = w.stories(u)
        print(f"OK @{u}: {len(st)} story item(s) right now")
