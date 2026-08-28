"""Reconnaissance script for candidate Polish ticketing sources.

Run this FIRST for any new source. It never parses anything — it only reports:
  - final URL (after redirects) and HTTP status
  - response length and content-type
  - whether the body looks like an anti-bot interstitial
  - whether it carries __NEXT_DATA__ / JSON-LD / obvious API hints
  - what robots.txt says about the paths we care about

Raw bodies are saved to raw/<host><path>.html so parsing logic can be written
against a real, inspected response later.

Usage:
    python diagnose.py            # check the built-in candidate list
    python diagnose.py <url> ...  # check specific URLs
"""

from __future__ import annotations

import sys
import time
import urllib.parse
from pathlib import Path

import httpx

UA = "TechnoRadar/0.1 (+https://github.com/techno-radar/bot)"
RAW = Path(__file__).parent / "raw"

# Candidate sources, priority order per CLAUDE.md.
CANDIDATES = [
    "https://stage24.pl/",
    "https://goingapp.pl/",
    "https://kicket.com/",
    "https://ebilet.pl/",
    "https://biletomat.pl/",
    "https://zalogarave.pl/",
]

ANTIBOT_MARKERS = (
    "captcha-delivery.com",
    "Please enable JS",
    "Checking your browser",
    "cf-browser-verification",
    "__cf_chl_",
    "Just a moment...",
    "Enable JavaScript and cookies to continue",
)


def save_raw(url: str, text: str) -> Path:
    parts = urllib.parse.urlsplit(url)
    name = (parts.netloc + parts.path).strip("/").replace("/", "_") or parts.netloc
    if not name.endswith((".html", ".json")):
        name += ".html"
    RAW.mkdir(exist_ok=True)
    path = RAW / name
    path.write_text(text, encoding="utf-8")
    return path


def check_robots(client: httpx.Client, base: str) -> str:
    parts = urllib.parse.urlsplit(base)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        r = client.get(robots_url)
    except httpx.HTTPError as exc:
        return f"  robots.txt: request failed ({exc!r})"
    if r.status_code != 200:
        return f"  robots.txt: HTTP {r.status_code}"
    lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]
    # Show the global section plus any Disallow lines — enough to judge.
    interesting = [
        ln for ln in lines
        if ln.lower().startswith(("user-agent:", "disallow:", "allow:", "crawl-delay:", "sitemap:"))
    ]
    body = "\n".join(f"    {ln}" for ln in interesting[:40])
    return f"  robots.txt ({len(lines)} lines):\n{body}"


def looks_like(text: str, markers: tuple[str, ...]) -> list[str]:
    low = text.lower()
    return [m for m in markers if m.lower() in low]


def diagnose(client: httpx.Client, url: str) -> None:
    print(f"\n=== {url}")
    try:
        r = client.get(url)
    except httpx.HTTPError as exc:
        print(f"  request failed: {exc!r}")
        return

    ctype = r.headers.get("content-type", "?")
    print(f"  final url : {r.url}")
    print(f"  status    : {r.status_code}")
    print(f"  length    : {len(r.text)}")
    print(f"  type      : {ctype}")
    print(f"  server    : {r.headers.get('server', '?')}")
    if "set-cookie" in r.headers:
        cookie_names = [c.split("=", 1)[0].strip() for c in r.headers.get_list("set-cookie")]
        print(f"  cookies   : {', '.join(cookie_names)}")

    hits = looks_like(r.text, ANTIBOT_MARKERS)
    if hits:
        print(f"  ANTI-BOT  : matched {hits}")

    signals = []
    if "__NEXT_DATA__" in r.text:
        signals.append("__NEXT_DATA__ present (Next.js embedded state)")
    if "application/ld+json" in r.text:
        signals.append("JSON-LD present (schema.org Event likely)")
    if "window.__NUXT__" in r.text:
        signals.append("__NUXT__ present (Nuxt embedded state)")
    if "id=\"__NEXT_DATA__\"" not in r.text and "next/static" in r.text:
        signals.append("Next.js assets but no inline data (SSR or client fetch)")
    for signal in signals:
        print(f"  signal    : {signal}")

    print(f"  preview   : {r.text[:300]!r}")
    saved = save_raw(str(r.url), r.text)
    print(f"  saved     : {saved.relative_to(Path(__file__).parent)}")

    print(check_robots(client, url))


def main() -> None:
    urls = sys.argv[1:] or CANDIDATES
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.5",
    }
    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        for i, url in enumerate(urls):
            if i:
                time.sleep(2)  # ~1 request / 2s per domain
            diagnose(client, url)


if __name__ == "__main__":
    main()
