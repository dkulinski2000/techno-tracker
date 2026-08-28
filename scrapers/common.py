"""Tiny shared helpers for scrapers. Deliberately minimal — no framework.

Every scraper module is meant to stay independently readable and fixable, so
this file holds only things that would otherwise be copy-pasted verbatim:
the bot identity, a rate limiter, a raw-response dumper, and a JSON-LD reader.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
from selectolax.parser import HTMLParser

USER_AGENT = "TechnoRadar/0.1 (+https://github.com/techno-radar/bot)"

RAW_DIR = Path(__file__).resolve().parent.parent / "raw"

def polite_client(**kwargs) -> httpx.Client:
    """An httpx.Client with our UA, sane timeouts and redirect following."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.5",
    }
    headers.update(kwargs.pop("headers", {}))
    kwargs.setdefault("timeout", 30)
    kwargs.setdefault("follow_redirects", True)
    return httpx.Client(headers=headers, **kwargs)


class RateLimiter:
    """Block until at least `min_interval` seconds have passed since last call.

    One instance per domain. Default 2s matches the 'be a good citizen' rule
    in CLAUDE.md.
    """

    def __init__(self, min_interval: float = 2.0) -> None:
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


def save_raw(name: str, text: str) -> Path:
    """Dump a raw response body to raw/ for later inspection. Never in git."""
    RAW_DIR.mkdir(exist_ok=True)
    path = RAW_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def find_jsonld(html: str, *types: str) -> dict | None:
    """Return the first schema.org object of one of `types`, or None.

    Handles the three shapes real sites ship: a bare object, a list of
    objects, and an @graph wrapper. A malformed block is skipped, not raised —
    one bad script tag must not take down a source.
    """
    wanted = set(types)
    for node in HTMLParser(html).css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except (json.JSONDecodeError, ValueError):
            continue
        candidates = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            candidates = data["@graph"]
        for obj in candidates:
            if isinstance(obj, dict) and obj.get("@type") in wanted:
                return obj
    return None
