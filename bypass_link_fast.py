"""
bypass_link_fast.py  -  optimized bypass_link.py with a SAFE baseline.

Default behaviour for the hubdrive tabs matches your original script (load,
short settle, up to 4 clicks x 6s).  Only changes that cannot alter site
behaviour are on by default:
  * no per-attempt disk I/O / global lock (session kept in memory)
  * tiny stagger between tabs (was up to 5.4s)
  * Drive link detected as soon as it appears (network event OR in the DOM)
  * final page fetched with httpx (auto-fallback to the browser)

Everything riskier is an opt-in flag so you can A/B test one at a time:
  --block-css     also block stylesheets on hubdrive tabs
  --strict        on hubdrive tabs, block third-party requests (ads/trackers)
                  except first-party + common CDNs / Google
  --fast-load     don't wait for DOMContentLoaded on hubdrive tabs ("commit")
  --settle 0      skip the networkidle settle wait before clicking
  --warp 20       speed up page clock (Date-based countdowns)
  --no-httpx      force the browser for the final page
  --no-skip-delay keep hubdrive's built-in 5s button delay (skipped by default)
  --httpx-hub     EXPERIMENTAL: Drive links via httpx POST, no browser tabs
  --probe         httpx-only test on the first hubdrive link (prints the JSON)
  --discover      record the real network calls (mediator page + first
                  hubdrive link) into discover_*.json / discover_page.html so
                  an httpx-only path can be written

Setup:
    pip install playwright httpx
    playwright install chromium

Usage:
    python bypass_link_fast.py "<url>"
    python bypass_link_fast.py "<url>" --workers 4
    python bypass_link_fast.py "<url>" --block-css --strict
    python bypass_link_fast.py "<url>" --discover
"""
import argparse
import asyncio
import base64
import json
import os
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse, urljoin, unquote

try:
    import httpx
except ImportError:  # httpx is optional; the browser path still works
    httpx = None

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

DEFAULT_URL = (
    "https://greenmotors.club/?id=bWlJMVFQYmxDQndKZzJ3Nm5QaHhtTHExVHVYYlVrRnNDWThua1BjZFRBTGN2YlNRWGgzRG5KZjVnbm5saFhnM3RFUHM0aEY1Q0pnYjZGaG1hWnhWWGltRTV5WTB6Qk9kSjcyWXZUUnE0TmM9"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Ad/tracker hosts that are always blocked (substring match on the URL).
BLOCKED_HOSTS = (
    "adsboosters.xyz",
    "cloudfront.net",
    "admaven",
    "googletagmanager.com",
    "lieusjabbed.com",
    "google-analytics.com",
    "doubleclick.net",
    "googlesyndication.com",
    "histats.com",
    "clarity.ms",
    "hotjar.com",
    "popads",
    "propellerads",
)

# Heavy resources nobody needs for scraping.
BLOCKED_RESOURCES = {"image", "media", "font"}

# Third-party hosts still allowed in --strict mode.
THIRD_PARTY_OK = (
    "google.com", "gstatic.com", "googleapis.com", "cloudflare.com",
    "jsdelivr.net", "jquery.com", "bootstrapcdn.com", "hcaptcha.com",
    "recaptcha.net",
)

# Original launch args (kept as-is on purpose).
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
]

# Runs before any page script.
#   __WARP__           page-clock speed factor (1 = off)
#   __TIMEOUT_RULES__  setTimeout shortening rules, per page type
INIT_JS = """
(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3, 4, 5] });
    window.chrome = window.chrome || { runtime: {} };

    Object.defineProperty(document, 'hidden',          { get: () => false });
    Object.defineProperty(document, 'visibilityState', { get: () => 'visible' });
    document.hasFocus = () => true;
    const _add = EventTarget.prototype.addEventListener;
    EventTarget.prototype.addEventListener = function (type, ...rest) {
        if (type === 'visibilitychange' || type === 'blur') return;
        return _add.call(this, type, ...rest);
    };

    const _si = window.setInterval;
    window.setInterval = function (fn, ms, ...args) {
        if (ms >= 900 && ms <= 1100) ms = 10;
        return _si.call(window, fn, ms, ...args);
    };
    const RULES = __TIMEOUT_RULES__;   // [[min_ms, max_ms, new_ms], ...]
    if (RULES.length) {
        const _st = window.setTimeout;
        window.setTimeout = function (fn, ms, ...args) {
            for (const [lo, hi, to] of RULES) {
                if (ms >= lo && ms <= hi) { ms = to; break; }
            }
            return _st.call(window, fn, ms, ...args);
        };
    }

    const K = __WARP__;
    if (K > 1) {
        const dNow = Date.now.bind(Date);
        const pNow = performance.now.bind(performance);
        const d0 = dNow(), p0 = pNow();
        Date.now = () => d0 + (dNow() - d0) * K;
        performance.now = () => p0 + (pNow() - p0) * K;
    }
})();
"""

# JS run on the final page (browser fallback).
EXTRACT_JS = """
() => {
    const root = document.querySelector('.entry-content') || document.body;
    const here = location.hostname;
    const out = [];
    root.querySelectorAll('a[href]').forEach(a => {
        let u;
        try { u = new URL(a.href, location.href); } catch { return; }
        if (!/^https?:$/.test(u.protocol) || u.hostname === here) return;
        const img = a.querySelector('img');
        out.push({
            href: u.href,
            text: (a.innerText || '').trim(),
            alt: img ? (img.getAttribute('alt') || '').trim() : '',
            img: img ? (img.getAttribute('src') || '') : '',
        });
    });
    return out;
}
"""

# Runs inside the page: returns a Drive URL if an <a href> to Drive exists.
DRIVE_FIND_JS = """() => {
    const re = /^(drive|docs|drive\\.usercontent)\\.google\\.com$/i;
    for (const a of document.querySelectorAll('a[href]')) {
        try {
            const u = new URL(a.href, location.href);
            if (re.test(u.hostname)) return u.href;
        } catch (e) {}
    }
    return false;
}"""

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "state.json")
META_FILE = os.path.join(SCRIPT_DIR, "meta.json")

DOWNLOAD_BTN = "#down"
DRIVE_HOST_RE = re.compile(
    r"^(drive|docs|drive\.usercontent)\.google\.com$", re.IGNORECASE
)

T0 = time.perf_counter()


@dataclass
class Cfg:
    workers: int = 10
    retries: int = 3
    clicks: int = 1
    click_wait: float = 20.0
    settle: float = 0.0
    warp: float = 1.0
    block_css: bool = False
    strict: bool = False
    fast_load: bool = False
    use_httpx: bool = True
    discover: bool = False
    skip_delay: bool = True
    httpx_hub: bool = False
    hub_delay: float = 0.0
    probe: bool = False


def log(msg: str) -> None:
    print(f"[{time.perf_counter() - T0:6.1f}s] {msg}", flush=True)


MEDIATOR_RULES = [[900, 1100, 10]]      # ~1s countdown ticks -> 10ms
HUB_SKIP_RULES = [[4900, 5100, 50]]     # hubdrive's hard-coded 5s delay -> 50ms


def init_script(warp: float, rules: list) -> str:
    return (INIT_JS.replace("__WARP__", str(float(warp)))
            .replace("__TIMEOUT_RULES__", json.dumps(rules)))


def is_drive_url(u: str) -> bool:
    try:
        host = urlparse(u).hostname or ""
    except ValueError:
        return False
    return bool(DRIVE_HOST_RE.match(host))


def base_domain(host: str) -> str:
    parts = (host or "").lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "")


# --------------------------------------------------------------------------
# Request routing (blocking)
# --------------------------------------------------------------------------
def _allowed_strict(req, site_domain: str) -> bool:
    """--strict: allow first-party, common CDNs/Google and main navigations."""
    try:
        if req.is_navigation_request() and req.frame.parent_frame is None:
            return True
    except Exception:  # noqa: BLE001
        return True
    p = urlparse(req.url)
    if p.scheme not in ("http", "https"):
        return True
    host = (p.hostname or "").lower()
    return any(host == d or host.endswith("." + d)
               for d in (site_domain, *THIRD_PARTY_OK))


def make_router(block_css: bool = False, strict: bool = False,
                site_domain: str | None = None, on_drive=None):
    blocked_types = set(BLOCKED_RESOURCES)
    if block_css:
        blocked_types.add("stylesheet")

    async def handler(route):
        req = route.request
        u = req.url
        if on_drive is not None and is_drive_url(u):
            on_drive(u)
            await route.continue_()
            return
        if req.resource_type in blocked_types or any(h in u for h in BLOCKED_HOSTS):
            await route.abort()
            return
        if strict and site_domain and not _allowed_strict(req, site_domain):
            await route.abort()
            return
        await route.continue_()

    return handler


# --------------------------------------------------------------------------
# Helpers: names, files, debug
# --------------------------------------------------------------------------
def pretty_name(item: dict) -> str:
    if item["text"]:
        return item["text"]
    if item["alt"]:
        return item["alt"]
    if item["img"]:
        stem = item["img"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
        stem = re.sub(r"[-_]+", " ", stem)
        stem = re.sub(r"\d+", "", stem)
        stem = re.sub(r"(?i)\blogo\b", "", stem).strip()
        if stem:
            return stem
    return urlparse(item["href"]).hostname or item["href"]


async def debug_dump(page, tag: str) -> None:
    try:
        await page.screenshot(path=f"debug_{tag}.png", full_page=True)
        with open(f"debug_{tag}.html", "w", encoding="utf-8") as f:
            f.write(await page.content())
        log(f"[i] Saved debug_{tag}.png and debug_{tag}.html")
    except Exception as e:  # noqa: BLE001
        log(f"[i] Could not save debug files: {e}")


def load_json(path: str) -> dict:
    if not os.path.exists(path):
        log(f"[!] {path} not found - continuing without it")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_context_options(state_path: str, meta_path: str) -> dict:
    """new_context() kwargs from state.json + meta.json (read ONCE;
    storage_state is returned as an in-memory dict)."""
    meta = load_json(meta_path)

    def pick(*keys):
        for k in keys:
            if meta.get(k) is not None:
                return meta[k]
        return None

    opts = {"user_agent": pick("user_agent", "userAgent") or USER_AGENT}
    for opt, keys in {
        "viewport": ("viewport",),
        "locale": ("locale",),
        "timezone_id": ("timezone_id", "timezoneId", "timezone"),
        "extra_http_headers": ("extra_http_headers", "extraHTTPHeaders", "headers"),
        "device_scale_factor": ("device_scale_factor", "deviceScaleFactor"),
        "geolocation": ("geolocation",),
    }.items():
        val = pick(*keys)
        if val is not None:
            opts[opt] = val

    if os.path.exists(state_path):
        with open(state_path, "r", encoding="utf-8") as f:
            opts["storage_state"] = json.load(f)
    else:
        log(f"[!] {state_path} not found - opening links without a session")
    return opts


# --------------------------------------------------------------------------
# httpx path for the final (static) page
# --------------------------------------------------------------------------
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "param", "source", "track", "wbr"}


class LinkParser(HTMLParser):
    """Stdlib equivalent of EXTRACT_JS: external <a href> inside
    .entry-content (or the whole page if that container is missing)."""

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.host = urlparse(base_url).hostname
        self.depth = 0
        self.root_depth = None
        self.cur = None
        self.in_root: list = []
        self.anywhere: list = []
        self.in_title = False
        self.title = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag not in _VOID:
            self.depth += 1
        if self.root_depth is None and "entry-content" in (a.get("class") or "").split():
            self.root_depth = self.depth
        if tag == "a" and a.get("href"):
            self.cur = {"href": a["href"], "text": [], "alt": "", "img": "",
                        "root": self.root_depth is not None}
        elif tag == "img" and self.cur is not None and not self.cur["img"]:
            self.cur["alt"] = (a.get("alt") or "").strip()
            self.cur["img"] = a.get("src") or ""

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "a" and self.cur is not None:
            c, self.cur = self.cur, None
            try:
                href = urljoin(self.base, c["href"])
                u = urlparse(href)
            except ValueError:
                href, u = None, None
            if u and u.scheme in ("http", "https") and u.hostname != self.host:
                item = {"href": href,
                        "text": re.sub(r"\s+", " ", "".join(c["text"])).strip(),
                        "alt": c["alt"], "img": c["img"]}
                self.anywhere.append(item)
                if c["root"]:
                    self.in_root.append(item)
        if tag not in _VOID:
            if self.root_depth is not None and self.depth == self.root_depth:
                self.root_depth = None
            self.depth -= 1

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.cur is not None:
            self.cur["text"].append(data)


def parse_links_html(html: str, base_url: str):
    p = LinkParser(base_url)
    p.feed(html)
    p.close()
    return p.title.strip(), (p.in_root or p.anywhere)


def cookies_to_httpx(cookies: list):
    jar = httpx.Cookies()
    for c in cookies:
        try:
            jar.set(c["name"], c["value"], domain=c.get("domain", ""),
                    path=c.get("path", "/"))
        except Exception:  # noqa: BLE001
            pass
    return jar


async def scrape_final_httpx(final_url: str, referer: str, cookies: list):
    """Return (title, raw_items) or None (caller falls back to the browser)."""
    if httpx is None:
        log("[i] httpx not installed - using the browser (pip install httpx)")
        return None
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
    }
    try:
        async with httpx.AsyncClient(headers=headers,
                                     cookies=cookies_to_httpx(cookies),
                                     follow_redirects=True,
                                     timeout=httpx.Timeout(10.0)) as c:
            r = await c.get(final_url)
    except Exception as e:  # noqa: BLE001
        log(f"[i] httpx failed ({type(e).__name__}: {e}) - using the browser")
        return None
    if r.status_code != 200:
        log(f"[i] httpx got HTTP {r.status_code} - using the browser")
        return None
    title, items = parse_links_html(r.text, str(r.url))
    if not items:
        log("[i] httpx page has no links (JS-rendered?) - using the browser")
        return None
    return title, items


# --------------------------------------------------------------------------
# Discovery recorder (for building an httpx-only path later)
# --------------------------------------------------------------------------
def attach_recorder(page, rec: dict) -> None:
    def on_req(req):
        try:
            post = req.post_data
        except Exception:  # noqa: BLE001
            post = "<binary>"
        rec["events"].append({
            "t": round(time.perf_counter() - T0, 2), "kind": "request",
            "method": req.method, "type": req.resource_type, "url": req.url,
            "post_data": (post[:800] if post else None),
        })

    async def read_resp(resp):
        body = None
        try:
            if resp.request.resource_type in ("xhr", "fetch", "document"):
                body = (await resp.text())[:800]
        except Exception:  # noqa: BLE001
            pass
        rec["events"].append({
            "t": round(time.perf_counter() - T0, 2), "kind": "response",
            "status": resp.status, "type": resp.request.resource_type,
            "url": resp.url, "body": body,
        })

    page.on("request", on_req)
    page.on("response", lambda r: asyncio.ensure_future(read_resp(r)))


def save_discovery(rec: dict, name: str) -> None:
    with open(f"discover_{name}.json", "w", encoding="utf-8") as f:
        json.dump(rec["events"], f, indent=2, ensure_ascii=False)
    log(f"[i] Saved discover_{name}.json ({len(rec['events'])} events)")
    for e in rec["events"]:
        if "cdn-cgi" in e["url"]:
            continue
        if e["kind"] == "request" and (
                e["type"] in ("xhr", "fetch") or e["method"] != "GET"):
            print(f"      {e['t']:6.1f}s {e['method']:<5} {e['type']:<6} "
                  f"{e['url'][:90]}  {(e.get('post_data') or '')[:100]}")
        elif e["kind"] == "response" and ("ajax" in e["url"] or "newdl" in e["url"]):
            print(f"      {e['t']:6.1f}s <-{e['status']} {e['url'][:90]}  "
                  f"{(e.get('body') or '')[:300]!r}")


# --------------------------------------------------------------------------
# Drive-link detection
# --------------------------------------------------------------------------
async def wait_for_drive_link(page, drive_links: list, found: asyncio.Event,
                              timeout_s: float) -> str | None:
    """Return as soon as EITHER a Drive request is seen OR a Drive <a href>
    appears in the DOM (checked inside the browser every 200ms)."""
    if drive_links:
        return drive_links[0]

    ev_task = asyncio.ensure_future(found.wait())
    js_task = asyncio.ensure_future(
        page.wait_for_function(DRIVE_FIND_JS, timeout=timeout_s * 1000,
                               polling=200)
    )
    try:
        await asyncio.wait({ev_task, js_task}, timeout=timeout_s + 1,
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (ev_task, js_task):
            if not t.done():
                t.cancel()
        await asyncio.gather(ev_task, js_task, return_exceptions=True)

    if drive_links:
        return drive_links[0]
    if js_task.done() and not js_task.cancelled() and js_task.exception() is None:
        try:
            val = await js_task.result().json_value()
            if val:
                return val
        except Exception:  # noqa: BLE001
            pass
    if is_drive_url(page.url):
        return page.url
    return None


# --------------------------------------------------------------------------
# Hubdrive
# --------------------------------------------------------------------------
async def hubdrive_attempt(browser, idx: int, total: int, link: dict,
                           shared: dict, attempt: int, last: bool,
                           cfg: Cfg, rec: dict | None = None) -> dict:
    entry = {"name": link["name"], "url": link["url"], "attempt": attempt}
    tag = f"[{idx}/{total}]"

    opts = dict(shared["opts"])
    if shared.get("state"):
        opts["storage_state"] = shared["state"]
    context = await browser.new_context(**opts)
    try:
        skip = cfg.skip_delay and attempt == 1   # retries use normal timing
        await context.add_init_script(
            init_script(cfg.warp, HUB_SKIP_RULES if skip else []))

        drive_links: list = []
        found = asyncio.Event()

        def on_drive(u: str) -> None:
            if u not in drive_links:
                drive_links.append(u)
            found.set()

        host = urlparse(link["url"]).hostname or ""
        await context.route("**/*", make_router(
            cfg.block_css, cfg.strict, base_domain(host), on_drive))

        page = await context.new_page()
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))

        def on_popup(np):
            if np != page:
                asyncio.ensure_future(np.close())

        context.on("page", on_popup)
        if rec is not None:
            attach_recorder(page, rec)

        log(f"{tag} Opening (try {attempt}{', 5s-delay skipped' if skip else ''}) "
            f"{link['name']}: {link['url']}")
        persist = False
        try:
            resp = await page.goto(
                link["url"],
                wait_until="commit" if cfg.fast_load else "domcontentloaded",
                timeout=45_000)
            entry["status"] = resp.status if resp else None
            try:
                await page.wait_for_selector(DOWNLOAD_BTN, state="visible",
                                             timeout=20_000)
            except PWTimeout:
                entry["error"] = "download button (#down) not found"
                log(f"{tag} [!] Download button not found (session expired?)")
                if last:
                    await debug_dump(page, f"hubdrive_{idx}")
                return entry
            try:
                entry.update(final_url=page.url, title=await page.title())
            except Exception:  # noqa: BLE001
                entry.update(final_url=page.url)
            if rec is not None:
                rec["html"] = await page.content()
            persist = True

            # page's own JS must have wired up the button
            try:
                await page.wait_for_function(
                    """() => {
                        const b = document.querySelector('#down');
                        return b && !b.disabled && typeof window.myDownloadV2 === 'function'
                                 && typeof window.jQuery === 'function';
                    }""",
                    timeout=10_000,
                )
            except PWTimeout:
                log(f"{tag} [i] button handler not ready after 10s - clicking anyway")
            if cfg.settle > 0:
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=int(cfg.settle * 1000))
                except PWTimeout:
                    pass

            t_click = time.perf_counter()
            gdrive = None
            for n in range(1, cfg.clicks + 1):
                try:
                    await page.click(DOWNLOAD_BTN, force=True, timeout=5_000,
                                     no_wait_after=True)
                except Exception:  # noqa: BLE001
                    pass
                gdrive = await wait_for_drive_link(page, drive_links, found,
                                                   cfg.click_wait)
                if gdrive:
                    break

            if gdrive:
                entry["gdrive_link"] = gdrive
                entry["all_drive_urls"] = list(drive_links)
                entry.pop("error", None)
                log(f"{tag} [+] Google Drive link "
                    f"({time.perf_counter() - t_click:.1f}s after first click, "
                    f"click #{n}): {gdrive}")
            else:
                entry["error"] = "no google drive link after clicking"
                log(f"{tag} [!] No Google Drive link appeared")
                if last:
                    await debug_dump(page, f"hubdrive_{idx}")
        except PWTimeout:
            entry["error"] = "timeout"
            log(f"{tag} [!] Timed out")
        except Exception as e:  # noqa: BLE001
            entry["error"] = f"{type(e).__name__}: {e}"
            log(f"{tag} [!] Failed: {e}")

        if persist:  # keep refreshed cookies in memory for the next attempt
            try:
                shared["state"] = await context.storage_state()
            except Exception:  # noqa: BLE001
                pass
    finally:
        await context.close()
    return entry


async def process_hubdrive(browser, idx: int, link: dict, shared: dict,
                           sem: asyncio.Semaphore, total: int, delay: float,
                           cfg: Cfg) -> dict:
    async with sem:
        if delay:
            await asyncio.sleep(delay)
        if cfg.httpx_hub and httpx is not None:
            r = await hubdrive_httpx(link, shared, cfg.hub_delay)
            if r.get("gdrive_link"):
                log(f"[{idx}/{total}] [+] Google Drive link (httpx, no browser): "
                    f"{r['gdrive_link']}")
                return {"name": link["name"], "url": link["url"], "via": "httpx",
                        "gdrive_link": r["gdrive_link"]}
            log(f"[{idx}/{total}] [i] httpx path failed: {r.get('error')} "
                f"- using the browser")
        entry: dict = {}
        for attempt in range(1, cfg.retries + 1):
            entry = await hubdrive_attempt(
                browser, idx, total, link, shared, attempt,
                attempt == cfg.retries, cfg)
            if entry.get("gdrive_link"):
                break
            if attempt < cfg.retries:
                log(f"[{idx}/{total}] Retrying in {attempt}s...")
                await asyncio.sleep(attempt)
    return entry


# --------------------------------------------------------------------------
# httpx path for hubdrive (replicates what myDownloadV2() does)
# --------------------------------------------------------------------------
_DRIVE_URL_RE = re.compile(
    r"https?://(?:drive|docs|drive\.usercontent)\.google\.com/[^\s\"'<>\\]+", re.I)
_B64_RE = re.compile(r"^[A-Za-z0-9+/_=-]{16,4000}$")


def find_drive_url(obj) -> str | None:
    """Search any JSON value for a Drive URL (plain, url-encoded or base64)."""
    if isinstance(obj, str):
        for cand in (obj, unquote(obj)):
            m = _DRIVE_URL_RE.search(cand)
            if m:
                return m.group(0)
        if _B64_RE.match(obj):
            try:
                raw = obj.replace("-", "+").replace("_", "/")
                dec = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore")
                m = _DRIVE_URL_RE.search(dec) or _DRIVE_URL_RE.search(unquote(dec))
                if m:
                    return m.group(0)
            except Exception:  # noqa: BLE001
                pass
        return None
    if isinstance(obj, dict):
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            r = find_drive_url(v)
            if r:
                return r
    return None


async def hubdrive_httpx(link: dict, shared: dict, delay: float = 0.0,
                         want_newdl: bool = False) -> dict:
    """GET the file page, POST /ajax.php?ajax=download (what the button does
    after its 5s timer) and look for a Drive URL in the JSON answer."""
    if httpx is None:
        return {"error": "httpx not installed"}
    opts = shared["opts"]
    cookies = (shared.get("state") or {}).get("cookies", [])
    headers = {"User-Agent": opts.get("user_agent", USER_AGENT),
               "Accept-Language": "en-US,en;q=0.9"}
    headers.update(opts.get("extra_http_headers") or {})
    u = urlparse(link["url"])
    base = f"{u.scheme}://{u.netloc}"
    out: dict = {}
    try:
        async with httpx.AsyncClient(headers=headers, cookies=cookies_to_httpx(cookies),
                                     follow_redirects=True,
                                     timeout=httpx.Timeout(15.0)) as c:
            r = await c.get(link["url"], headers={
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
            out["page_status"] = r.status_code
            if r.status_code != 200:
                out["error"] = f"page HTTP {r.status_code}"
                return out
            m = re.search(r"""id=["']down-id["'][^>]*>\s*([^<]+?)\s*<""", r.text)
            fid = m.group(1) if m else u.path.rstrip("/").rsplit("/", 1)[-1]
            out["file_id"] = fid
            if delay:
                await asyncio.sleep(delay)
            r2 = await c.post(
                base + "/ajax.php?ajax=download", data={"id": fid},
                headers={"X-Requested-With": "XMLHttpRequest",
                         "Referer": str(r.url), "Origin": base,
                         "Accept": "application/json, text/javascript, */*; q=0.01"})
            out["ajax_status"] = r2.status_code
            try:
                js = r2.json()
            except Exception:  # noqa: BLE001
                out["error"] = f"ajax HTTP {r2.status_code}, not JSON"
                out["ajax_raw"] = r2.text[:500]
                return out
            out["ajax_json"] = js
            if want_newdl:
                r3 = await c.get(base + "/newdl", headers={"Referer": str(r.url)})
                out["newdl_status"] = r3.status_code
                out["newdl_html"] = r3.text
            if str(js.get("code")) != "200":
                out["error"] = f"ajax code {js.get('code')}: {js.get('file')}"
                return out
            url = find_drive_url(js.get("data"))
            if url:
                out["gdrive_link"] = url
            else:
                out["error"] = "ajax OK but no Drive URL in its data (see --probe)"
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def snippet_around(html: str, needle: str, width: int = 1500) -> str | None:
    for pat in (f"function {needle}", f"{needle} =", f"{needle}=", needle):
        i = html.find(pat)
        if i >= 0:
            return html[max(0, i - 200): i + width]
    return None


async def open_hubdrive_links(browser, links: list, state_path: str,
                              meta_path: str, cfg: Cfg,
                              out_path: str = "hubdrive_results.json") -> list:
    hub = [
        l for l in links
        if "hubdrive" in (urlparse(l["url"]).hostname or "").lower()
    ]
    if not hub:
        log("[!] No hubdrive links found in results.")
        return []

    opts = build_context_options(state_path, meta_path)   # disk read: once
    shared = {"opts": opts, "state": opts.pop("storage_state", None)}

    if cfg.probe:
        log("[probe] httpx-only test on the first hubdrive link")
        r = await hubdrive_httpx(hub[0], shared, cfg.hub_delay, want_newdl=True)
        html = r.pop("newdl_html", "")
        with open("probe_result.json", "w", encoding="utf-8") as f:
            json.dump(r, f, indent=2, ensure_ascii=False)
        if html:
            with open("probe_newdl.html", "w", encoding="utf-8") as f:
                f.write(html)
        print(json.dumps(r, indent=2, ensure_ascii=False)[:2500])
        snip = snippet_around(html, "dls", 1800) if html else None
        if snip:
            print("\n----- /newdl page, around 'dls' -----\n" + snip
                  + "\n---------------------------------------")
        log("[probe] saved probe_result.json and probe_newdl.html")
        return [r]

    if cfg.discover:
        log("[discover] recording the first hubdrive link only")
        rec = {"events": [], "html": ""}
        entry = await hubdrive_attempt(browser, 1, 1, hub[0], shared, 1, True,
                                       cfg, rec)
        save_discovery(rec, "hubdrive")
        if rec["html"]:
            with open("discover_page.html", "w", encoding="utf-8") as f:
                f.write(rec["html"])
            snip = snippet_around(rec["html"], "myDownloadV2")
            if snip:
                print("\n----- myDownloadV2 (from page HTML) -----\n"
                      + snip + "\n-----------------------------------------")
        return [entry]

    workers = max(1, cfg.workers)
    log(f"Found {len(hub)} hubdrive link(s); processing with {workers} workers")
    sem = asyncio.Semaphore(workers)
    entries = list(await asyncio.gather(*(
        process_hubdrive(browser, i, l, shared, sem, len(hub),
                         0.15 * ((i - 1) % workers), cfg)
        for i, l in enumerate(hub, 1)
    )))

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)

    if shared.get("state") and os.path.exists(state_path):  # write ONCE
        try:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(shared["state"], f)
        except Exception as e:  # noqa: BLE001
            log(f"[i] Could not update {state_path}: {e}")

    ok = sum(1 for e in entries if e.get("gdrive_link"))
    log(f"[+] Got {ok}/{len(entries)} Google Drive links -> {out_path}")
    return entries


# --------------------------------------------------------------------------
# Main flow
# --------------------------------------------------------------------------
async def run(url: str, headless: bool, state_path: str, meta_path: str,
              cfg: Cfg) -> dict | None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=LAUNCH_ARGS)
        log("Browser launched")
        try:
            # ---- Step 1: mediator page (needs the browser) ----------------
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1366, "height": 768},
                locale="en-US",
            )
            await context.route("**/*", make_router())
            await context.add_init_script(init_script(cfg.warp, MEDIATOR_RULES))

            page = await context.new_page()
            context.on(
                "page",
                lambda np: asyncio.ensure_future(np.close()) if np != page else None,
            )
            med_rec = {"events": [], "html": ""}
            if cfg.discover:
                attach_recorder(page, med_rec)

            log(f"[*] Opening {url}")
            await page.goto(url, wait_until="domcontentloaded")

            try:
                await page.wait_for_selector("#verify_btn", timeout=30_000)
            except PWTimeout:
                log("[!] Continue button not found.")
                await debug_dump(page, "no_button")
                return None
            log("[*] '#verify_btn' is visible")

            log("[*] Clicking 'Click To Continue'")
            await page.click("#verify_btn", force=True)

            log("[*] Waiting for countdown (sped up)...")
            try:
                await page.wait_for_function(
                    """() => {
                        const a = document.querySelector('a.get-link');
                        const h = a && a.getAttribute('href');
                        return h && !h.startsWith('javascript');
                    }""",
                    timeout=60_000,
                    polling=50,
                )
            except PWTimeout:
                log("[!] Countdown finished without a link appearing.")
                await debug_dump(page, "no_link")
                return None

            final_url = await page.get_attribute("a.get-link", "href")
            log(f"[+] Final page: {final_url}")
            if cfg.discover:
                save_discovery(med_rec, "mediator")

            # ---- Step 2: final page (httpx first, browser as fallback) ----
            referer = page.url
            scraped = None
            if cfg.use_httpx:
                scraped = await scrape_final_httpx(
                    final_url, referer, await context.cookies())
            if scraped:
                title_raw, raw = scraped
                title = title_raw.split("–")[0].strip()
                log("[+] Final page scraped with httpx")
            else:
                await page.goto(final_url, referer=referer,
                                wait_until="domcontentloaded")
                try:
                    await page.wait_for_selector(".entry-content a", timeout=30_000)
                except PWTimeout:
                    log("[!] No links found on final page.")
                    await debug_dump(page, "final_page")
                    return None
                title = (await page.title()).split("–")[0].strip()
                raw = await page.evaluate(EXTRACT_JS)
            await context.close()

            links, seen = [], set()
            for item in raw:
                if item["href"] in seen:
                    continue
                seen.add(item["href"])
                links.append({"name": pretty_name(item), "url": item["href"]})

            result = {"title": title, "page": final_url, "links": links}

            log(f"[+] {title}")
            for l in links:
                print(f"    {l['name']:<20} {l['url']}")

            with open("results.json", "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            log("[+] Saved to results.json")

            # ---- Step 3: hubdrive links in parallel -----------------------
            result["hubdrive"] = await open_hubdrive_links(
                browser, links, state_path, meta_path, cfg)
            with open("results.json", "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            log("[+] Done")
            return result
        finally:
            await browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default=DEFAULT_URL)
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--workers", type=int, default=10,
                    help="hubdrive links processed in parallel (default 10)")
    ap.add_argument("--retries", type=int, default=3,
                    help="attempts per hubdrive link (default 3)")
    ap.add_argument("--clicks", type=int, default=1,
                    help="download-button clicks per attempt (default 1; the "
                         "button disables itself after the first click)")
    ap.add_argument("--click-wait", type=float, default=20.0,
                    help="seconds to wait for a Drive link after each click (default 20)")
    ap.add_argument("--settle", type=float, default=0.0,
                    help="max seconds to wait for network idle before the first "
                         "click; 0 = skip (default 0)")
    ap.add_argument("--no-skip-delay", action="store_true",
                    help="keep hubdrive's built-in 5s delay (default: skipped on "
                         "the first attempt, normal timing on retries)")
    ap.add_argument("--httpx-hub", action="store_true",
                    help="EXPERIMENTAL: get Drive links via httpx POST (no browser "
                         "tabs); falls back to the browser per link")
    ap.add_argument("--hub-delay", type=float, default=0.0,
                    help="seconds to wait before the httpx POST (try 5 if the "
                         "server rejects an instant request)")
    ap.add_argument("--probe", action="store_true",
                    help="httpx-only test on the first hubdrive link; saves "
                         "probe_result.json + probe_newdl.html")
    ap.add_argument("--warp", type=float, default=1.0,
                    help="page clock speed-up factor, e.g. 20 (default off)")
    ap.add_argument("--block-css", action="store_true",
                    help="also block stylesheets on hubdrive tabs")
    ap.add_argument("--strict", action="store_true",
                    help="block third-party requests on hubdrive tabs")
    ap.add_argument("--fast-load", action="store_true",
                    help="don't wait for DOMContentLoaded on hubdrive tabs")
    ap.add_argument("--no-httpx", action="store_true",
                    help="use the browser for the final page")
    ap.add_argument("--discover", action="store_true",
                    help="record network calls for an httpx-only path")
    ap.add_argument("--state", default=STATE_FILE, help="Playwright storage state json")
    ap.add_argument("--meta", default=META_FILE, help="browser meta json")
    a = ap.parse_args()

    cfg = Cfg(workers=a.workers, retries=a.retries, clicks=a.clicks,
              click_wait=a.click_wait, settle=a.settle, warp=a.warp,
              block_css=a.block_css, strict=a.strict, fast_load=a.fast_load,
              use_httpx=not a.no_httpx, discover=a.discover,
              skip_delay=not a.no_skip_delay, httpx_hub=a.httpx_hub,
              hub_delay=a.hub_delay, probe=a.probe)
    asyncio.run(run(a.url, headless=not a.headed, state_path=a.state,
                    meta_path=a.meta, cfg=cfg))