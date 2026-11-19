"""Find contact details (phone numbers / store location) printed inside a video and cover them
with the store's own details in a matching box."""
import glob, logging, os, re, shutil, subprocess, tempfile
from statistics import median
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("hn-repost")
BASE = os.path.dirname(os.path.abspath(__file__))
FONT = os.path.join(BASE, "assets", "DejaVuSerifCondensed-Bold.ttf")
PHONE_RE = re.compile(r"(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}")
# place names that mean "another branch's address" - generic words like "location" alone never trigger
DEFAULT_WORDS = ["address", "najafgarh", "sonipat", "gurugram", "gurgaon", "rohtak", "jhajjar", "bahadurgarh",
                 "panipat", "karnal", "hisar", "rewari", "narnaul", "faridabad", "palwal", "bhiwani", "jind",
                 "kaithal", "ambala", "noida", "ghaziabad", "dwarka", "rohini", "pitampura", "janakpuri"]


def _digits(s):
    return re.sub(r"\D", "", s)[-10:]


def _ocr_frame(path, cfg):
    import pytesseract
    im = Image.open(path)
    d = pytesseract.image_to_data(im, output_type=pytesseract.Output.DICT, config="--psm 11")
    words = []
    for k, t in enumerate(d["text"]):
        t = t.strip()
        if t and float(d["conf"][k]) > 35:
            words.append({"t": t, "x": d["left"][k], "y": d["top"][k], "w": d["width"][k], "h": d["height"][k]})
    return im, words


def _contact_box(words, cfg, ours):
    """Return (box, is_phone, text) for the contact block in one frame, or None."""
    kw = [w.lower() for w in cfg.get("keywords", DEFAULT_WORDS)]
    place = cfg.get("place", "").lower()
    # glue neighbouring digit chunks so "85718 50010" is seen as one number
    lines = {}
    for w in words:
        lines.setdefault(round((w["y"] + w["h"] / 2) / 18), []).append(w)
    trig = []
    for ws in lines.values():
        ws.sort(key=lambda w: w["x"])
        text = " ".join(w["t"] for w in ws)
        for m in PHONE_RE.finditer(text):
            if _digits(m.group()) in ours:
                continue
            num = [w for w in ws if re.search(r"\d{3,}", w["t"]) and w["t"].strip("+-") in m.group()]
            trig += num or ws
    for w in words:
        lw = w["t"].lower().strip(".,:/|")
        if lw in kw and (not place or place not in lw):
            trig.append(w)
    has_phone = any(re.search(r"\d{5}", w["t"]) for w in trig)
    if not has_phone and len(trig) < 1:
        return None
    lh = median([w["h"] for w in trig]) or 30
    # keep only trigger words that sit together (the main cluster around the phone number)
    anchor = next((w for w in trig if re.search(r"\d{5}", w["t"])), trig[0])
    ay = anchor["y"] + anchor["h"] / 2
    block = [w for w in trig if abs(w["y"] + w["h"] / 2 - ay) < 3.5 * lh]
    x0 = min(w["x"] for w in block); x1 = max(w["x"] + w["w"] for w in block)
    y0 = min(w["y"] for w in block); y1 = max(w["y"] + w["h"] for w in block)
    text = " ".join(w["t"] for w in sorted(block, key=lambda w: (w["y"] // 20, w["x"])))
    return (x0, y0, x1, y1), has_phone, text, lh


def find_card(im, box, lh):
    """Flood-fill the plain-coloured card behind the text, starting from the text box.
    Returns (card_box, colour, found_card)."""
    import numpy as np
    from PIL import ImageDraw as _D
    a = np.asarray(im.convert("RGB")).astype(int)
    H, W = a.shape[:2]
    x0, y0, x1, y1 = [int(v) for v in box]
    region = a[y0:y1, x0:x1].reshape(-1, 3)
    col = np.median(region, axis=0)
    near = np.abs(region - col).sum(axis=1) < 60
    if near.mean() < 0.45:                       # no plain background behind the text
        return box, tuple(int(c) for c in col), False
    col = region[near].mean(axis=0)
    mask = (np.abs(a - col).sum(axis=2) < 60).astype("uint8") * 255
    m = Image.fromarray(mask).convert("L").copy()
    seeds = [(x, y) for y in range(y0, y1, 3) for x in range(x0, x1, 3) if mask[y, x]]
    if not seeds:
        return box, tuple(int(c) for c in col), False
    for sx, sy in seeds[:: max(1, len(seeds) // 25)]:
        if m.getpixel((sx, sy)) == 255:
            _D.floodfill(m, (sx, sy), 128)
    filled = np.argwhere(np.asarray(m) == 128)
    cy0, cx0 = filled.min(axis=0); cy1, cx1 = filled.max(axis=0)
    too_big = (cy1 - cy0) > 9 * lh or (cx1 - cx0) * (cy1 - cy0) > 0.35 * W * H
    if too_big:                                  # leaked into a plain wall/background - just use the text area
        return box, tuple(int(c) for c in col), False
    return (int(cx0), int(cy0), int(cx1) + 1, int(cy1) + 1), tuple(int(c) for c in col), True


def _card_colour(im, box, pad):
    """Colour of the background around the text (the original card/box), sampled at the padding."""
    x0, y0, x1, y1 = box
    rgb = im.convert("RGB")
    W, H = rgb.size
    pts = []
    for x in range(int(x0), int(x1), 6):
        for y in (y0 - pad * 0.6, y1 + pad * 0.6):
            if 0 <= y < H:
                pts.append(rgb.getpixel((x, int(y))))
    for y in range(int(y0), int(y1), 6):
        for x in (x0 - pad * 0.6, x1 + pad * 0.6):
            if 0 <= x < W:
                pts.append(rgb.getpixel((int(x), y)))
    if not pts:
        return (255, 255, 255)
    return tuple(int(median(p[i] for p in pts)) for i in range(3))


def _render(size, box, bg, two_lines, title, place, phone):
    W, H = size
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    x0, y0, x1, y1 = box
    lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
    ink = (12, 12, 12) if lum > 140 else (255, 255, 255)
    d.rounded_rectangle(box, radius=max(8, int((y1 - y0) * 0.13)), fill=bg + (255,))
    pin = Image.open(os.path.join(BASE, "assets", "icon_pin.png"))
    tel = Image.open(os.path.join(BASE, "assets", "icon_phone.png"))
    bw, bh = x1 - x0, y1 - y0
    rows = [title, None] if two_lines else [None]
    row_h = bh / len(rows)

    def fit(text_w_fn, max_w, max_h):
        for s in range(int(max_h * 0.72), 10, -1):
            f = ImageFont.truetype(FONT, s)
            if text_w_fn(f, s) <= max_w:
                return f, s
        return ImageFont.truetype(FONT, 11), 11

    cx = (x0 + x1) / 2
    for i, r in enumerate(rows):
        top = y0 + i * row_h
        if r:                                   # title line
            f, s = fit(lambda f, s: d.textlength(r, font=f), bw * 0.9, row_h)
            bb = d.textbbox((0, 0), r, font=f)
            d.text((cx - (bb[2] - bb[0]) / 2 - bb[0], top + (row_h - (bb[3] - bb[1])) / 2 - bb[1]), r, font=f, fill=ink)
            continue
        a = f"{place} / " if place else ""

        def width(f, s):
            ih = int(s * 0.9)
            pw = int(ih * pin.width / pin.height) + 6 if place else 0
            return pw + d.textlength(a, font=f) + int(ih * tel.width / tel.height) + 6 + d.textlength(phone, font=f)
        f, s = fit(width, bw * 0.92, row_h)
        ih = int(s * 0.9)
        bb = d.textbbox((0, 0), "U8", font=f)
        ty = top + (row_h - (bb[3] - bb[1])) / 2 - bb[1]
        mid = ty + bb[1] + (bb[3] - bb[1]) / 2
        x = cx - width(f, s) / 2
        if place:
            p = pin.resize((int(ih * pin.width / pin.height), ih), Image.LANCZOS)
            ov.alpha_composite(p, (int(x), int(mid - p.height / 2))); x += p.width + 6
            d.text((x, ty), a, font=f, fill=ink); x += d.textlength(a, font=f)
        t = tel.resize((int(ih * 0.9 * tel.width / tel.height), int(ih * 0.9)), Image.LANCZOS)
        ov.alpha_composite(t, (int(x), int(mid - t.height / 2))); x += t.width + 6
        d.text((x, ty), phone, font=f, fill=ink)
    return ov


def replace_contacts(src, dest, cfg, ours):
    """Returns (path, note). path == src when nothing was found."""
    if not shutil.which("tesseract"):
        log.warning("tesseract not installed - skipping on-screen contact replacement")
        return src, None
    tmp = tempfile.mkdtemp(prefix="ocr_")
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height:format=duration", "-of", "default=nw=1", src],
                             capture_output=True, text=True).stdout
        info = dict(l.split("=") for l in out.split() if "=" in l)
        W, H, dur = int(info["width"]), int(info["height"]), float(info.get("duration", 0) or 0)
        step = 0.5 if dur <= 90 else 1.0
        subprocess.run(["ffmpeg", "-loglevel", "error", "-i", src, "-vf", f"fps={1 / step}",
                        os.path.join(tmp, "f_%05d.png")], check=True, timeout=900)
        hits = []
        for i, p in enumerate(sorted(glob.glob(os.path.join(tmp, "f_*.png")))):
            im, words = _ocr_frame(p, cfg)
            r = _contact_box(words, cfg, ours)
            if r:
                hits.append((i * step, r, im, words))
        if not hits:
            return src, None
        # group frames into time segments, ignoring single-frame flickers of OCR
        segs, cur = [], [hits[0]]
        for h in hits[1:]:
            if h[0] - cur[-1][0] <= 1.5 * step + 0.01 and _overlap(h[1][0], cur[-1][1][0]):
                cur.append(h)
            else:
                segs.append(cur); cur = [h]
        segs.append(cur)
        segs = [s for s in segs if len(s) >= 2 or s[0][1][1]]
        if not segs:
            return src, None
        phone = cfg.get("phone", "")
        place = cfg.get("place", "")
        inputs, chains, notes = [], [], []
        for n, s in enumerate(segs):
            lh = median(h[1][3] for h in s)
            cards = [find_card(h[2], h[1][0], lh) for h in s[:: max(1, len(s) // 8)]]
            boxes = [c[0] for c in cards]
            box = tuple(sorted(b[i] for b in boxes)[len(boxes) // 2] for i in range(4))   # median box
            found = sum(c[2] for c in cards) >= len(cards) / 2
            pad = int(0.25 * lh) if found else max(14, int(0.6 * lh))
            box = (max(0, box[0] - pad), max(0, box[1] - pad), min(W, box[2] + pad), min(H, box[3] + pad))
            raw_h = box[3] - box[1] - 2 * pad
            text = " ".join(w["t"] for h in s[:6] for w in h[3]
                            if box[0] <= w["x"] + w["w"] / 2 <= box[2] and box[1] <= w["y"] + w["h"] / 2 <= box[3]).lower()
            two = raw_h > 1.7 * lh or any(k in text for k in ("location", "order", "visit", "address", "contact"))
            title = cfg.get("title", "Location / online order") if any(k in text for k in ("location", "order")) \
                else cfg.get("title_alt", "Hooda Nutrition")
            bg = cards[len(cards) // 2][1] if found else (255, 255, 255)
            png = os.path.join(tmp, f"ov_{n}.png")
            _render((W, H), tuple(int(v) for v in box), bg, two, title, place, phone).save(png)
            t0, t1 = max(0, s[0][0] - step), s[-1][0] + step
            if t1 >= dur - step * 1.5:
                t1 = dur + 1
            inputs += ["-i", png]
            notes.append(f"{_fmt(t0)}–{_fmt(min(t1, dur))}")
            chains.append((n + 1, t0, t1))
        fc, last = [], "0:v"
        for k, (idx, t0, t1) in enumerate(chains):
            lab = f"v{k}"
            fc.append(f"[{last}][{idx}:v]overlay=0:0:enable='between(t,{t0:.2f},{t1:.2f})'[{lab}]")
            last = lab
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src] + inputs +
                       ["-filter_complex", ";".join(fc), "-map", f"[{last}]", "-map", "0:a?", "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "18", "-c:a", "copy", dest], check=True, timeout=1800)
        merged = []
        for _, t0, t1 in chains:
            if merged and t0 - merged[-1][1] <= 1.5:
                merged[-1][1] = max(merged[-1][1], t1)
            else:
                merged.append([t0, t1])
        spans = ", ".join(f"{_fmt(a)}–{_fmt(min(b, dur))}" for a, b in merged)
        return dest, f"🔁 Replaced on-screen contact details ({spans})"
    except Exception as e:
        log.warning("on-screen contact replacement failed: %s", e)
        return src, f"⚠️ Couldn't check the video for on-screen contact details ({e})"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _overlap(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _fmt(t):
    t = max(0, int(t))
    return f"{t // 60}:{t % 60:02d}"
