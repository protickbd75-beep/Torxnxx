import sys
import asyncio
import subprocess
import time
import re
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import requests
from bs4 import BeautifulSoup

# ── Bootstrap onionsearch args (required before import) ──────────────────────
sys.argv = ["onionsearch", "placeholder"]
import onionsearch as _os

# ── FIX #1 & #4: socks5h for remote DNS — patch ALL requests calls ───────────
TOR_SOCKS   = "socks5h://127.0.0.1:9050"
TOR_PROXIES = {"http": TOR_SOCKS, "https": TOR_SOCKS}

# Patch onionsearch global proxy dict
_os.proxies = TOR_PROXIES

# Monkey-patch requests.get so every engine uses Tor regardless of how it calls
_orig_get = requests.get
def _tor_get(url, **kwargs):
    kwargs.setdefault("proxies", TOR_PROXIES)
    kwargs.setdefault("timeout", 20)
    return _orig_get(url, **kwargs)
requests.get = _tor_get

# ── Engine list ───────────────────────────────────────────────────────────────
ENGINES = {
    "ahmia":          _os.ahmia,
    "darksearchio":   _os.darksearchio,
    "phobos":         _os.phobos,
    "tor66":          _os.tor66,
    "haystack":       _os.haystack,
    "tordex":         _os.tordex,
    "onionland":      _os.onionland,
    "deeplink":       _os.deeplink,
}

# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(title="TORRX", description="OnionSearch REST API via Tor", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Tor helpers ───────────────────────────────────────────────────────────────
def tor_running() -> bool:
    try:
        r = _orig_get("https://check.torproject.org/api/ip", proxies=TOR_PROXIES, timeout=15)
        return r.json().get("IsTor", False)
    except Exception:
        return False

def _wait_for_tor(max_wait: int = 120) -> bool:
    start = time.time()
    while time.time() - start < max_wait:
        if tor_running():
            return True
        time.sleep(4)
    return False


# ── FIX #2 & #3: Use torrc file, run as root (no appuser) ────────────────────
@app.on_event("startup")
async def startup_event():
    subprocess.Popen(
        ["tor", "-f", "/app/torrc"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("⏳ Waiting for Tor to bootstrap (up to 120s)...")
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, _wait_for_tor, 120)
    print("✅ Tor ready!" if ok else "⚠️  Tor timed out — .onion engines may fail.")


# ── Engine runner ─────────────────────────────────────────────────────────────
def _run_engine(engine_name: str, query: str) -> List[dict]:
    fn = ENGINES.get(engine_name)
    if not fn:
        return []
    try:
        raw = fn(query)
        results = []
        for item in raw:
            if isinstance(item, dict):
                results.append({
                    "engine": engine_name,
                    "name": item.get("name", ""),
                    "link": item.get("link", ""),
                    "description": item.get("text", ""),
                })
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                results.append({
                    "engine": engine_name,
                    "name": str(item[0]),
                    "link": str(item[1]),
                    "description": str(item[2]) if len(item) > 2 else "",
                })
        return results
    except Exception as e:
        print(f"[{engine_name}] error: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health", response_class=HTMLResponse)
def health():
    return """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>TORRX</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:#0a0a0a;display:flex;align-items:center;justify-content:center;height:100vh;font-family:monospace}
  .dot{width:18px;height:18px;background:#00ff88;border-radius:50%;display:inline-block;margin-bottom:16px;
       box-shadow:0 0 12px #00ff88;animation:pulse 1.5s infinite}
  @keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(1.3)}}
  .text{color:#00ff88;font-size:1.4rem;letter-spacing:2px}
</style></head>
<body><div style="text-align:center"><div class="dot"></div><div class="text">I AM OK</div></div></body>
</html>"""


@app.head("/")
def root_head():
    return HTMLResponse(status_code=200)


@app.get("/")
def root():
    return {"status": "ok", "endpoints": {
        "GET /search":  "?q=query&engines=ahmia,tor66&limit=20",
        "GET /fetch":   "?url=http://example.onion&extract=auto|text|html|links",
        "GET /engines": "List engines",
        "GET /tor":     "Tor status",
        "GET /health":  "Uptime HTML page",
    }}


@app.get("/engines")
def list_engines():
    return {"engines": list(ENGINES.keys())}


@app.get("/tor")
def tor_status():
    return {"tor_active": tor_running(), "proxy": TOR_SOCKS}


@app.get("/search")
async def search(
    q: str = Query(..., description="Search query"),
    engines: Optional[str] = Query(None, description="Comma-separated engines. Default: all"),
    limit: int = Query(30, ge=1, le=200),
):
    if not q.strip():
        raise HTTPException(400, "Query cannot be empty")

    selected = (
        [e.strip() for e in engines.split(",") if e.strip() in ENGINES]
        if engines else list(ENGINES.keys())
    )
    if not selected:
        raise HTTPException(400, f"No valid engines. Available: {list(ENGINES.keys())}")

    loop = asyncio.get_event_loop()
    tasks = [loop.run_in_executor(None, _run_engine, e, q) for e in selected]
    nested = await asyncio.gather(*tasks, return_exceptions=True)

    seen: set = set()
    results: List[dict] = []
    for batch in nested:
        if isinstance(batch, Exception):
            continue
        for item in batch:
            link = item.get("link", "")
            if link and link not in seen:
                seen.add(link)
                results.append(item)

    return {"query": q, "engines_used": selected, "total": len(results[:limit]), "results": results[:limit]}


@app.get("/fetch")
async def fetch_url(
    url: str = Query(..., description="URL to fetch via Tor"),
    extract: str = Query("auto", description="auto | text | html | links"),
):
    if not url.startswith("http"):
        raise HTTPException(400, "URL must start with http:// or https://")

    # ── FIX #1: Use requests+PySocks with socks5h — NOT httpx ────────────────
    # httpx strips the 'h' from socks5h breaking .onion DNS resolution.
    # requests[socks] + PySocks correctly does remote DNS through Tor.
    try:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, lambda: _orig_get(
            url,
            proxies=TOR_PROXIES,
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/115.0"},
            allow_redirects=True,
        ))
    except requests.exceptions.ConnectionError as e:
        raise HTTPException(503, f"Tor connect failed: {e}")
    except requests.exceptions.Timeout:
        raise HTTPException(504, "Timed out via Tor")
    except Exception as e:
        raise HTTPException(500, f"Fetch error: {e}")

    raw_html = resp.text
    content_type = resp.headers.get("content-type", "")

    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "meta", "link"]):
        tag.decompose()
    title = soup.title.string.strip() if soup.title and soup.title.string else ""

    if extract == "html":
        return {"url": url, "status_code": resp.status_code, "title": title, "html": raw_html[:50000]}

    if extract == "links":
        links = [
            {"text": a.get_text(strip=True), "url": a["href"]}
            for a in soup.find_all("a", href=True)
            if a["href"].startswith("http")
        ]
        return {"url": url, "status_code": resp.status_code, "title": title, "links": links}

    # auto / text
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text(separator="\n", strip=True))
    paragraphs = [p.get_text(strip=True) for p in soup.find_all("p") if p.get_text(strip=True)]
    return {
        "url": url, "status_code": resp.status_code, "content_type": content_type,
        "title": title, "text": text[:10000], "paragraphs": paragraphs[:50],
    }
