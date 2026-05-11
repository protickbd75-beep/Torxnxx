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

# ── FIX #1 & #4: Proper socks5h proxy — patch ALL requests sessions ──────────
# socks5h = remote DNS via Tor (required for .onion resolution)
TOR_SOCKS    = "socks5h://127.0.0.1:9050"
TOR_PROXIES  = {"http": TOR_SOCKS, "https": TOR_SOCKS}

# Patch onionsearch global proxies dict
_os.proxies = TOR_PROXIES

# Also monkey-patch requests.get so any engine that calls it directly uses Tor
_original_requests_get = requests.get
def _tor_requests_get(url, **kwargs):
    kwargs.setdefault("proxies", TOR_PROXIES)
    kwargs.setdefault("timeout", 20)
    return _original_requests_get(url, **kwargs)
requests.get = _tor_requests_get

# ── Engine list ───────────────────────────────────────────────────────────────
ENGINES = {
    "ahmia":            _os.ahmia,
    "darksearchio":     _os.darksearchio,
    "phobos":           _os.phobos,
    "tor66":            _os.tor66,
    "haystack":         _os.haystack,
    "tordex":           _os.tordex,
    "onionland":        _os.onionland,
    "deeplink":         _os.deeplink,
}

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="TORRX — OnionSearch REST API",
    description="Search .onion sites and fetch content via Tor",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Tor helpers ───────────────────────────────────────────────────────────────
def tor_running() -> bool:
    try:
        r = _original_requests_get(
            "https://check.torproject.org/api/ip",
            proxies=TOR_PROXIES,
            timeout=15,
        )
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


# ── FIX #2 & #3: Use torrc, run as root, better Tor startup ──────────────────
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
    if ok:
        print("✅ Tor is ready!")
    else:
        print("⚠️  Tor bootstrap timed out — .onion engines may fail.")


# ── Engine runner (thread-safe) ───────────────────────────────────────────────
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
<html>
<head>
  <meta charset="UTF-8">
  <title>TORRX Status</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      background: #0a0a0a;
      display: flex;
      align-items: center;
      justify-content: center;
      height: 100vh;
      font-family: monospace;
    }
    .dot {
      width: 18px;
      height: 18px;
      background: #00ff88;
      border-radius: 50%;
      display: inline-block;
      margin-bottom: 16px;
      box-shadow: 0 0 12px #00ff88;
      animation: pulse 1.5s infinite;
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.5; transform: scale(1.3); }
    }
    .text { color: #00ff88; font-size: 1.4rem; letter-spacing: 2px; }
  </style>
</head>
<body>
  <div style="text-align:center">
    <div class="dot"></div>
    <div class="text">I AM OK</div>
  </div>
</body>
</html>"""


@app.get("/")
def root():
    return {
        "status": "ok",
        "endpoints": {
            "GET /search":  "?q=query&engines=ahmia,tor66&limit=20",
            "GET /fetch":   "?url=http://example.onion&extract=auto|text|html|links",
            "GET /engines": "List available search engines",
            "GET /tor":     "Check Tor connection status",
            "GET /health":  "Uptime check (HTML)",
        }
    }


@app.get("/engines")
def list_engines():
    return {"engines": list(ENGINES.keys())}


@app.get("/tor")
def tor_status():
    ok = tor_running()
    return {"tor_active": ok, "proxy": TOR_SOCKS}


@app.get("/search")
async def search(
    q: str = Query(..., description="Search query"),
    engines: Optional[str] = Query(None, description="Comma-separated engine names. Default: all"),
    limit: int = Query(30, ge=1, le=200, description="Max results"),
):
    if not q.strip():
        raise HTTPException(400, "Query cannot be empty")

    if engines:
        selected = [e.strip() for e in engines.split(",") if e.strip() in ENGINES]
        if not selected:
            raise HTTPException(400, f"No valid engines. Available: {list(ENGINES.keys())}")
    else:
        selected = list(ENGINES.keys())

    loop = asyncio.get_event_loop()
    tasks = [loop.run_in_executor(None, _run_engine, e, q) for e in selected]
    results_nested = await asyncio.gather(*tasks, return_exceptions=True)

    seen_links: set = set()
    all_results: List[dict] = []
    for batch in results_nested:
        if isinstance(batch, Exception):
            continue
        for item in batch:
            link = item.get("link", "")
            if link and link not in seen_links:
                seen_links.add(link)
                all_results.append(item)

    return {
        "query": q,
        "engines_used": selected,
        "total": len(all_results[:limit]),
        "results": all_results[:limit],
    }


@app.get("/fetch")
async def fetch_url(
    url: str = Query(..., description="URL to fetch via Tor"),
    extract: str = Query("auto", description="auto | text | html | links"),
):
    if not url.startswith("http"):
        raise HTTPException(400, "URL must start with http:// or https://")

    # ── FIX #1: Use requests + socks5h (NOT httpx which strips the h) ─────────
    # requests[socks] + PySocks correctly handles socks5h remote DNS for .onion
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: _original_requests_get(
                url,
                proxies=TOR_PROXIES,
                timeout=30,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/115.0"
                },
                allow_redirects=True,
            )
        )
    except requests.exceptions.ConnectionError as e:
        raise HTTPException(503, f"Could not connect via Tor: {str(e)}")
    except requests.exceptions.Timeout:
        raise HTTPException(504, "Request timed out via Tor")
    except Exception as e:
        raise HTTPException(500, f"Fetch error: {str(e)}")

    content_type = response.headers.get("content-type", "")
    raw_html = response.text

    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "meta", "link"]):
        tag.decompose()

    title = soup.title.string.strip() if soup.title and soup.title.string else ""

    if extract == "html":
        return {
            "url": url,
            "status_code": response.status_code,
            "title": title,
            "html": raw_html[:50000],
        }

    elif extract == "links":
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            if href.startswith("http"):
                links.append({"text": text, "url": href})
        return {
            "url": url,
            "status_code": response.status_code,
            "title": title,
            "links": links,
        }

    else:  # auto / text
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        paragraphs = [p.get_text(strip=True) for p in soup.find_all("p") if p.get_text(strip=True)]
        return {
            "url": url,
            "status_code": response.status_code,
            "content_type": content_type,
            "title": title,
            "text": text[:10000],
            "paragraphs": paragraphs[:50],
        }
