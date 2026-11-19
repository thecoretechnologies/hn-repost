import subprocess, json, os, re, glob
os.makedirs("out/ocr", exist_ok=True)
url = "https://www.instagram.com/reel/Dfy5W_UMZny/"
subprocess.run(["yt-dlp", "-q", "-f", "mp4/best", "-o", "out/ocr/src.mp4", url])
print("downloaded", os.path.exists("out/ocr/src.mp4"))
subprocess.run(["ffmpeg","-loglevel","error","-i","out/ocr/src.mp4","-vf","fps=2","/tmp/f_%04d.png"])
import pytesseract
from PIL import Image
hits=[]
for i,p in enumerate(sorted(glob.glob("/tmp/f_*.png"))):
    t=i*0.5
    im=Image.open(p)
    d=pytesseract.image_to_data(im,output_type=pytesseract.Output.DICT,config="--psm 11")
    words=[(d["text"][k],d["left"][k],d["top"][k],d["width"][k],d["height"][k],d["conf"][k]) for k in range(len(d["text"])) if d["text"][k].strip() and float(d["conf"][k])>40]
    txt=" ".join(w[0] for w in words)
    if re.search(r"\d{5}\s?\d{5}|\+91|najafgarh|road|delhi|store|visit|call|whatsapp|address|pillar|nagar|near|shop",txt,re.I):
        hits.append({"t":t,"text":txt,"words":words,"size":im.size})
        if len([h for h in hits])<=12: im.save(f"out/ocr/hit_{t:05.1f}.jpg")
json.dump(hits,open("out/ocr/hits.json","w"),indent=1)
# contact sheet every 2s
subprocess.run(["ffmpeg","-loglevel","error","-i","out/ocr/src.mp4","-vf","fps=0.5,scale=270:-1,tile=6x3","-frames:v","1","out/ocr/sheet.jpg"])
print("hits",len(hits))
for h in hits: print(h["t"],h["text"][:150])
