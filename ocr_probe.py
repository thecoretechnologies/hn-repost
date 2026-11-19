import subprocess, os
os.makedirs("out/ocr", exist_ok=True)
src="out/ocr/src.mp4"
if not os.path.exists(src):
    subprocess.run(["yt-dlp","-q","-f","mp4/best","-o",src,"https://www.instagram.com/reel/Dfy5W_UMZny/"])
subprocess.run(["ffmpeg","-y","-loglevel","error","-i",src,"-i","assets/contact_overlay.png","-filter_complex",
 "[0:v][1:v]scale2ref=iw:ih[ov][base];[base][ov]overlay=0:0:enable='between(t,0,29.5)'",
 "-c:v","libx264","-crf","24","-preset","veryfast","-c:a","copy","out/ocr/replaced_preview.mp4"])
print("size",os.path.getsize("out/ocr/replaced_preview.mp4"))
