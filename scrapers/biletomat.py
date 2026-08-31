"""biletomat.pl scraper.

Approach (preference 1 in CLAUDE.md — hidden JSON API).

The page at biletomat.pl/wydarzenia/muzyka *does* ship schema.org JSON-LD, and
that was the obvious route given ``find_jsonld()`` already existed. It is a
trap: the block only ever holds the first 18 events, and ``?page=N`` is ignored
server-side — pages 1, 2, 3 and 200 all return byte-for-byte the same list.
Pagination is done in the browser, so the JSON-LD is a dead end past page one.

The real source is an Angular app talking to its own API. Found by reading the
bundle rather than guessing: ``chunk-B4IANB7V.js`` has

    this.apiUrl = "/marketplace/events"          -> `${baseUrl}${apiUrl}/listing`

and ``main-E6OEU67A.js`` resolves that baseUrl with

    function hi(t){ return t.forSubDomain("api")... }   -> https://api.biletomat.pl

Which gives, with no auth, no cookies and no anti-bot:

    GET https://api.biletomat.pl/marketplace/events/listing
        ?page=<n>&size=<n>&genre=TECHNO,ELECTONIC,HOUSE
        -> [ {id, title, slug, displayPeriod{startsAt}, show{description,genres},
              location{name,address}, city{name,country{code}},
              offer{prices{marketplace{price}}}}, ... ]

Everything we need is on the listing item, so there is no per-event request at
all — this is the cheapest source in the project (~2 requests per run).

`robots.txt` (both biletomat.pl and api.biletomat.pl) is Cloudflare-managed:
``User-agent: * / Allow: /`` with no ``/api/`` restriction, plus a Disallow
list of AI crawlers (ClaudeBot, GPTBot, CCBot...). Our UA is TechnoRadar, and
the content signals say ``search=yes, use=reference`` — which is what this is:
an index that links back. Same situation as goingapp.pl.
"""

from __future__ import annotations

import json
import sys

import httpx

from .common import RateLimiter, polite_client, save_raw

API = "https://api.biletomat.pl/marketplace/events/listing"
EVENT_URL = "https://biletomat.pl/wydarzenia"

# `size` above 50 is silently treated as an OFFSET of `size - 50`: size=60
# returns 45 items starting at index 10, size=120 returns nothing at all.
# Asking for more does not get capped, it *skips data*. Do not raise this.
PAGE_SIZE = 50

# Passing a single genre returns 0 results — their parser wants a list, so the
# full set always goes in one query. `ELECTONIC` is spelled that way in their
# enum; do not "fix" it.
#
# Verified exclusions: DANCE is flamenco/burlesque/aerial, DISCO is potańcówka
# and "DiscoOpera przy świecach", EXPERIMENTAL is experimental *theatre*.
GENRES = ("TECHNO", "ELECTONIC", "HOUSE")

# Their enum -> a tag string normalise._TAG_TO_GENRE already understands.
_GENRE_HINT = {
    "TECHNO": "Techno",
    "ELECTONIC": "Elektronika",
    "HOUSE": "House",
}


def _price(offer: dict) -> float | None:
    """Cheapest marketplace ticket price in PLN, or None.

    Prices arrive as decimal strings ("102.00"). The API is PLN-only (the site
    hardcodes `{provide: sa, useValue: "PLN"}`), so there is no currency to
    check here — unlike exist.pl, which sells Budapest dates.
    """
    prices = (offer.get("prices") or {}).get("marketplace") or {}
    for key in ("price", "start"):
        raw = prices.get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(str(raw).replace(",", "."))
        except ValueError:
            continue
        if value > 0:
            return value
    return None


def _record(item: dict) -> dict | None:
    """Map one listing item onto the scraper contract. Return None to skip."""
    event_id = str(item.get("id") or "").strip()
    show = item.get("show") or {}
    slug = (item.get("slug") or show.get("slug") or "").strip()
    title = (item.get("title") or "").strip()
    if not event_id or not slug or not title:
        return None

    if item.get("cancelled"):
        return None

    date = (item.get("displayPeriod") or {}).get("startsAt")
    if not date:
        return None

    location = item.get("location") or {}
    venue = (location.get("name") or "").strip() or None

    city_obj = item.get("city") or {}
    city = (city_obj.get("name") or "").strip() or None

    # Keep the country so build.py's Poland filter can act on it.
    country = ((city_obj.get("country") or {}).get("code") or "").strip().upper()

    genres = show.get("genres") or []
    tags = [_GENRE_HINT[g] for g in genres if g in _GENRE_HINT]

    # The eventId query param matters: without it every date of a recurring
    # night points at the same page.
    url = f"{EVENT_URL}/{slug}-{show.get('id')}?eventId={event_id}"

    return {
        "source": "biletomat",
        "source_id": event_id,
        "title": title,
        "date": date,
        "city": city if country in ("", "PL") else f"{city} ({country})",
        "venue": venue,
        "artists": [],  # no performer field on this API
        "genres": [],  # build.py classifies from title + _description + _tags
        "price_min": _price(item.get("offer") or {}),
        "url": url,
        # --- hints for build.py, stripped from the final events.json ---
        "_description": show.get("description") or "",
        "_tags": tags,
    }


def _fetch_genre(client: httpx.Client, limiter: RateLimiter,
                 genre_param: str) -> list[dict]:
    """Every page of one genre query. Stops on a short page."""
    items: list[dict] = []
    page = 1
    while True:
        limiter.wait()
        r = client.get(API, params={"page": page, "size": PAGE_SIZE,
                                    "genre": genre_param})
        r.raise_for_status()
        batch = r.json()
        if not isinstance(batch, list):
            print(f"[biletomat] unexpected payload on page {page}", file=sys.stderr)
            break
        if page == 1:
            save_raw("biletomat_listing_page1.json",
                     json.dumps(batch, ensure_ascii=False, indent=2))
        items.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        page += 1
    return items


def fetch() -> list[dict]:
    """Return biletomat.pl's electronic-tagged events as contract records."""
    limiter = RateLimiter(2.0)
    records: list[dict] = []
    seen: set[str] = set()
    genre_param = ",".join(GENRES)

    with polite_client() as client:
        try:
            items = _fetch_genre(client, limiter, genre_param)
        except httpx.HTTPError as exc:
            print(f"[biletomat] listing fetch failed: {exc!r}", file=sys.stderr)
            return []

        print(f"[biletomat] {len(items)} tagged events returned", file=sys.stderr)

        for item in items:
            try:
                rec = _record(item)
                if not rec or rec["source_id"] in seen:
                    continue
                seen.add(rec["source_id"])
                records.append(rec)
            except Exception as exc:  # one bad item must not kill the source
                print(f"[biletomat] skipped {item.get('id')}: {exc!r}", file=sys.stderr)

    print(f"[biletomat] built {len(records)} records", file=sys.stderr)
    return records


if __name__ == "__main__":
    rows = sorted(fetch(), key=lambda r: r["date"])
    for r in rows:
        price = f"{r['price_min']:.0f} PLN" if r["price_min"] else "  ? PLN"
        print(f"{r['date'][:10]}  {price:>8}  {(r['city'] or '?'):<12}  "
              f"{(r['venue'] or '?')[:22]:<22}  {r['title'][:52]}")
    print(f"\n{len(rows)} events total")
