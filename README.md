# Techno Radar

Aggregates electronic-music events (techno, hard techno, schranz, acid,
industrial and neighbours) happening **in Poland**, into one static page with
client-side filters by city, date, genre, price and lineup artist.

No backend, no database. Python scrapers → one `events.json` → a vanilla-JS page
on GitHub Pages. A GitHub Actions cron refreshes the data twice a day.

See [`CLAUDE.md`](CLAUDE.md) for the full design rationale and the build log at
the bottom of that file.

## Layout

```
scrapers/
  common.py      shared: UA, rate limiter, raw-response dump, JSON-LD reader
  stage24.py     stage24.pl  — via its NestJS API (api.stage24.pl)
  going.py       goingapp.pl — via its public Algolia search index
  exist.py       exist.pl    — Webflow site, via schema.org JSON-LD
normalise.py     pure helpers: dates, cities, genre classification, artists
build.py         run scrapers → normalise → classify → dedup → docs/events.json
curated.json     hand-curated artist→genre knowledge + per-event overrides
curate.py        report: which events still need a human genre call
diagnose.py      recon tool: run against any new source before writing a scraper
docs/
  index.html     the site (fetches events.json, filters in the browser)
  events.json     generated — the only data file
test_normalise.py  plain-assert tests for the normalisation core
raw/             gitignored: saved responses + stage24 detail cache
```

## Run it

```sh
python -m pip install -r requirements.txt

python test_normalise.py          # fast, no network
python -m scrapers.stage24 --fast # smoke-test one source to the terminal
python -m scrapers.going
python -m scrapers.exist
python build.py                   # writes docs/events.json
python curate.py                  # which events still need a genre call

python -m http.server -d docs 8000   # then open http://localhost:8000
```

## Fixing a genre label

The keyword matcher can only find genres that are actually written down, and
most event pages never name a subgenre — "EXIST pres. HOLY PRIEST" says only
"electronic music experiences". In this scene the **lineup** implies the genre,
so that knowledge lives in `curated.json`, which you edit by hand.

```sh
python curate.py       # what still needs a call, ranked by how many events it fixes
# edit curated.json
python build.py        # re-run; no code changes needed
```

Three sections, in increasing order of bluntness:

```jsonc
{
  // genres ADDED to whatever the text matcher found.
  // matches the artists list OR the name appearing in the title,
  // fuzzily (Shlømo == Shlomo == Shl0mo)
  "artists": { "holy priest": ["hard-techno", "hardcore"] },

  // acts that simply aren't this scene. an event whose only
  // recognisable act is one of these is dropped
  "not_electronic": ["jmsn", "son lux"],

  // final say for one event, keyed by ticket URL (copy it from the app).
  // REPLACES all detected genres; use [] to drop the event
  "events": { "https://…": { "genres": ["schranz"] } }
}
```

`build.py` warns about unknown genre labels (typos) rather than failing.
Valid labels are in `normalise.KNOWN_GENRES`.

## Adding a source

1. `python diagnose.py https://newsite.pl/` — reports status, size, anti-bot
   markers, embedded-JSON hints and `robots.txt`. Raw body saved to `raw/`.
2. Find the JSON API in DevTools → Network. Only fall back to HTML parsing
   (`selectolax`, semantic attributes only) or Playwright if there is no API.
3. New `scrapers/<name>.py` exposing `fetch() -> list[dict]` in the record shape
   documented in `CLAUDE.md`. Never raise on one bad record — log, skip, go on.
4. Add the module to `SOURCES` in `build.py`.

## Deploy

Push to GitHub, enable Pages on the `docs/` folder. The `scrape` workflow
(`.github/workflows/scrape.yml`) runs twice daily and on manual dispatch;
it commits `docs/events.json` only when it changed.
