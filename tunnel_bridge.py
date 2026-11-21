"""Persistent Cloudflare HTTPS Tunnel forwarding to secondary PC (192.168.1.104:8000)."""
import time
import os
import re
import subprocess
from pathlib import Path
from pycloudflared.util import get_info

url_pattern = re.compile(r"(?P<url>https?://\S+\.trycloudflare\.com)")

def main():
    info = get_info()
    target = "http://192.168.1.104:8000"
    print(f"[Tunnel] Launching Cloudflare tunnel pointing to {target}...")
    
    args = [
        info.executable,
        "tunnel",
        "--url",
        target,
    ]
    
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    
    tunnel_url = None
    for _ in range(50):
        line = proc.stderr.readline()
        m = url_pattern.search(line)
        if m:
            tunnel_url = m.group("url")
            break
            
    if not tunnel_url:
        print("[X] Failed to extract Cloudflare tunnel URL")
        return
        
    print("=======================================================")
    print(f"MOBILE SECURE HTTPS URL: {tunnel_url}")
    print("=======================================================")
    
    os.makedirs("data", exist_ok=True)
    with open("data/mobile_url.txt", "w", encoding="utf-8") as f:
        f.write(tunnel_url)
        
    try:
        with open(r"\\192.168.1.104\Users\Salon\Bet-app\data\mobile_url.txt", "w", encoding="utf-8") as f:
            f.write(tunnel_url)
    except Exception:
        pass
        
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
