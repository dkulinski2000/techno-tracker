"""ebilet.pl scraper.

Approach (preference 3 — HTML + schema.org JSON-LD).

Preference 1 is not available here *by rule, not by difficulty*: ebilet's
`robots.txt` says

    User-agent: *
    Disallow: /api/
    Disallow: /cms/

so whatever internal API the site has is off limits and was not probed. The
public HTML is explicitly allowed, and it happens to carry everything we need
as structured data, so nothing is lost.

Two levels, both JSON-LD, no HTML structure touched at all:

1. **Listing** — ``/muzyka/elektro-techno`` ships a schema.org ``ItemList``
   whose members are full ``Event`` objects (name, startDate, url, city).
   This is the site's own electronic category, so it is a genre-accurate
   listing rather than a keyword guess. Paginated with ``?page=N``.

2. **Detail** — the per-event page ships an ``Event`` with three things the
   listing does not have:
     * the real **venue** (``location.name`` is "NIEBO" on the detail page but
       merely "Warszawa" — the city — in the listing),
     * ``offers[].lowPrice`` in PLN,
     * a real **description** instead of the listing's SEO boilerplate
       ("Bilety na Elektro i Techno: X już w sprzedaży na eBilet.pl ♫ ..."),
       which matters because that description is what build.py classifies on.

Detail responses are cached on disk exactly like stage24's, so a warm run costs
only the 3 listing requests.

Dates on this source are **naive** ("2026-09-16T20:00:00", no offset) unlike
biletomat's. That is fine — ``normalise.to_warsaw_iso`` localises a naive
timestamp to Europe/Warsaw, which is the correct reading for a Polish
ticketing site.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import httpx

from .common import RateLimiter, polite_client, save_raw

BASE = "https://www.ebilet.pl"
CATEGORY = "/muzyka/elektro-techno"

CACHE_DIR = Path(__file__).resolve().parent.parent / "raw" / "cache" / "ebilet"

# The whole category is one electronic bucket; normalise._TAG_TO_GENRE maps it.
CATEGORY_TAG = "Elektro i Techno"

# Generous upper bound — the walk stops on its own (see _listing_pages).
MAX_PAGES = 15


def _jsonld_blocks(html: str) -> list:
    """Every parsed application/ld+json payload on a page. Bad blocks skipped.

    ``common.find_jsonld`` returns the *first* matching object, which is what
    exist.py needs. Here the listing's payload is an ItemList wrapper, so the
    raw blocks are wanted instead.
    """
    out = []
    for m in re.finditer(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
    ):
        try:
            out.append(json.loads(m.group(1)))
        except (json.JSONDecodeError, ValueError):
            continue
    return out


def _listing_events(html: str) -> list[dict]:
    """The Event objects inside the listing page's ItemList."""
    for data in _jsonld_blocks(html):
        if isinstance(data, dict) and data.get("@type") == "ItemList":
            return [
                el["item"]
                for el in data.get("itemListElement") or []
                if isinstance(el, dict) and isinstance(el.get("item"), dict)
            ]
    return []


def _detail_event(html: str) -> dict | None:
    """The Event object on a detail page."""
    for data in _jsonld_blocks(html):
        for obj in data if isinstance(data, list) else [data]:
            if isinstance(obj, dict) and obj.get("@type") in ("Event", "MusicEvent"):
                return obj
    return None


def _slug(url: str) -> str:
    """Stable id for an event: the last path segment of its URL."""
    return (url or "").rstrip("/").rsplit("/", 1)[-1].split("?")[0]


def _price(event: dict) -> float | None:
    """Lowest PLN ticket price from the detail page's offers, or None."""
    offers = event.get("offers") or []
    if isinstance(offers, dict):
        offers = [offers]
    best = None
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        if offer.get("priceCurrency") not in (None, "PLN"):
            continue
        for key in ("lowPrice", "price"):
            raw = offer.get(key)
            if raw in (None, ""):
                continue
            try:
                value = float(str(raw).replace(",", "."))
            except ValueError:
                continue
            if value > 0 and (best is None or value < best):
                best = value
    return best


def _cached_detail(client: httpx.Client, limiter: RateLimiter,
                   url: str, slug: str) -> dict:
    """Fetch and parse one detail page, cached on disk. {} on any failure."""
    # The slug comes from a remote page and becomes a path — validate it before
    # it can pick where we write (same reasoning as stage24's event_id check).
    if not re.fullmatch(r"[0-9a-zA-Z_-]{1,80}", slug or ""):
        print(f"[ebilet] refusing odd slug {slug!r}", file=sys.stderr)
        return {}

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / f"{slug}.json"
    if cached.exists():
        try:
            return json.loads(cached.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            pass  # corrupt cache entry — refetch below

    limiter.wait()
    try:
        page = client.get(url)
        page.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"[ebilet] detail fetch failed for {slug}: {exc!r}", file=sys.stderr)
        return {}

    event = _detail_event(page.text) or {}
    cached.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
    return event


def _record(listed: dict, detail: dict) -> dict | None:
    """Merge listing + detail into one contract-shaped record."""
    url = listed.get("url") or detail.get("url")
    name = (listed.get("name") or detail.get("name") or "").strip()
    date = detail.get("startDate") or listed.get("startDate")
    if not url or not name or not date:
        return None

    address = (listed.get("location") or {}).get("address") or {}
    detail_loc = detail.get("location") or {}
    detail_addr = detail_loc.get("address") or {}

    city = (
        detail_addr.get("addressLocality") or address.get("addressLocality") or ""
    ).strip() or None
    venue = (detail_loc.get("name") or "").strip() or None
    # The listing calls the city the venue. If they agree we know nothing extra,
    # and a city masquerading as a venue would poison the venue+date dedup key.
    if venue and city and venue.casefold() == city.casefold():
        venue = None

    country = (
        detail_addr.get("addressCountry") or address.get("addressCountry") or ""
    ).strip().upper()

    description = (detail.get("description") or "").strip()
    if not description:
        description = (listed.get("description") or "").strip()

    return {
        "source": "ebilet",
        "source_id": _slug(url),
        "title": name,
        "date": date,
        "city": city if country in ("", "PL") else f"{city} ({country})",
        "venue": venue,
        "artists": [],
        "genres": [],  # build.py classifies from title + _description + _tags
        "price_min": _price(detail),
        "url": url,
        # --- hints for build.py, stripped from the final events.json ---
        "_description": description,
        "_tags": [CATEGORY_TAG],
    }


def _listing_pages(client: httpx.Client, limiter: RateLimiter) -> list[dict]:
    """Walk ?page=N until it stops yielding events we have not already seen.

    Two stop conditions, because ebilet signals "past the last page" in two
    different ways depending on the moment:

    * a **302 with no Location header**, which httpx cannot follow and which
      raise_for_status() would turn into an exception — so listing pages are
      status-checked by hand and any non-200 simply ends the walk;
    * a **200 serving page 1's content again**, which no length check would
      ever catch — hence "stop when no new URLs", not "stop when empty".
    """
    seen: dict[str, dict] = {}
    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE}{CATEGORY}" + ("" if page == 1 else f"?page={page}")
        limiter.wait()
        r = client.get(url)
        if r.status_code != 200:
            print(f"[ebilet] page {page}: HTTP {r.status_code}, end of listing",
                  file=sys.stderr)
            break
        if page == 1:
            save_raw("ebilet_elektro_page1.html", r.text)

        events = _listing_events(r.text)
        new = [e for e in events if e.get("url") and e["url"] not in seen]
        for e in new:
            seen[e["url"]] = e
        print(f"[ebilet] page {page}: {len(events)} listed, {len(new)} new "
              f"({len(seen)} total)", file=sys.stderr)

        if not new:
            break

    return list(seen.values())


def fetch(enrich: bool = True) -> list[dict]:
    """Return ebilet.pl's Elektro i Techno events as contract records.

    enrich=False skips the per-event detail requests (so no venue, price or
    real description) — useful for a fast smoke test.
    """
    limiter = RateLimiter(2.0)
    records: list[dict] = []

    with polite_client() as client:
        try:
            listed = _listing_pages(client, limiter)
        except httpx.HTTPError as exc:
            print(f"[ebilet] listing fetch failed: {exc!r}", file=sys.stderr)
            return []

        for item in listed:
            url = item.get("url") or ""
            try:
                detail = _cached_detail(client, limiter, url, _slug(url)) if enrich else {}
                rec = _record(item, detail)
                if rec:
                    records.append(rec)
            except Exception as exc:  # one bad page must not kill the source
                print(f"[ebilet] skipped {url}: {exc!r}", file=sys.stderr)

    print(f"[ebilet] built {len(records)} records", file=sys.stderr)
    return records


if __name__ == "__main__":
    fast = "--fast" in sys.argv
    rows = sorted(fetch(enrich=not fast), key=lambda r: r["date"])
    for r in rows:
        price = f"{r['price_min']:.0f} PLN" if r["price_min"] else "  ? PLN"
        print(f"{r['date'][:10]}  {price:>8}  {(r['city'] or '?'):<12}  "
              f"{(r['venue'] or '?')[:22]:<22}  {r['title'][:52]}")
    print(f"\n{len(rows)} events total")
