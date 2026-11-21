"""Persistent Cloudflare HTTPS Tunnel for mobile access."""
import time
from pycloudflared import try_cloudflare

def main():
    print("[Tunnel] Starting Cloudflare secure HTTPS tunnel for port 8000...")
    urls = try_cloudflare(port=8000, verbose=False)
    print("=======================================================")
    print(f"MOBILE SECURE HTTPS URL: {urls.tunnel}")
    print("=======================================================")
    with open("data/mobile_url.txt", "w", encoding="utf-8") as f:
        f.write(urls.tunnel)
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
