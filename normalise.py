"""Pure normalisation helpers for build.py — no I/O, easy to test.

Covers the four messy fields: date, city, genre, artist. Everything here is
deterministic; the genre pass is keyword matching, per CLAUDE.md
("structure is a job for code, semantics is a job for an LLM").
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")


# --------------------------------------------------------------------------- #
# text helpers
# --------------------------------------------------------------------------- #
# Letters that do NOT decompose under NFKD and must be mapped explicitly.
# ł/Ł (Polish barred L) is the big one; the rest show up in DJ names.
_TRANSLIT = str.maketrans({
    "ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D",
    "ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ð": "d", "þ": "th",
})


def strip_diacritics(text: str) -> str:
    text = (text or "").translate(_TRANSLIT)
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def fold(text: str) -> str:
    """Lowercase + diacritic-free, for matching only (never for display)."""
    return strip_diacritics(text or "").lower().strip()


def clean_ws(text: str | None) -> str | None:
    if not text:
        return None
    out = re.sub(r"\s+", " ", text).strip()
    return out or None


def safe_url(url: str | None) -> str | None:
    """Return the URL only if it is a plain http(s) link, else None.

    Security boundary. `url` can come straight from a third party — exist.pl
    reads it out of the page's own JSON-LD — and it ends up in an `href` in
    docs/index.html. HTML-escaping stops an attacker breaking *out* of the
    attribute but does nothing about the scheme: `javascript:...` survives
    escaping untouched and runs when the visitor clicks "Bilety". So the
    scheme is allow-listed here, at the one point every source passes through.
    """
    url = clean_ws(url)
    if not url:
        return None
    # Strip control characters first: "java\tscript:x" is treated as
    # "javascript:x" by browsers but would sneak past a naive prefix check.
    cleaned = re.sub(r"[\x00-\x20]", "", url)
    if not re.match(r"(?i)^https?://[^/\\]", cleaned):
        return None
    return url


# --------------------------------------------------------------------------- #
# dates
# --------------------------------------------------------------------------- #
def to_warsaw_iso(value: str) -> str | None:
    """Parse an ISO-ish datetime and return tz-aware ISO 8601 in Europe/Warsaw.

    Handles trailing 'Z', explicit offsets, and naive strings (assumed Warsaw).
    Returns None if it cannot be parsed — the caller drops the record.
    """
    if not value:
        return None
    raw = value.strip().replace("Z", "+00:00")
    # Some feeds give milliseconds with a 'Z' already swapped -> fromisoformat ok.
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        # Last-ditch: "YYYY-MM-DD HH:MM" or "DD.MM.YYYY / HH:MM"
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", raw)
        if not m:
            m2 = re.search(r"(\d{2})\.(\d{2})\.(\d{4}).{0,4}(\d{2}):(\d{2})", raw)
            if not m2:
                return None
            d, mo, y, h, mi = m2.groups()
            dt = datetime(int(y), int(mo), int(d), int(h), int(mi))
        else:
            y, mo, d, h, mi = m.groups()
            dt = datetime(int(y), int(mo), int(d), int(h), int(mi))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=WARSAW)
    return dt.astimezone(WARSAW).isoformat()


# --------------------------------------------------------------------------- #
# cities
# --------------------------------------------------------------------------- #
# Canonical display names for the set CLAUDE.md pins. Trójmiasto folds the
# tri-city; everything else outside the map is passed through title-cased with
# diacritics preserved.
_CITY_CANON = {
    "warszawa": "Warszawa",
    "warsaw": "Warszawa",
    "krakow": "Kraków",
    "cracow": "Kraków",
    "wroclaw": "Wrocław",
    "poznan": "Poznań",
    "lodz": "Łódź",
    "katowice": "Katowice",
    "szczecin": "Szczecin",
    "gdansk": "Trójmiasto",
    "gdynia": "Trójmiasto",
    "sopot": "Trójmiasto",
    "trojmiasto": "Trójmiasto",
}


# Countries / foreign cities that Polish ticketing sites occasionally list.
# Not exhaustive — a deny list, since the sources are Polish by default.
_NOT_POLAND = {
    "szwecja", "sweden", "niemcy", "germany", "berlin", "hamburg", "monachium",
    "czechy", "czech republic", "praga", "prague", "brno", "ostrawa",
    "slowacja", "slovakia", "bratyslawa", "wieden", "vienna", "wien", "austria",
    "ukraina", "ukraine", "kijow", "lwow", "lviv", "wielka brytania", "london",
    "londyn", "amsterdam", "holandia", "netherlands", "belgia", "bruksela",
    "francja", "france", "paryz", "paris", "hiszpania", "spain", "barcelona",
    "madryt", "wlochy", "italy", "rzym", "mediolan", "wegry", "budapeszt",
    "litwa", "wilno", "lotwa", "ryga", "dania", "kopenhaga",
}


def _tidy_city(name: str) -> str:
    # drop leading postal codes ("61- 714 Poznań"), trailing glued noise
    name = re.sub(r"^\s*\d[\d\s\-]*", "", name)
    name = re.split(r"\s[|/(]", name)[0]
    return clean_ws(name) or name


def is_poland(city: str | None) -> bool:
    """False only when the city clearly names a place outside Poland."""
    if not city:
        return True  # unknown — sources are Polish, keep it
    return fold(city) not in _NOT_POLAND


def canonical_city(name: str | None) -> str | None:
    name = _tidy_city(clean_ws(name) or "")
    if not name:
        return None
    key = fold(name)
    if key in _CITY_CANON:
        return _CITY_CANON[key]
    key = re.split(r"[|/(]", key)[0].strip()
    if key in _CITY_CANON:
        return _CITY_CANON[key]
    # Unknown city: keep it, presented nicely (ALLCAPS -> Title Case).
    return name.title() if name.isupper() else name


# --------------------------------------------------------------------------- #
# genres
# --------------------------------------------------------------------------- #
# Ordered: more specific labels first so "hard techno" wins over "techno".
# Needles are matched with word boundaries (no letter either side) so "house"
# does not fire inside "warehouse". They are fold()ed before matching.
_GENRE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("hard-techno", ("hard techno", "hardtechno", "hard-techno", "harde techno")),
    ("schranz", ("schranz",)),
    ("acid", ("acid",)),
    ("industrial", ("industrial", "ebm")),
    ("hardstyle", ("hardstyle", "rawstyle")),
    ("hardcore", ("hardcore", "gabber", "uptempo", "frenchcore", "terrorcore")),
    ("psytrance", ("psytrance", "psy-trance", "psy trance", "goa", "psychedelic trance")),
    ("trance", ("trance", "tranceformations", "euforia dzwieku", "tranceformation")),
    ("drum-and-bass", ("drum and bass", "drum'n'bass", "drum n bass", "dnb", "d&b", "jungle", "neurofunk", "liquid funk")),
    ("bounce", ("bounce", "makina")),
    ("techno", ("techno",)),
    ("house", ("house",)),
    ("electro", ("electro", "electroclash")),
    ("rave", ("rave", "warehouse", "afterparty", "all night long", "till late")),
    ("electronic", ("elektronika", "elektroniczna", "electronic", "muzyka basowa", "bass music", "club night")),
]

_GENRE_PATTERNS: list[tuple[str, list[re.Pattern]]] = [
    (label, [re.compile(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])") for n in needles])
    for label, needles in _GENRE_RULES
]

# Source-assigned tags/categories -> genre label. Applied on top of text
# matching. Keys are fold()ed with underscores turned to spaces, so GOING's
# "Muzyka basowa" and stage24's "ELEKTRO_I_TECHNO" both land here.
_TAG_TO_GENRE = {
    "techno": "techno",
    "house": "house",
    "industrial": "industrial",
    "muzyka basowa": "electronic",
    "elektronika": "electronic",
    "trance": "trance",
    "elektro i techno": "electronic",  # stage24's only electronic bucket
    "drum and bass": "drum-and-bass",
}

# Genres that qualify an event as "in scope" for this aggregator.
IN_SCOPE = {
    "techno", "hard-techno", "schranz", "acid", "industrial", "rave",
    "hardstyle", "hardcore", "trance", "psytrance", "drum-and-bass",
    "house", "electro", "bounce", "electronic",
}

# Every label this module can emit — used to validate curated.json for typos.
KNOWN_GENRES = {label for label, _ in _GENRE_RULES} | set(_TAG_TO_GENRE.values())

# Labels too vague to be a useful answer on their own. An event carrying only
# these is what `curate.py` reports as needing attention.
VAGUE = {"electronic"}


def tidy_genres(found: list[str]) -> list[str]:
    """Dedupe, and drop the vague catch-all once something specific is known."""
    out = list(dict.fromkeys(found))
    if len(out) > 1:
        specific = [g for g in out if g not in VAGUE]
        if specific:
            return specific
    return out


def classify_genres(*texts: str, tags: list[str] | None = None) -> list[str]:
    """Return an ordered, deduplicated genre list from free text + source tags."""
    haystack = fold(" \n ".join(t for t in texts if t))
    found: list[str] = []
    for label, patterns in _GENRE_PATTERNS:
        if any(p.search(haystack) for p in patterns):
            found.append(label)
    for tag in tags or []:
        label = _TAG_TO_GENRE.get(fold(tag).replace("_", " "))
        if label and label not in found:
            found.append(label)
    return tidy_genres(found)


def in_scope(genres: list[str]) -> bool:
    return any(g in IN_SCOPE for g in genres)


# --------------------------------------------------------------------------- #
# artists
# --------------------------------------------------------------------------- #
_ARTIST_JUNK = ("voucher", "bilet", "vouchery", "karnet", "wejsciowka", "wejściówka")

# Sources sometimes cram a whole lineup into one artist string, e.g. stage24's
# "> SANTØS [NL] > IKKHI [AR] > MIKASO b2b BOBAIO". These separators are
# unambiguous in a DJ context, so splitting on them is structure, not guesswork.
# Comma is deliberately NOT included — too risky, and no source actually uses it.
_ACT_SEPARATORS = re.compile(r"\s*(?:>|\||\bb2b\b|\bvs\b\.?)\s*", re.IGNORECASE)


def split_acts(name: str) -> list[str]:
    """"A b2b B" -> ["A", "B"]. Returns [name] when there is nothing to split."""
    parts = [p.strip(" \t-–—:") for p in _ACT_SEPARATORS.split(name or "")]
    parts = [p for p in parts if p]
    return parts or ([name] if name else [])


def clean_artists(artists: list[str] | None, *, venue: str | None = None,
                  title: str | None = None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    venue_fold = fold(venue or "")
    for raw in artists or []:
        for a in split_acts(raw):
            a = clean_ws(a)
            if not a:
                continue
            af = fold(a)
            if af in seen:
                continue
            if any(j in af for j in _ARTIST_JUNK):
                continue
            if venue_fold and af == venue_fold:  # venue listed as its own act
                continue
            seen.add(af)
            out.append(a)
    return out


def artist_key(name: str) -> str:
    """Fuzzy-match key: Shlømo / Shlomo / Shl0mo -> the same string."""
    k = fold(name)
    k = k.replace("0", "o").replace("$", "s").replace("1", "i")
    return re.sub(r"[^a-z0-9]+", "", k)


# --------------------------------------------------------------------------- #
# curated knowledge (curated.json)
# --------------------------------------------------------------------------- #
def _mentions(name: str, artists: list[str], title: str) -> bool:
    """Is `name` one of the event's artists, or named in its title?

    Artists match on `artist_key` (fuzzy). The title is matched on word
    boundaries so a short name like "STARA" cannot fire inside another word.
    """
    key = artist_key(name)
    if not key:
        return False
    if any(artist_key(a) == key for a in artists):
        return True
    folded = fold(name)
    if len(folded) < 3:  # too short to risk a title substring match
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(folded)}(?![a-z0-9])", fold(title)) is not None


def curated_genres(artists: list[str], title: str, curated: dict) -> list[str]:
    """Extra genres contributed by curated artist knowledge."""
    out: list[str] = []
    for name, genres in (curated.get("artists") or {}).items():
        if _mentions(name, artists, title):
            out.extend(genres)
    return out


def curated_excluded(artists: list[str], title: str, curated: dict) -> bool:
    """True if the only recognisable act here is one we've marked out of scope."""
    names = curated.get("not_electronic") or []
    if not names:
        return False
    hits = [n for n in names if _mentions(n, artists, title)]
    if not hits:
        return False
    # If a curated in-scope artist is also on the bill, keep the event.
    return not curated_genres(artists, title, curated)
