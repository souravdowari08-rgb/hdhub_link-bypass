"""
push_session.py - run on your LOCAL PC to upload state.json / meta.json
to the server.

    python push_session.py --server http://YOUR_VPS_IP:8000 --key YOUR_API_KEY
    python push_session.py --status        (just show what the server has)

You can also set the env vars PUSH_SERVER and API_KEY instead of the flags.
Needs:  pip install httpx
"""
import argparse
import json
import os
import sys

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.getenv("PUSH_SERVER"),
                    help="e.g. http://1.2.3.4:8000 or https://api.example.com")
    ap.add_argument("--key", default=os.getenv("API_KEY"), help="server API_KEY")
    ap.add_argument("--state", default=os.path.join(HERE, "state.json"))
    ap.add_argument("--meta", default=os.path.join(HERE, "meta.json"))
    ap.add_argument("--status", action="store_true",
                    help="only show the session status on the server")
    a = ap.parse_args()

    if not a.server or not a.key:
        print("Need --server and --key (or PUSH_SERVER / API_KEY env vars)")
        return 2
    server = a.server.rstrip("/")
    if not server.startswith("http"):
        server = "http://" + server
    headers = {"X-API-Key": a.key}

    try:
        if a.status:
            r = httpx.get(server + "/session", headers=headers, timeout=30)
        else:
            body = {}
            for field, path in (("state", a.state), ("meta", a.meta)):
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        body[field] = json.load(f)
                    print(f"[+] Loaded {path}")
                else:
                    print(f"[i] {path} not found - skipping")
            if not body:
                print("Nothing to push.")
                return 1
            r = httpx.post(server + "/session", json=body, headers=headers,
                           timeout=60)
    except httpx.HTTPError as e:
        print(f"[!] Request failed: {e}")
        return 1

    print(f"HTTP {r.status_code}: {r.text}")
    return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
