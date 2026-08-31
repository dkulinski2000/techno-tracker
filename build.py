"""Merge every scraper's output into docs/events.json.

Pipeline:  fetch  ->  normalise  ->  classify genre + filter to scope  ->
           drop past  ->  dedupe/merge  ->  sort  ->  write JSON

Design rules from CLAUDE.md that this file honours:
  * One broken source must not fail the build. Each fetch() is wrapped; on
    error we log it and carry on with whatever else returned.
  * The output is a single flat JSON file: {updated, count, events:[...]}.
  * Dedup key is normalised venue (or title) + calendar date; on collision the
    records are merged, preferring the more complete one.
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

from normalise import (
    KNOWN_GENRES,
    WARSAW,
    artist_key,
    canonical_city,
    classify_genres,
    clean_artists,
    clean_ws,
    curated_excluded,
    curated_genres,
    fold,
    in_scope,
    is_non_music,
    is_poland,
    safe_url,
    squash,
    tidy_genres,
    to_warsaw_iso,
)
from scrapers import biletomat, ebilet, exist, going, stage24

SOURCES = [stage24, going, exist, biletomat, ebilet]

OUT_PATH = Path(__file__).parent / "docs" / "events.json"
CURATED_PATH = Path(__file__).parent / "curated.json"

# The shipped record is whatever normalise() returns minus the leading-underscore
# keys, which are build-time hints only (see the pop in build()).


def load_curated() -> dict:
    """Read curated.json, warning loudly about typos rather than failing."""
    if not CURATED_PATH.exists():
        print("[build] no curated.json — running on detected genres only", file=sys.stderr)
        return {}
    try:
        data = json.loads(CURATED_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[build] curated.json is not valid JSON ({exc}); ignoring it", file=sys.stderr)
        return {}

    bad = {
        g
        for genres in (data.get("artists") or {}).values()
        for g in genres
        if g not in KNOWN_GENRES
    }
    bad |= {
        g
        for override in (data.get("events") or {}).values()
        for g in (override.get("genres") or [])
        if g not in KNOWN_GENRES
    }
    if bad:
        print(f"[build] curated.json: unknown genre labels {sorted(bad)} "
              f"— valid labels are {sorted(KNOWN_GENRES)}", file=sys.stderr)

    print(f"[build] curated: {len(data.get('artists') or {})} artists, "
          f"{len(data.get('not_electronic') or [])} excluded, "
          f"{len(data.get('events') or {})} event overrides", file=sys.stderr)
    return data


def run_scrapers() -> list[dict]:
    records: list[dict] = []
    for mod in SOURCES:
        name = mod.__name__.split(".")[-1]
        try:
            got = mod.fetch()
        except Exception:  # noqa: BLE001 — a source blowing up is expected
            print(f"[build] SOURCE FAILED: {name}", file=sys.stderr)
            traceback.print_exc()
            continue
        print(f"[build] {name}: {len(got)} raw records", file=sys.stderr)
        records.extend(got)
    return records


def normalise(rec: dict, curated: dict | None = None) -> dict | None:
    """Apply field normalisation + genre classification. None -> drop."""
    curated = curated or {}
    date = to_warsaw_iso(rec.get("date", ""))
    title = clean_ws(rec.get("title"))
    # An event with no usable ticket link is worthless here, and a non-http(s)
    # one is hostile — either way, drop it rather than ship it.
    url = safe_url(rec.get("url"))
    if not date or not title or not url:
        return None

    venue = clean_ws(rec.get("venue"))
    if venue and fold(venue) == fold(title):  # stage24 sometimes doubles these
        venue = None
    city = canonical_city(rec.get("city"))
    artists = clean_artists(rec.get("artists"), venue=venue, title=title)

    # stage24 categories behave like GOING's tags, so route both through the
    # tag mapper rather than the free-text matcher (which would over-call
    # "techno" on the "ELEKTRO_I_TECHNO" bucket string).
    source_tags = (rec.get("_tags") or []) + (rec.get("_categories") or [])
    genres = classify_genres(
        title,
        rec.get("_description", ""),
        tags=source_tags,
    )

    # Curated artist knowledge fills the gap the text cannot: an event page
    # rarely names its subgenre, but in this scene the lineup implies it.
    extra = curated_genres(artists, title, curated)
    if extra:
        genres = tidy_genres(genres + extra)

    price = rec.get("price_min")
    price_min = float(price) if isinstance(price, (int, float)) and price > 0 else None

    src = rec.get("source", "?")
    return {
        "title": title,
        "date": date,
        "city": city,
        "venue": venue,
        "artists": artists,
        "genres": genres,
        "price_min": price_min,
        "url": url,
        "source": src,
        "sources": [src],
        # kept internally for dedup + debugging, popped before write
        "_source_id": rec.get("source_id"),
    }


def dedup_key(rec: dict) -> tuple[str, str]:
    anchor = squash(rec["venue"]) if rec.get("venue") else squash(rec["title"])
    return anchor, rec["date"][:10]


def merge(a: dict, b: dict) -> dict:
    """Combine two records for the same event. Prefer the fuller value."""
    winner, other = (a, b) if _completeness(a) >= _completeness(b) else (b, a)
    out = dict(winner)

    for key in ("city", "venue", "url"):
        if not out.get(key) and other.get(key):
            out[key] = other[key]

    if other.get("price_min") is not None:
        out["price_min"] = (
            other["price_min"] if out.get("price_min") is None
            else min(out["price_min"], other["price_min"])
        )

    # union artists (fuzzy), genres, sources
    seen = {artist_key(x) for x in out["artists"]}
    for art in other["artists"]:
        if artist_key(art) not in seen:
            out["artists"].append(art)
            seen.add(artist_key(art))

    out["genres"] = list(dict.fromkeys(out["genres"] + other["genres"]))
    out["sources"] = sorted(set(out["sources"]) | set(other["sources"]))
    return out


def _completeness(rec: dict) -> int:
    score = 0
    score += 2 * len(rec.get("artists", []))
    score += len(rec.get("genres", []))
    score += 1 if rec.get("venue") else 0
    score += 1 if rec.get("city") else 0
    score += 1 if rec.get("price_min") is not None else 0
    return score


def build() -> dict:
    curated = load_curated()
    raw = run_scrapers()

    now = datetime.now(WARSAW)
    normalised: list[dict] = []
    dropped_scope = dropped_past = dropped_bad = dropped_abroad = 0
    dropped_curated = dropped_nonmusic = 0

    for rec in raw:
        norm = normalise(rec, curated)
        if norm is None:
            dropped_bad += 1
            continue
        if curated_excluded(norm["artists"], norm["title"], curated):
            dropped_curated += 1
            continue
        if is_non_music(norm["title"]):
            dropped_nonmusic += 1
            continue
        if not in_scope(norm["genres"]):
            dropped_scope += 1
            continue
        if not is_poland(norm["city"]):
            dropped_abroad += 1
            continue
        if norm["date"] < now.isoformat():
            dropped_past += 1
            continue
        normalised.append(norm)

    # dedupe / merge
    merged: dict[tuple[str, str], dict] = {}
    for rec in normalised:
        key = dedup_key(rec)
        merged[key] = merge(merged[key], rec) if key in merged else rec

    # Per-event overrides have the final say, keyed by ticket URL so they can
    # be copied straight out of the app. Applied after dedup so "replace"
    # semantics are not muddied by a merge.
    overrides = curated.get("events") or {}
    events = sorted(merged.values(), key=lambda r: r["date"])
    applied = 0
    if overrides:
        kept = []
        for ev in events:
            override = overrides.get(ev["url"])
            if override is None:
                kept.append(ev)
                continue
            applied += 1
            if "genres" in override:
                ev["genres"] = tidy_genres(override["genres"])
            for key in ("city", "venue", "title"):
                if key in override:
                    ev[key] = override[key]
            if ev["genres"]:  # genres: [] means "drop this event"
                kept.append(ev)
        events = kept
        unmatched = set(overrides) - {e["url"] for e in events}
        print(f"[build] applied {applied}/{len(overrides)} event overrides", file=sys.stderr)
        if applied < len(overrides):
            print(f"[build] override URLs that matched nothing: "
                  f"{sorted(unmatched)[:5]}", file=sys.stderr)

    for ev in events:
        ev.pop("_source_id", None)
        ev["artists"].sort(key=str.lower)

    print(
        f"[build] {len(raw)} raw -> {len(normalised)} in scope "
        f"-> {len(events)} after dedup  "
        f"(dropped: {dropped_bad} unparsable, {dropped_scope} off-genre, "
        f"{dropped_curated} curated-out, {dropped_nonmusic} non-music, "
        f"{dropped_abroad} abroad, {dropped_past} past)",
        file=sys.stderr,
    )

    return {
        "updated": now.isoformat(timespec="seconds"),
        "count": len(events),
        "events": events,
    }


def main() -> None:
    data = build()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[build] wrote {OUT_PATH.relative_to(Path(__file__).parent)} "
          f"({data['count']} events)", file=sys.stderr)


if __name__ == "__main__":
    main()
