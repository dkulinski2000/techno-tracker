"""Show which events still need a human genre call, ranked by impact.

The keyword matcher can only find genres that are written down. Plenty of
event pages never name a subgenre — in this scene the lineup implies it. This
script tells you which artists are worth adding to `curated.json`: the ones
appearing on the most vaguely-labelled events.

    python curate.py            # artists worth curating + the vague events
    python curate.py --events   # just the event list, with ticket URLs
    python curate.py --all      # every artist seen, not only uncurated ones

Reads docs/events.json (so run build.py first) and curated.json.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from normalise import VAGUE, artist_key

ROOT = Path(__file__).parent
EVENTS_PATH = ROOT / "docs" / "events.json"
CURATED_PATH = ROOT / "curated.json"


def load() -> tuple[list[dict], dict]:
    if not EVENTS_PATH.exists():
        sys.exit("docs/events.json not found — run `python build.py` first.")
    events = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))["events"]
    curated = (
        json.loads(CURATED_PATH.read_text(encoding="utf-8"))
        if CURATED_PATH.exists() else {}
    )
    return events, curated


def is_vague(ev: dict) -> bool:
    """No genres, or nothing more specific than the catch-all."""
    return not ev["genres"] or set(ev["genres"]) <= VAGUE


def main() -> None:
    events, curated = load()
    show_all = "--all" in sys.argv
    events_only = "--events" in sys.argv

    known = {artist_key(a) for a in (curated.get("artists") or {})}
    known |= {artist_key(a) for a in (curated.get("not_electronic") or [])}

    vague = [e for e in events if is_vague(e)]
    print(f"{len(vague)} / {len(events)} events have no specific genre "
          f"({100 * len(vague) // max(len(events), 1)}%)\n")

    if not events_only:
        # Rank artists by how many vague events they would fix.
        impact: Counter[str] = Counter()
        display: dict[str, str] = {}
        for ev in vague:
            for a in ev["artists"]:
                k = artist_key(a)
                if not show_all and k in known:
                    continue
                impact[k] += 1
                display.setdefault(k, a)

        if impact:
            print("Artists worth adding to curated.json (most impact first):")
            for k, n in impact.most_common(25):
                mark = " [curated]" if k in known else ""
                print(f'  {n:3} event(s)  "{display[k]}"{mark}')
            print()
        else:
            print("No uncurated artists on vague events — nice.\n")

    print("Vague events:")
    by_day: dict[str, list[dict]] = defaultdict(list)
    for ev in vague:
        by_day[ev["date"][:10]].append(ev)
    for day in sorted(by_day):
        for ev in by_day[day]:
            artists = ", ".join(ev["artists"]) or "—"
            print(f"  {day}  {(ev['city'] or '?'):<11} {ev['title'][:44]:<44}")
            print(f"              artists: {artists[:70]}")
            print(f"              {ev['url']}")

    print("\nEdit curated.json, then re-run `python build.py`.")


if __name__ == "__main__":
    main()
