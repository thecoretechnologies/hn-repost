import subprocess, os
r = subprocess.run(["gh","release","delete","debug","-R",os.environ["GITHUB_REPOSITORY"],"-y","--cleanup-tag"],capture_output=True,text=True)
print("cleanup", r.returncode, r.stderr[:200])
