"""Minimal Instagram web reader using a logged-in browser cookie file (cookies.txt).
Used only for things the official API can't do: reading other accounts' stories."""
import http.cookiejar, requests, sys

APP_ID = "936619743392459"   # Instagram web app id (public, used by instagram.com itself)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


class IGWeb:
    def __init__(self, cookie_file, proxy=None):
        jar = http.cookiejar.MozillaCookieJar()
        jar.load(cookie_file, ignore_discard=True, ignore_expires=True)
        self.s = requests.Session()
        for c in jar:
            self.s.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
        names = {c.name for c in jar}
        if "sessionid" not in names:
            raise RuntimeError("the cookie file has no 'sessionid' - the account isn't logged in in that browser")
        self.s.headers.update({"User-Agent": UA, "X-IG-App-ID": APP_ID, "Referer": "https://www.instagram.com/",
                               "X-CSRFToken": self.s.cookies.get("csrftoken", ""),
                               "X-Requested-With": "XMLHttpRequest", "Accept": "*/*"})
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        self._ids = {}

    def _get(self, url, **params):
        r = self.s.get(url, params=params, timeout=60, allow_redirects=False)
        if r.status_code in (301, 302) or "login" in r.headers.get("location", ""):
            raise RuntimeError("Instagram says the spare account is logged out - export the cookies again")
        if r.status_code != 200:
            raise RuntimeError(f"Instagram HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def user_id(self, username):
        if username not in self._ids:
            j = self._get("https://www.instagram.com/api/v1/users/web_profile_info/", username=username)
            self._ids[username] = j["data"]["user"]["id"]
        return self._ids[username]

    def stories(self, username):
        uid = self.user_id(username)
        j = self._get("https://www.instagram.com/api/v1/feed/reels_media/", reel_ids=uid)
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
    for u in sys.argv[2:] or ["hoodanutrition_official"]:
        st = w.stories(u)
        print(f"OK @{u}: {len(st)} story item(s) right now")
