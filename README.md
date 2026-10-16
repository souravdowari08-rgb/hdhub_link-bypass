# Driveseed link API

Two queries, protected by an API key:

| Endpoint | What it does |
|---|---|
| `GET /hubdrive?url=<mediator url>` | returns the Hubdrive link(s) + all links |
| `GET /gdrive?url=<hubdrive url>` | returns the Google Drive link |
| `POST /session` | upload `state.json` / `meta.json` from your PC |
| `GET /session` | session status (cookie count, last update) |
| `GET /health` | health check (no key needed) |

Send the key as header `X-API-Key: <API_KEY>`.

---

## 1. Push to GitHub (from your PC)

Use a **private** repo. `.gitignore` already keeps `.env`, `state.json`, `meta.json` and `data/` out of git.

```powershell
cd C:\Users\sourav\Downloads\driveseed_cookies
git init
git add .
git commit -m "driveseed api"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

Check on GitHub that `state.json` and `.env` are NOT in the repo.

## 2. Open the port on Oracle Cloud

1. Console -> Networking -> Virtual Cloud Networks -> your VCN -> Security Lists -> Default -> **Add Ingress Rule**
   Source `0.0.0.0/0` (or your own IP/32), TCP, destination port `8000`.
2. On the VPS itself (Ubuntu image on Oracle blocks ports with iptables):

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8000 -j ACCEPT
sudo netfilter-persistent save
```
(Oracle Linux: `sudo firewall-cmd --permanent --add-port=8000/tcp && sudo firewall-cmd --reload`)

## 3. Deploy on the VPS

```bash
# install docker (once)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER      # then log out and back in

git clone https://github.com/<you>/<repo>.git driveseed-api
cd driveseed-api
cp .env.example .env
nano .env                          # set API_KEY (openssl rand -hex 32) and MAX_PARALLEL_TABS
docker compose up -d --build
docker compose logs -f             # should show "Browser launched, API ready"
```

Check: `curl http://127.0.0.1:8000/health`

## 4. Push your session from your PC

```powershell
pip install httpx
python push_session.py --server http://<VPS_IP>:8000 --key <API_KEY>
python push_session.py --server http://<VPS_IP>:8000 --key <API_KEY> --status
```

It uploads `state.json` and `meta.json` that sit next to `push_session.py`.
The server saves them to `./data/` (a Docker volume, survives rebuilds) and uses them
immediately - no restart needed. Run it again whenever the session expires
(hubdrive log says "Download button not found (session expired?)").

Tip: set `PUSH_SERVER` and `API_KEY` env vars once so you can just run `python push_session.py`.

## 5. Use it

```powershell
$h = @{ "X-API-Key" = "<API_KEY>" }
$u = [uri]::EscapeDataString("https://greenmotors.club/?id=...")
Invoke-RestMethod "http://<VPS_IP>:8000/hubdrive?url=$u" -Headers $h

$l = [uri]::EscapeDataString("https://hubdrive.pics/file/2137264134")
Invoke-RestMethod "http://<VPS_IP>:8000/gdrive?url=$l" -Headers $h
```
Interactive docs: `http://<VPS_IP>:8000/docs` (click **Authorize** and paste the key).

## Update later

```bash
cd driveseed-api && git pull && docker compose up -d --build
```

## Notes

* **RAM**: each Chromium tab costs ~150-300 MB. On the 1 GB AMD micro instance use `MAX_PARALLEL_TABS=2`.
  The free Ampere A1 (ARM) instance is much better; the Docker image supports it.
* **HTTPS**: over plain HTTP the API key and your cookies travel unencrypted. For real use put a domain
  in front with Caddy (automatic certificates):
  ```bash
  sudo apt install -y caddy
  echo 'api.yourdomain.com { reverse_proxy 127.0.0.1:8000 }' | sudo tee /etc/caddy/Caddyfile
  sudo systemctl reload caddy      # open ports 80 and 443 instead of 8000
  ```
* **Datacenter IP**: the site may treat a cookie session created on your home IP differently from an
  Oracle IP (Cloudflare etc.). If hubdrive keeps failing from the VPS, regenerate the session and push again.
