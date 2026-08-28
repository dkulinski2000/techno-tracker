"""exist.pl scraper — EXIST, a Warsaw hard-techno promoter.

Approach (preference 3 — HTML parsing, because there is no API):
exist.pl is a **Webflow** site: server-rendered static HTML, no anti-bot, no
cookies needed, `robots.txt` is a single `Sitemap:` line with nothing
disallowed. A plain httpx GET returns the full page.

Every event page ships a complete schema.org ``MusicEvent`` block:

    {"@type": "MusicEvent", "name": ..., "startDate": "...Z",
     "location": {"name": <venue>, "address": {"addressLocality": <city>}},
     "offers": {"lowPrice": "150", "highPrice": "250",
                "priceCurrency": "PLN"}, ...}

So we never touch Webflow's generated class names — we read the JSON-LD, and
use `selectolax` only to pull `/events/...` hrefs off the listing page.

Listing source is **https://www.exist.pl/tickets**, not `sitemap.xml`. The
sitemap is stale: it was missing a live October event at the time of writing,
and it also carries `/pl-pl/` duplicates of every URL. `/tickets` is what the
site itself renders as "upcoming", so it is the honest listing.

Small source (~5 upcoming events) but ~100% in-scope: this is a techno
promoter, so signal-to-noise is far better than the general ticketing
platforms. It is also the pattern biletomat/ebilet will reuse.
"""

from __future__ import annotations

import html as html_mod
import re
import sys

import httpx
from selectolax.parser import HTMLParser

from .common import RateLimiter, find_jsonld, polite_client, save_raw

BASE = "https://www.exist.pl"
LISTING_URL = f"{BASE}/tickets"


def _event_paths(listing_html: str) -> list[str]:
    """Distinct /events/<slug> paths from the listing, in document order.

    Selects on `href` — a semantic attribute — never on Webflow's generated
    class names, which change on every publish.
    """
    seen: dict[str, None] = {}
    for node in HTMLParser(listing_html).css("a[href]"):
        href = (node.attributes.get("href") or "").split("#")[0].split("?")[0]
        # skip the /pl-pl/ locale duplicates; they carry identical JSON-LD
        if href.startswith("/events/") and len(href) > len("/events/"):
            seen.setdefault(href, None)
    return list(seen)


def _price(offers: dict) -> float | None:
    """Lowest ticket price in PLN, or None. Ignores non-PLN offers."""
    if not isinstance(offers, dict):
        return None
    if offers.get("priceCurrency") not in (None, "PLN"):
        return None
    for key in ("lowPrice", "price"):
        raw = offers.get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(str(raw).replace(",", "."))
        except ValueError:
            continue
        if value > 0:
            return value
    return None


def _headliner(name: str) -> list[str]:
    """Headliner from the event title, when the title states one.

    "EXIST pres. HOLY PRIEST" -> ["HOLY PRIEST"]; "BOILER ROOM WARSAW 2026" -> [].
    The *full* lineup only ever appears in free-form prose ("Completing the
    night's lineup are LUCA AGNELLI, ELMEFTI, ..."), and schema.org
    `performer` is always null on this site. Extracting that is the LLM pass
    CLAUDE.md defers to step 7 — not guessed at with a brittle regex here.
    """
    m = re.search(r"\bpres(?:ents)?\.?\s+(.+)$", name, re.IGNORECASE)
    if not m:
        return []
    act = m.group(1).strip(" .-—|")
    return [act] if act else []


def _record(ev: dict, path: str) -> dict | None:
    name = (ev.get("name") or "").strip()
    date = ev.get("startDate")
    if not name or not date:
        return None

    location = ev.get("location") or {}
    address = location.get("address") or {}
    venue = (location.get("name") or "").strip() or None
    city = (address.get("addressLocality") or "").strip() or None

    # The site is a Polish promoter but also runs dates abroad (Budapest).
    # Keep the country so build.py's Poland filter can act on it.
    country = (address.get("addressCountry") or "").strip().upper()

    description = html_mod.unescape(ev.get("description") or "")

    return {
        "source": "exist",
        "source_id": path.rsplit("/", 1)[-1],
        "title": name,
        "date": date,
        "city": city if country in ("", "PL") else f"{city} ({country})",
        "venue": venue,
        "artists": _headliner(name),
        "genres": [],  # build.py classifies from title + _description
        "price_min": _price(ev.get("offers") or {}),
        "url": ev.get("url") or f"{BASE}{path}",
        # --- hint for build.py, stripped from the final events.json ---
        "_description": description,
    }


def fetch() -> list[dict]:
    """Return upcoming exist.pl events as contract-shaped records."""
    limiter = RateLimiter(2.0)
    records: list[dict] = []

    with polite_client() as client:
        limiter.wait()
        try:
            listing = client.get(LISTING_URL)
            listing.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"[exist] listing fetch failed: {exc!r}", file=sys.stderr)
            return []

        save_raw("exist_tickets.html", listing.text)
        paths = _event_paths(listing.text)
        print(f"[exist] {len(paths)} event pages linked from /tickets", file=sys.stderr)

        for path in paths:
            limiter.wait()
            try:
                page = client.get(BASE + path)
                page.raise_for_status()
                ev = find_jsonld(page.text, "MusicEvent", "Event")
                if not ev:
                    print(f"[exist] no MusicEvent JSON-LD on {path}", file=sys.stderr)
                    continue
                rec = _record(ev, path)
                if rec:
                    records.append(rec)
            except Exception as exc:  # one bad page must not kill the source
                print(f"[exist] skipped {path}: {exc!r}", file=sys.stderr)

    print(f"[exist] built {len(records)} records", file=sys.stderr)
    return records


if __name__ == "__main__":
    rows = sorted(fetch(), key=lambda r: r["date"])
    for r in rows:
        price = f"{r['price_min']:.0f} PLN" if r["price_min"] else "  ? PLN"
        print(f"{r['date'][:10]}  {price:>8}  {(r['city'] or '?'):<10}  "
              f"{(r['venue'] or '?'):<12}  {r['title'][:44]:<44}  {r['artists']}")
    print(f"\n{len(rows)} events total")
