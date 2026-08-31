"""Plain-assert tests for the normalisation core + parsing helpers.
Run: python test_normalise.py

No pytest dependency on purpose — this is a small, flat project.
"""

from scrapers.common import find_jsonld
from scrapers.exist import _headliner, _price
from normalise import (
    curated_excluded,
    curated_genres,
    safe_url,
    split_acts,
    squash,
    tidy_genres,
    artist_key,
    canonical_city,
    classify_genres,
    clean_artists,
    in_scope,
    is_poland,
    strip_diacritics,
    to_warsaw_iso,
)

cases = 0


def check(got, want, label):
    global cases
    cases += 1
    assert got == want, f"{label}: got {got!r}, want {want!r}"


# --- diacritics: the ł/ø non-decomposing letters ---------------------------
check(strip_diacritics("Wrocław"), "Wroclaw", "barred L")
check(strip_diacritics("Shlømo"), "Shlomo", "slashed o")
check(strip_diacritics("Łódź"), "Lodz", "L and z-dot")

# --- dates: everything must land in Europe/Warsaw, tz-aware ----------------
check(to_warsaw_iso("2026-10-16T23:00:00.000Z"), "2026-10-17T01:00:00+02:00", "utc Z -> CEST")
check(to_warsaw_iso("2026-01-16T23:00:00Z"), "2026-01-17T00:00:00+01:00", "utc Z -> CET")
check(to_warsaw_iso("2026-05-23 22:00"), "2026-05-23T22:00:00+02:00", "naive -> assumed Warsaw")
check(to_warsaw_iso("23.05.2026 / 22:00"), "2026-05-23T22:00:00+02:00", "dotted PL format")
check(to_warsaw_iso("nonsense"), None, "unparsable -> None")

# --- cities: canonical set + tri-city fold + junk cleanup ------------------
check(canonical_city("WROCŁAW"), "Wrocław", "allcaps -> canonical")
check(canonical_city("warsaw"), "Warszawa", "english -> canonical")
check(canonical_city("Sopot"), "Trójmiasto", "tri-city fold")
check(canonical_city("61- 714 Poznań"), "Poznań", "postal prefix stripped")
check(canonical_city("TYCHY"), "Tychy", "unknown allcaps -> title case")
check(canonical_city(None), None, "none -> none")

check(is_poland("Warszawa"), True, "PL city")
check(is_poland("Szwecja"), False, "country name -> not PL")
check(is_poland(None), True, "unknown -> keep")

# --- genres: specific wins, tags contribute, catch-all drops --------------
check(classify_genres("HARD NIGHT WAREHOUSE KRK"), ["rave"], "warehouse -> rave, no false 'house'")
check(classify_genres("Kollektiv Turmstrasse live"), [], "no keyword -> empty")
check("hard-techno" in classify_genres("Perc pres. Hard Techno Assault"), True, "hard techno label")
check(classify_genres("Jassmine", tags=["Jazz", "Techno"]), ["techno"], "tag -> genre")
check(classify_genres("night", tags=["ELEKTRO_I_TECHNO"]), ["electronic"], "stage24 category tag")
check("electronic" not in classify_genres("acid rave", tags=["Elektronika"]), True, "catch-all dropped when specific present")

check(in_scope(["electronic"]), True, "electronic is in scope")
check(in_scope(["jazz"]), False, "jazz not in scope")

# --- artists: junk + venue-as-act removal, fuzzy key ---------------------
check(split_acts("MIKASO b2b BOBAIO"), ["MIKASO", "BOBAIO"], "split b2b")
check(split_acts("> SANTØS [NL] > IKKHI [AR]"), ["SANTØS [NL]", "IKKHI [AR]"], "split > chain")
check(split_acts("A vs. B"), ["A", "B"], "split vs.")
check(split_acts("Frankey & Sandrino"), ["Frankey & Sandrino"], "& is one act, not two")
check(split_acts("Catz 'n Dogz, VJ Emiko"), ["Catz 'n Dogz, VJ Emiko"], "comma never splits")
check(split_acts(""), [], "empty -> empty")

check(clean_artists(["A b2b B", "B"]), ["A", "B"], "split then dedupe")
check(clean_artists(["Voucher | Klub", "Shlømo"], venue="Klub"), ["Shlømo"], "drop voucher")
check(clean_artists(["Ciało"], venue="Ciało"), [], "drop venue-as-act")
check(artist_key("Shlømo") == artist_key("Shl0mo") == artist_key("Shlomo"), True, "fuzzy artist key")

# --- JSON-LD extraction: the three shapes real sites ship -----------------
_BARE = '<script type="application/ld+json">{"@type":"MusicEvent","name":"A"}</script>'
_LIST = '<script type="application/ld+json">[{"@type":"Organization"},{"@type":"MusicEvent","name":"B"}]</script>'
_GRAPH = '<script type="application/ld+json">{"@graph":[{"@type":"MusicEvent","name":"C"}]}</script>'
_BAD = '<script type="application/ld+json">{not json,,}</script>' + _BARE

check(find_jsonld(_BARE, "MusicEvent")["name"], "A", "bare object")
check(find_jsonld(_LIST, "MusicEvent")["name"], "B", "list, skips wrong @type")
check(find_jsonld(_GRAPH, "MusicEvent")["name"], "C", "@graph wrapper")
check(find_jsonld(_BAD, "MusicEvent")["name"], "A", "malformed block skipped, not raised")
check(find_jsonld(_BARE, "Recipe"), None, "no match -> None")
check(find_jsonld("<p>no scripts</p>", "MusicEvent"), None, "no JSON-LD -> None")

# --- exist.pl helpers ----------------------------------------------------
check(_headliner("EXIST pres. HOLY PRIEST"), ["HOLY PRIEST"], "headliner after pres.")
check(_headliner("EXIST presents Charlotte De Witte "), ["Charlotte De Witte"], "presents variant")
check(_headliner("BOILER ROOM WARSAW 2026"), [], "no pres. -> no artist")

check(_price({"lowPrice": "150", "priceCurrency": "PLN"}), 150.0, "lowPrice string")
check(_price({"price": "99.50", "priceCurrency": "PLN"}), 99.5, "price fallback")
check(_price({"lowPrice": "0", "priceCurrency": "PLN"}), None, "zero -> unknown")
check(_price({"lowPrice": "20", "priceCurrency": "EUR"}), None, "non-PLN ignored")
check(_price({}), None, "no offers -> None")

# --- curated knowledge layer (curated.json) ------------------------------
CUR = {
    "artists": {
        "holy priest": ["hard-techno", "hardcore"],
        "Shlømo": ["techno"],
    },
    "not_electronic": ["jmsn", "son lux"],
}

check(curated_genres(["HOLY PRIEST"], "EXIST pres. HOLY PRIEST", CUR),
      ["hard-techno", "hardcore"], "curated genres via artists list")
check(curated_genres([], "EXIST pres. HOLY PRIEST", CUR),
      ["hard-techno", "hardcore"], "curated genres via title match")
check(curated_genres(["Shlomo"], "some night", CUR), ["techno"],
      "curated artist matched without diacritics")
check(curated_genres(["Nobody"], "quiet night", CUR), [], "no curated match")

check(curated_excluded(["JMSN"], "JMSN | Kraków", CUR), True, "excluded artist")
check(curated_excluded([], "Son Lux | Poznań", CUR), True, "excluded via title")
check(curated_excluded(["HOLY PRIEST", "JMSN"], "split bill", CUR), False,
      "in-scope artist on the bill rescues the event")
check(curated_excluded(["Nobody"], "quiet night", CUR), False, "not excluded")
check(curated_excluded(["JMSN"], "JMSN", {}), False, "empty curated -> never excludes")

# vague catch-all is dropped only when something specific survives
check(tidy_genres(["electronic", "techno"]), ["techno"], "specific beats vague")
check(tidy_genres(["electronic"]), ["electronic"], "vague alone is kept")
check(tidy_genres(["techno", "techno", "house"]), ["techno", "house"], "dedupes")
check(tidy_genres([]), [], "empty stays empty")

# --- safe_url: the XSS boundary ------------------------------------------
check(safe_url("https://stage24.pl/x"), "https://stage24.pl/x", "https allowed")
check(safe_url("http://exist.pl/y"), "http://exist.pl/y", "http allowed")
check(safe_url("HTTPS://EXIST.PL/Z"), "HTTPS://EXIST.PL/Z", "scheme is case-insensitive")
check(safe_url("javascript:alert(1)"), None, "javascript: blocked")
check(safe_url("java\tscript:alert(1)"), None, "control chars cannot smuggle a scheme")
check(safe_url(" javascript:alert(1) "), None, "leading whitespace does not help")
check(safe_url("data:text/html,<script>x</script>"), None, "data: blocked")
check(safe_url("//evil.example/x"), None, "protocol-relative blocked")
check(safe_url("https:///evil"), None, "empty host blocked")
check(safe_url("https://\\evil.example"), None, "backslash host blocked")
check(safe_url("/relative/path"), None, "relative blocked")
check(safe_url(None), None, "none -> none")
check(safe_url(""), None, "empty -> none")

# --- squash: the dedup anchor --------------------------------------------
# The pairs this exists for: same venue, two sellers, different punctuation.
check(squash("Energy 2000 Przytkowice"), squash("ENERGY2000 - PRZYTKOWICE"),
      "spacing and dashes do not split a venue")
check(squash("BARdzo bardzo"), squash("BARdzo, bardzo"), "a comma does not split a venue")
check(squash("Klub Muzyczny B17"), "klubmuzycznyb17", "alphanumerics survive")
check(squash("Wrocław"), "wroclaw", "diacritics still fold")
check(squash("  Progresja  "), "progresja", "surrounding whitespace goes")
# ...and the merge it must NOT make: two real, different venues.
check(squash("ERGO ARENA") == squash("ERGO ARENA Sopot/Gdańsk"), False,
      "containment is not equality — no prefix merging")
check(squash(""), "", "empty stays empty")

print(f"ok — {cases} checks passed")
