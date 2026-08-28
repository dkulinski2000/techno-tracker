"""Event scrapers. Each module exposes ``fetch() -> list[dict]``.

build.py imports the modules it wants directly; there is no registry to keep
in sync. Kept import-free here on purpose so ``python -m scrapers.stage24``
does not trigger the "already in sys.modules" runpy warning.
"""
