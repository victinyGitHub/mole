"""Playground — try the mole REPL on this file.

A small URL shortener with 3 holes to explore.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import hashlib


# concrete types — no holes here
@dataclass
class ShortURL:
    """A shortened URL mapping."""
    code: str          # the short code (e.g. "a3f8b2")
    original: str      # the full original URL
    hits: int = 0      # access counter


@dataclass
class URLStore:
    """In-memory URL store."""
    urls: dict[str, ShortURL]  # code -> ShortURL mapping


from mole import hole


# ─── Functions with holes ────────────────────────────────────────────────────

def generate_code(url: str, length: int = 6) -> str:
    """Generate a short code from a URL."""
    # @mole:behavior hash the URL with sha256 and take the first `length`
    #   hex characters as the short code
    # @mole:ensures returns a string of exactly `length` characters
    # @mole:filled-by v3@2026-03-04
    # approach: sha256-hex-prefix
    # @mole:behavior compute sha256 hex digest of the url bytes
    # @mole:filled-by v3@2026-03-04
    # approach: direct-hashlib-inline
    digest: str = hashlib.sha256(url.encode()).hexdigest()
    # @mole:behavior slice the first `length` characters from the digest
    code: str = digest[:length]
    return code


def shorten(store: URLStore, url: str) -> ShortURL:
    """Shorten a URL, reusing existing code if already stored."""
    # @mole:behavior check if URL already exists in store (by scanning
    #   values). if found, return existing. otherwise generate new code,
    #   create ShortURL, add to store, and return it.
    # @mole:requires url is a non-empty string
    # @mole:ensures returned ShortURL.original == url
    # @mole:filled-by v3@2026-03-04
    # approach: hash-based dedup (same url → same hash → same code, no scan needed)
    code: str = generate_code(url)
    # @mole:behavior check if code already exists in store and return it if so
    # @mole:filled-by v3@2026-03-04
    # approach: dict.get direct return
    existing: Optional[ShortURL] = store.urls.get(code)
    if existing is not None:
        result: ShortURL = existing
    else:
        # @mole:behavior construct a new ShortURL with the generated code and original url
        # @mole:filled-by v3@2026-03-04
        # approach: direct constructor call
        new_entry: ShortURL = ShortURL(code=code, original=url)
        # @mole:behavior persist new_entry into store.urls keyed by code
        # @mole:filled-by v3@2026-03-04
        # approach: direct dictionary assignment
        store.urls[code] = new_entry
        result: ShortURL = new_entry
    return result


def lookup(store: URLStore, code: str) -> Optional[ShortURL]:
    """Look up a short code, incrementing hit counter if found."""
    # @mole:behavior look up code in store. if found, increment hits
    #   and return the ShortURL. if not found, return None.
    # @mole:ensures if found, hits is incremented by exactly 1
    # @mole:filled-by v3@2026-03-04
    # approach: direct dict lookup with in-place hit increment
    # @mole:behavior look up the short code in the store's url dict
    url_entry: Optional[ShortURL] = hole("get ShortURL by code from store.urls, or None if missing")
    if url_entry is not None:
        # @mole:behavior increment hits counter on the found ShortURL by 1
        hole("increment url_entry.hits by 1")
    found: Optional[ShortURL] = url_entry
    return found