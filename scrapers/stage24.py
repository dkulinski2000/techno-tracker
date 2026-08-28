"""stage24.pl scraper.

Approach (preference 1 in CLAUDE.md — hidden JSON API):
stage24.pl is an Angular app backed by a NestJS API at ``api.stage24.pl``.
The listing the site renders comes from:

    GET https://api.stage24.pl/events?page=<n>&limit=<n>
        -> {"items": [...], "total_count": int, "page_count": int}

Each list item already carries name, date, location, city and artists.
The per-event endpoint adds price and description:

    GET https://api.stage24.pl/events/<uuid>
        -> {..., "lowest_price": float, "description": "<html>", ...}

No auth, no anti-bot, no cookies required (verified with a plain httpx GET).
We still rate-limit to one request / 2s and cache event detail on disk, because
detail is fetched once per event and most events do not change between runs.

Genre is NOT decided here. The API's category triplet is coarse
(KONCERT / IMPREZA / FESTIVAL) but does carry one electronic bucket,
``ELEKTRO_I_TECHNO`` — we pass the categories to build.py as tag hints and let
it do the keyword classification over the title + ``_description``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import httpx

from .common import RateLimiter, polite_client, save_raw

API = "https://api.stage24.pl"
LIST_URL = f"{API}/events"
EVENT_URL = "https://stage24.pl/wydarzenia"  # public detail page (not the API host)
PAGE_SIZE = 100

CACHE_DIR = Path(__file__).resolve().parent.parent / "raw" / "cache" / "stage24"


def _get_json(client: httpx.Client, limiter: RateLimiter, url: str, **kw) -> dict:
    limiter.wait()
    r = client.get(url, **kw)
    r.raise_for_status()
    return r.json()


def _iter_list_pages(client: httpx.Client, limiter: RateLimiter, max_pages: int | None):
    """Yield raw event dicts from every listing page."""
    page = 1
    while True:
        data = _get_json(
            client, limiter, LIST_URL, params={"page": page, "limit": PAGE_SIZE}
        )
        items = data.get("items", [])
        page_count = data.get("page_count", 1)
        if page == 1:
            save_raw("stage24_events_page1.json", json.dumps(data, ensure_ascii=False, indent=2))
            print(
                f"[stage24] {data.get('total_count', '?')} events over {page_count} pages",
                file=sys.stderr,
            )
        yield from items
        page += 1
        if page > page_count or (max_pages and page > max_pages):
            break


def _event_detail(client: httpx.Client, limiter: RateLimiter, event_id: str) -> dict:
    """Fetch /events/<id>, cached on disk. Returns {} on any failure."""
    # event_id is a UUID from the API, but it becomes a filename, so it is
    # validated rather than trusted — an id containing "../" would otherwise
    # let the remote side choose where we write.
    if not re.fullmatch(r"[0-9a-zA-Z_-]{1,64}", event_id or ""):
        print(f"[stage24] refusing odd event id {event_id!r}", file=sys.stderr)
        return {}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / f"{event_id}.json"
    if cached.exists():
        try:
            return json.loads(cached.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass  # fall through and refetch
    try:
        detail = _get_json(client, limiter, f"{LIST_URL}/{event_id}")
    except httpx.HTTPError as exc:
        print(f"[stage24] detail fetch failed for {event_id}: {exc!r}", file=sys.stderr)
        return {}
    cached.write_text(json.dumps(detail, ensure_ascii=False), encoding="utf-8")
    return detail


# Fetching /events/<id> for all ~770 events twice a day is rude and pointless —
# we keep maybe 15% of them. So we only pull detail (price + description) when
# the cheap list fields already hint "electronic". Everything else is still
# returned as a record; build.py drops it as off-genre.
_ELECTRO_HINTS = (
    "techno", "elektro", "rave", "acid", "schranz", "industrial", "house",
    "trance", "hardstyle", "hardcore", "gabber", "warehouse", "dnb",
    "drum & bass", "drum and bass", "bass music", "b2b",
)


def _looks_electronic(item: dict) -> bool:
    blob = " ".join(
        str(item.get(k, ""))
        for k in ("name", "category", "second_category", "third_category")
    ).lower()
    return any(h in blob for h in _ELECTRO_HINTS)


def _record(item: dict, detail: dict) -> dict | None:
    """Map one API event onto the scraper contract. Return None to skip."""
    event_id = item.get("id")
    slug = item.get("url")
    if not event_id or not slug:
        return None

    location = item.get("location") or {}
    city = ((location.get("city") or {}).get("name") or "").strip() or None
    venue = (location.get("name") or "").strip() or None

    # date_start is ISO 8601 UTC ("...Z"); build.py converts to Europe/Warsaw.
    date = item.get("date_start") or item.get("date")
    if not date:
        return None

    artists = [a.strip() for a in (item.get("artists") or []) if a and a.strip()]

    price = detail.get("lowest_price")
    # A NestJS default of 0 on a paid ticket is almost always "not set"; techno
    # raves are never free, so treat 0 as unknown rather than free.
    price_min = float(price) if isinstance(price, (int, float)) and price > 0 else None

    categories = [
        item.get(k)
        for k in ("category", "second_category", "third_category")
        if item.get(k)
    ]

    return {
        "source": "stage24",
        "source_id": event_id,
        "title": (item.get("name") or "").strip(),
        "date": date,
        "city": city,
        "venue": venue,
        "artists": artists,
        "genres": [],  # build.py classifies
        "price_min": price_min,
        "url": f"{EVENT_URL}/{slug}",
        # --- hints for build.py, stripped from the final events.json ---
        "_description": detail.get("description") or "",
        "_categories": categories,
    }


def fetch(enrich: bool = True, max_pages: int | None = None) -> list[dict]:
    """Return every current stage24.pl event as contract-shaped records.

    enrich=False skips per-event detail requests (no price / description) —
    useful for a fast smoke test. max_pages caps listing pages for the same.
    """
    limiter = RateLimiter(2.0)
    records: list[dict] = []
    with polite_client() as client:
        for item in _iter_list_pages(client, limiter, max_pages):
            try:
                want_detail = enrich and _looks_electronic(item)
                detail = _event_detail(client, limiter, item["id"]) if want_detail else {}
                rec = _record(item, detail)
                if rec:
                    records.append(rec)
            except Exception as exc:  # never let one event kill the source
                print(f"[stage24] skipped {item.get('id')}: {exc!r}", file=sys.stderr)
    print(f"[stage24] built {len(records)} records", file=sys.stderr)
    return records


if __name__ == "__main__":
    # Smoke test: print events to the terminal, nothing else.
    fast = "--fast" in sys.argv
    rows = fetch(enrich=not fast, max_pages=2 if fast else None)
    for r in rows[:60]:
        price = f"{r['price_min']:.0f} PLN" if r["price_min"] else "  ? PLN"
        print(f"{r['date'][:10]}  {price:>8}  {(r['city'] or '?'):<12}  {r['title'][:64]}")
    print(f"\n{len(rows)} events total")
