"""goingapp.pl scraper.

Approach (preference 1 — hidden JSON API):
goingapp.pl is a React SPA. Its search and listing pages are powered by a
**public, search-only Algolia index** (`search-main`). The app ships the
credentials in its JS bundle — they are meant for client-side use:

    POST https://faffkuslk0-dsn.algolia.net/1/indexes/search-main/query
    X-Algolia-Application-Id: FAFFKUSLK0
    X-Algolia-API-Key: 2116b4baed0596249c1f98b9a20dfc6c   (search-only)
    body: {"params": "query=techno&hitsPerPage=200&facetFilters=[[\"type:rundate\"]]"}

A hit for a dated event (`type: "rundate"`) already carries everything we need:
name, description, artists, venue, city (in `caption`), price, ISO start date,
and — usefully — human genre tags in `tags_names` (e.g. "Techno", "Industrial").

Algolia caps a single result set at 1000 hits and the index holds ~1800 future
rundates, most of them theatre and pop. We do NOT page the whole index. Free-
text queries ("rave", "warehouse") are no good either — Algolia's typo
tolerance rates comedy and classical shows as partial matches and there is no
score threshold in the response to cut on.

Instead we browse by genre tag (`tags_names`): "Techno", "House", "Elektronika",
"Industrial", "Muzyka basowa". Every hit is then genuinely an electronic event.
The cost is that a techno night GOING forgot to tag is missed — acceptable for
v1, and build.py's keyword pass over stage24 covers a lot of the same ground.
"""

from __future__ import annotations

import json
import sys
import urllib.parse

import httpx

from .common import RateLimiter, polite_client, save_raw

ALGOLIA_APP = "FAFFKUSLK0"
ALGOLIA_KEY = "2116b4baed0596249c1f98b9a20dfc6c"  # public search-only key
ALGOLIA_URL = f"https://{ALGOLIA_APP.lower()}-dsn.algolia.net/1/indexes/search-main/query"
EVENT_URL = "https://goingapp.pl/wydarzenie"

# Genre tags the site actually assigns. Queried with an empty search string so
# Algolia returns every rundate carrying the tag.
TAG_FILTERS = ["Techno", "House", "Elektronika", "Industrial", "Muzyka basowa"]

RETRIEVE = [
    "name_pl", "caption", "place_name", "start_date", "rundate",
    "tags_names", "category_name", "artists_names", "price", "currency",
    "slug", "rundate_slug", "description_pl", "timezone", "type",
]


def _query(client: httpx.Client, limiter: RateLimiter, *, query: str = "",
           facet_filters: list | None = None) -> list[dict]:
    params = {
        "query": query,
        "hitsPerPage": 200,
        "attributesToRetrieve": json.dumps(RETRIEVE),
    }
    filters = [["type:rundate"]]
    if facet_filters:
        filters.extend(facet_filters)
    params["facetFilters"] = json.dumps(filters)

    limiter.wait()
    r = client.post(ALGOLIA_URL, content=json.dumps({"params": urllib.parse.urlencode(params)}))
    r.raise_for_status()
    return r.json().get("hits", [])


def _city_from_caption(caption: str | None) -> str | None:
    """`caption` is "Venue, City" (occasionally just "City"). Take the tail."""
    if not caption:
        return None
    parts = [p.strip() for p in caption.split(",") if p.strip()]
    if not parts:
        return None
    tail = parts[-1]
    # "Bilety w całej Polsce" and similar are not cities.
    if "polsce" in tail.lower() or "polska" in tail.lower():
        return parts[-2] if len(parts) > 1 else None
    return tail


def _record(hit: dict) -> dict | None:
    slug = hit.get("slug")
    rundate_slug = hit.get("rundate_slug")
    date = hit.get("start_date") or hit.get("rundate")
    title = hit.get("name_pl")
    if not (slug and date and title):
        return None

    price = hit.get("price")
    price_min = float(price) if isinstance(price, (int, float)) and price > 0 else None

    artists = [a.strip() for a in (hit.get("artists_names") or []) if a and a.strip()]
    tags = [t for t in (hit.get("tags_names") or []) if t]

    url = f"{EVENT_URL}/{slug}"
    if rundate_slug:
        url = f"{url}/{rundate_slug}"

    return {
        "source": "going",
        "source_id": hit.get("objectID") or f"{slug}/{rundate_slug}",
        "title": title.strip(),
        "date": date,
        "city": _city_from_caption(hit.get("caption")),
        "venue": (hit.get("place_name") or "").strip() or None,
        "artists": artists,
        "genres": [],  # build.py classifies; _tags below is a strong hint
        "price_min": price_min,
        "url": url,
        # --- hints for build.py, stripped from the final events.json ---
        "_description": hit.get("description_pl") or "",
        "_tags": tags,
        "_category": hit.get("category_name") or "",
    }


def fetch() -> list[dict]:
    """Return electronic-music rundates from goingapp.pl as contract records."""
    limiter = RateLimiter(2.0)
    by_id: dict[str, dict] = {}
    raw_hits: list[dict] = []

    with polite_client(headers={
        "X-Algolia-Application-Id": ALGOLIA_APP,
        "X-Algolia-API-Key": ALGOLIA_KEY,
        "Content-Type": "application/json",
    }) as client:
        for tag in TAG_FILTERS:
            try:
                hits = _query(client, limiter, facet_filters=[[f"tags_names:{tag}"]])
            except httpx.HTTPError as exc:
                print(f"[going] tag {tag!r} failed: {exc!r}", file=sys.stderr)
                continue
            for hit in hits:
                raw_hits.append(hit)
                rec = _safe_record(hit)
                if rec:
                    by_id[rec["source_id"]] = _merge(by_id.get(rec["source_id"]), rec)

    save_raw("going_algolia_hits.json", json.dumps(raw_hits, ensure_ascii=False, indent=2))
    records = list(by_id.values())
    print(f"[going] built {len(records)} unique records from {len(raw_hits)} hits", file=sys.stderr)
    return records


def _safe_record(hit: dict) -> dict | None:
    try:
        return _record(hit)
    except Exception as exc:
        print(f"[going] skipped a hit: {exc!r}", file=sys.stderr)
        return None


def _merge(old: dict | None, new: dict) -> dict:
    """Same rundate returned under two tag filters — keep the fuller copy."""
    if not old:
        return new
    if len(new.get("_tags", [])) > len(old.get("_tags", [])):
        old["_tags"] = new["_tags"]
    for key in ("city", "venue", "price_min"):
        if not old.get(key) and new.get(key):
            old[key] = new[key]
    if len(new.get("artists", [])) > len(old.get("artists", [])):
        old["artists"] = new["artists"]
    return old


if __name__ == "__main__":
    rows = fetch()
    rows.sort(key=lambda r: r["date"])
    for r in rows:
        price = f"{r['price_min']:.0f} PLN" if r["price_min"] else "  ? PLN"
        tags = ",".join(r.get("_tags", []))
        print(f"{r['date'][:10]}  {price:>8}  {(r['city'] or '?'):<12}  {r['title'][:50]:<50}  [{tags}]")
    print(f"\n{len(rows)} events total")
