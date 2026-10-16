"""
api.py - FastAPI wrapper around bypass_link_fast.py

Endpoints (all except /health need the header  X-API-Key: <API_KEY>):
  GET  /hubdrive?url=<mediator url>   -> hubdrive link(s) + all links
  GET  /gdrive?url=<hubdrive url>     -> google drive link
  POST /session                       -> push state.json / meta.json from your PC
  GET  /session                       -> session status (no secrets returned)
  GET  /health                        -> liveness check

Env vars:
  API_KEY            (required) secret for X-API-Key
  DATA_DIR           where state.json / meta.json live (default: script folder)
  PORT               default 8000
  MAX_PARALLEL_TABS  default 10   (use 2-3 on a 1 GB VPS)
  HUBDRIVE_RETRIES   default 3
  REQUEST_TIMEOUT    default 120 (seconds)
"""
import asyncio
import hmac
import json
import os
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.security import APIKeyHeader
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from pydantic import BaseModel

import bypass_link_fast as bl

API_KEY = os.getenv("API_KEY", "")
DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", bl.SCRIPT_DIR))
STATE_PATH = os.path.join(DATA_DIR, "state.json")
META_PATH = os.path.join(DATA_DIR, "meta.json")

MAX_PARALLEL_TABS = int(os.getenv("MAX_PARALLEL_TABS", "10"))
HUBDRIVE_RETRIES = int(os.getenv("HUBDRIVE_RETRIES", "3"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "120"))

S: dict = {}  # app state: playwright, browser, shared session, semaphore, lock

_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_key(key: Optional[str] = Security(_key_header)) -> None:
    if not key or not hmac.compare_digest(key.encode(), API_KEY.encode()):
        raise HTTPException(401, "Invalid or missing X-API-Key")


# --------------------------------------------------------------------------
# Session files (state.json / meta.json)
# --------------------------------------------------------------------------
def write_json_atomic(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:   # mkstemp -> mode 0600
        json.dump(obj, f)
    os.replace(tmp, path)


def load_shared() -> dict:
    opts = bl.build_context_options(STATE_PATH, META_PATH)
    return {"opts": opts, "state": opts.pop("storage_state", None)}


async def persist_state() -> None:
    """Save refreshed cookies back to disk (same as the original script)."""
    async with S["lock"]:
        st = S["shared"].get("state")
        if st:
            try:
                write_json_atomic(STATE_PATH, st)
            except Exception as e:  # noqa: BLE001
                bl.log(f"[i] Could not save state: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not API_KEY:
        raise RuntimeError("API_KEY environment variable is required")
    os.makedirs(DATA_DIR, exist_ok=True)
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, args=bl.LAUNCH_ARGS)
    S.update(pw=pw, browser=browser, shared=load_shared(),
             sem=asyncio.Semaphore(MAX_PARALLEL_TABS), lock=asyncio.Lock())
    bl.log(f"Browser launched, API ready (data dir: {DATA_DIR})")
    try:
        yield
    finally:
        await browser.close()
        await pw.stop()


app = FastAPI(title="Link bypass API", lifespan=lifespan)


# --------------------------------------------------------------------------
# Part 1: mediator url -> hubdrive link(s)
# --------------------------------------------------------------------------
async def fetch_links(url: str) -> dict:
    browser = S["browser"]
    cfg = bl.Cfg()
    context = await browser.new_context(
        user_agent=bl.USER_AGENT,
        viewport={"width": 1366, "height": 768},
        locale="en-US",
    )
    try:
        await context.route("**/*", bl.make_router())
        await context.add_init_script(bl.init_script(cfg.warp, bl.MEDIATOR_RULES))

        page = await context.new_page()
        context.on("page", lambda np: asyncio.ensure_future(np.close())
                   if np != page else None)

        await page.goto(url, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector("#verify_btn", timeout=30_000)
        except PWTimeout:
            raise HTTPException(502, "Continue button (#verify_btn) not found")

        await page.click("#verify_btn", force=True)
        try:
            await page.wait_for_function(
                """() => {
                    const a = document.querySelector('a.get-link');
                    const h = a && a.getAttribute('href');
                    return h && !h.startsWith('javascript');
                }""",
                timeout=60_000, polling=50)
        except PWTimeout:
            raise HTTPException(502, "Countdown finished without a link appearing")

        final_url = await page.get_attribute("a.get-link", "href")
        referer = page.url

        scraped = await bl.scrape_final_httpx(
            final_url, referer, await context.cookies())
        if scraped:
            title_raw, raw = scraped
            title = title_raw.split("–")[0].strip()
        else:  # browser fallback
            await page.goto(final_url, referer=referer,
                            wait_until="domcontentloaded")
            try:
                await page.wait_for_selector(".entry-content a", timeout=30_000)
            except PWTimeout:
                raise HTTPException(502, "No links found on the final page")
            title = (await page.title()).split("–")[0].strip()
            raw = await page.evaluate(bl.EXTRACT_JS)
    finally:
        await context.close()

    links, seen = [], set()
    for item in raw:
        if item["href"] in seen:
            continue
        seen.add(item["href"])
        links.append({"name": bl.pretty_name(item), "url": item["href"]})

    hubdrive = [l for l in links
                if "hubdrive" in (urlparse(l["url"]).hostname or "").lower()]
    return {"title": title, "page": final_url,
            "hubdrive": hubdrive, "links": links}


@app.get("/hubdrive", dependencies=[Depends(require_key)])
async def get_hubdrive(url: str = Query(..., description="Mediator URL (URL-encode it)")):
    try:
        async with S["sem"]:
            return await asyncio.wait_for(fetch_links(url), REQUEST_TIMEOUT)
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        raise HTTPException(504, "Timed out")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------
# Part 2: hubdrive url -> google drive link
# --------------------------------------------------------------------------
async def fetch_gdrive(url: str) -> dict:
    cfg = bl.Cfg(retries=HUBDRIVE_RETRIES)
    link = {"name": "Hubdrive", "url": url}
    entry: dict = {}
    for attempt in range(1, cfg.retries + 1):
        # last=False -> never writes debug_*.png/html files from the API
        entry = await bl.hubdrive_attempt(
            S["browser"], 1, 1, link, S["shared"], attempt, False, cfg)
        if entry.get("gdrive_link"):
            break
        if attempt < cfg.retries:
            await asyncio.sleep(attempt)
    return entry


@app.get("/gdrive", dependencies=[Depends(require_key)])
async def get_gdrive(url: str = Query(..., description="Hubdrive file URL")):
    host = (urlparse(url).hostname or "").lower()
    if "hubdrive" not in host:
        raise HTTPException(400, "url must be a hubdrive link")
    try:
        async with S["sem"]:
            entry = await asyncio.wait_for(fetch_gdrive(url), REQUEST_TIMEOUT)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Timed out")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}")

    await persist_state()
    if not entry.get("gdrive_link"):
        raise HTTPException(502, entry.get("error") or "No Google Drive link found")
    return {"hubdrive": url,
            "title": entry.get("title"),
            "gdrive_link": entry["gdrive_link"]}


# --------------------------------------------------------------------------
# Session push (local PC -> server)
# --------------------------------------------------------------------------
class SessionPush(BaseModel):
    state: Optional[dict[str, Any]] = None   # contents of state.json
    meta: Optional[dict[str, Any]] = None    # contents of meta.json


def _mtime(path: str) -> Optional[int]:
    return int(os.path.getmtime(path)) if os.path.exists(path) else None


@app.post("/session", dependencies=[Depends(require_key)])
async def push_session(body: SessionPush):
    if body.state is None and body.meta is None:
        raise HTTPException(400, "Send at least one of: state, meta")
    if body.state is not None and not isinstance(body.state.get("cookies"), list):
        raise HTTPException(400, "state is not a Playwright storage_state "
                                 "(missing 'cookies' list)")
    saved = []
    async with S["lock"]:
        if body.state is not None:
            write_json_atomic(STATE_PATH, body.state)
            saved.append("state.json")
        if body.meta is not None:
            write_json_atomic(META_PATH, body.meta)
            saved.append("meta.json")
        new = load_shared()
        S["shared"].clear()          # in place: running requests see it too
        S["shared"].update(new)
    bl.log(f"[+] Session pushed: {', '.join(saved)}")
    return {"saved": saved,
            "cookies": len((S["shared"].get("state") or {}).get("cookies", []))}


@app.get("/session", dependencies=[Depends(require_key)])
async def session_status():
    st = S["shared"].get("state") or {}
    return {"state_loaded": bool(st),
            "cookies": len(st.get("cookies", [])),
            "state_file_updated": _mtime(STATE_PATH),
            "meta_file_updated": _mtime(META_PATH),
            "server_time": int(time.time())}


@app.get("/health")
async def health():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")),
                loop="asyncio")
