"""mole — Content-addressable cache for LLM results.

Caches diversify, expand, and fill results to `.mole-cache/<filename>/`.
Cache key = hash of (hole description + expected type + context layers + model).

Design:
  - File-based: one JSON file per cached entry, keyed by content hash
  - Deterministic: same hole spec + context → same cache key
  - Inspectable: human-readable JSON, grouped by source file
  - Invalidation: cache entries include source hash so stale entries
    can be detected when the source file changes

Cache structure:
  .mole-cache/
    myfile.py/
      <hash>.json         — cached result (expand, diversify, or fill)
      manifest.json       — index of all cached entries for this file

Entry format:
  {
    "key": "<sha256 hex>",
    "operation": "diversify" | "expand" | "fill",
    "hole_description": "...",
    "hole_type": "...",
    "hole_line": 42,
    "model": "sonnet",
    "source_hash": "<sha256 of source at cache time>",
    "created_at": "2026-03-04T17:30:00Z",
    "result": { ... }  — operation-specific payload
  }
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .types import Expansion, Hole, VerifyResult


# ─── Cache Key ──────────────────────────────────────────────────────────────

def cache_key(
    hole: Hole,
    operation: str,
    model: str = "sonnet",
    context_hash: str = "",
    idea_hint: str = "",
) -> str:
    """Generate a deterministic cache key for a hole + operation combo.

    Components:
      - hole description (the NL intent — primary identity)
      - hole expected_type (type constraint)
      - operation name (diversify/expand/fill)
      - model name (different models → different results)
      - context_hash (hash of assembled context — captures scope/imports)
      - idea_hint (for expand with specific approach)

    Returns a hex SHA-256 digest.
    """
    # Build key material — order matters for determinism
    parts = [
        f"op={operation}",
        f"desc={hole.description}",
        f"type={hole.expected_type or ''}",
        f"model={model}",
        f"ctx={context_hash}",
    ]
    # Behavioral spec contributes to identity
    if hole.behavior.behavior:
        parts.append(f"behavior={hole.behavior.behavior}")
    if hole.behavior.requires:
        parts.append(f"requires={hole.behavior.requires}")
    if hole.behavior.ensures:
        parts.append(f"ensures={hole.behavior.ensures}")
    # Idea hint for targeted expansions
    if idea_hint:
        parts.append(f"hint={idea_hint}")

    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def source_hash(source: str) -> str:
    """Hash of source file content — used for staleness detection."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def context_hash(context_text: str) -> str:
    """Hash assembled context for cache key differentiation."""
    return hashlib.sha256(context_text.encode("utf-8")).hexdigest()[:16]


# ─── Cached Result Serialization ────────────────────────────────────────────

def _serialize_expansion(exp: Expansion) -> dict:
    """Serialize an Expansion to a JSON-safe dict."""
    return {
        "approach_name": exp.approach_name,
        "approach_description": exp.approach_description,
        "expanded_code": exp.expanded_code,
        "sub_hole_count": len(exp.sub_holes),
    }


def _deserialize_expansion(data: dict) -> Expansion:
    """Deserialize an Expansion from cached JSON."""
    return Expansion(
        approach_name=data["approach_name"],
        approach_description=data["approach_description"],
        expanded_code=data["expanded_code"],
        sub_holes=[],  # sub_holes are re-discovered from expanded_code
    )


def _serialize_fill(code: str, result: VerifyResult) -> dict:
    """Serialize a fill result to a JSON-safe dict."""
    return {
        "fill_code": code,
        "success": result.success,
        "new_errors": result.new_errors,
    }


def _serialize_diversify(expansions: list[Expansion]) -> dict:
    """Serialize a list of diversify expansions."""
    return {
        "expansions": [_serialize_expansion(e) for e in expansions],
        "count": len(expansions),
    }


def _deserialize_diversify(data: dict) -> list[Expansion]:
    """Deserialize diversify results from cached JSON."""
    return [_deserialize_expansion(e) for e in data["expansions"]]


# ─── Cache Manager ──────────────────────────────────────────────────────────

class CacheManager:
    """File-based cache for mole operations.

    Cache root defaults to `.mole-cache/` in the source file's parent directory.
    Each source file gets its own subdirectory.

    Thread-safe for reads. Writes are atomic (write-to-temp then rename).
    """

    def __init__(self, cache_root: Optional[Path] = None):
        """Initialize cache manager.

        Args:
            cache_root: Root directory for cache. If None, resolved per-file
                        to `.mole-cache/` alongside the source file.
        """
        self._cache_root = cache_root
        self._stats = CacheStats()

    @property
    def stats(self) -> "CacheStats":
        """Access cache hit/miss statistics."""
        return self._stats

    def _cache_dir(self, source_path: Path) -> Path:
        """Get cache directory for a source file.

        Returns: .mole-cache/<filename>/ alongside the source file.
        """
        if self._cache_root:
            base = self._cache_root
        else:
            base = source_path.parent / ".mole-cache"
        return base / source_path.name

    def _ensure_dir(self, source_path: Path) -> Path:
        """Create cache directory if needed, return the path."""
        cache_dir = self._cache_dir(source_path)
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir

    # ─── Store Operations ────────────────────────────────────────────────

    def store_diversify(
        self,
        key: str,
        hole: Hole,
        expansions: list[Expansion],
        source_path: Path,
        current_source: str,
        model: str = "sonnet",
    ) -> None:
        """Cache diversify results."""
        entry = self._make_entry(
            key=key,
            operation="diversify",
            hole=hole,
            result=_serialize_diversify(expansions),
            current_source=current_source,
            model=model,
        )
        self._write_entry(source_path, key, entry)

    def store_expand(
        self,
        key: str,
        hole: Hole,
        expansion: Expansion,
        source_path: Path,
        current_source: str,
        model: str = "sonnet",
    ) -> None:
        """Cache a single expansion result."""
        entry = self._make_entry(
            key=key,
            operation="expand",
            hole=hole,
            result=_serialize_expansion(expansion),
            current_source=current_source,
            model=model,
        )
        self._write_entry(source_path, key, entry)

    def store_fill(
        self,
        key: str,
        hole: Hole,
        code: str,
        verify_result: VerifyResult,
        source_path: Path,
        current_source: str,
        model: str = "sonnet",
    ) -> None:
        """Cache a fill result (only cache successful fills)."""
        if not verify_result.success:
            return  # Don't cache failed fills
        entry = self._make_entry(
            key=key,
            operation="fill",
            hole=hole,
            result=_serialize_fill(code, verify_result),
            current_source=current_source,
            model=model,
        )
        self._write_entry(source_path, key, entry)

    # ─── Retrieve Operations ─────────────────────────────────────────────

    def get_diversify(
        self,
        key: str,
        source_path: Path,
        current_source: str,
    ) -> Optional[list[Expansion]]:
        """Look up cached diversify results. Returns None on miss."""
        entry = self._read_entry(source_path, key)
        if entry is None:
            self._stats.misses += 1
            return None
        # Check staleness — source changed since cache time
        if entry.get("source_hash") != source_hash(current_source):
            self._stats.stale += 1
            return None  # Source changed, results may be invalid
        self._stats.hits += 1
        return _deserialize_diversify(entry["result"])

    def get_expand(
        self,
        key: str,
        source_path: Path,
        current_source: str,
    ) -> Optional[Expansion]:
        """Look up cached expansion. Returns None on miss."""
        entry = self._read_entry(source_path, key)
        if entry is None:
            self._stats.misses += 1
            return None
        if entry.get("source_hash") != source_hash(current_source):
            self._stats.stale += 1
            return None
        self._stats.hits += 1
        return _deserialize_expansion(entry["result"])

    def get_fill(
        self,
        key: str,
        source_path: Path,
        current_source: str,
    ) -> Optional[tuple[str, VerifyResult]]:
        """Look up cached fill. Returns None on miss."""
        entry = self._read_entry(source_path, key)
        if entry is None:
            self._stats.misses += 1
            return None
        if entry.get("source_hash") != source_hash(current_source):
            self._stats.stale += 1
            return None
        self._stats.hits += 1
        result_data = entry["result"]
        vr = VerifyResult(
            success=result_data["success"],
            new_errors=result_data.get("new_errors", []),
        )
        return result_data["fill_code"], vr

    # ─── Cache Status ────────────────────────────────────────────────────

    def status(self, source_path: Path, current_source: str) -> "CacheStatus":
        """Get cache status for a source file.

        Reports: total entries, fresh entries, stale entries, entries by op type.
        """
        cache_dir = self._cache_dir(source_path)
        if not cache_dir.exists():
            return CacheStatus(path=cache_dir)

        src_hash = source_hash(current_source)
        total = 0
        fresh = 0
        stale = 0
        by_operation: dict[str, int] = {}

        for entry_file in cache_dir.glob("*.json"):
            if entry_file.name == "manifest.json":
                continue
            try:
                data = json.loads(entry_file.read_text())
                total += 1
                op = data.get("operation", "unknown")
                by_operation[op] = by_operation.get(op, 0) + 1
                if data.get("source_hash") == src_hash:
                    fresh += 1
                else:
                    stale += 1
            except (json.JSONDecodeError, OSError):
                continue

        return CacheStatus(
            path=cache_dir,
            total=total,
            fresh=fresh,
            stale=stale,
            by_operation=by_operation,
        )

    def clear(self, source_path: Path) -> int:
        """Clear all cached entries for a source file. Returns count removed."""
        cache_dir = self._cache_dir(source_path)
        if not cache_dir.exists():
            return 0
        count = 0
        for entry_file in cache_dir.glob("*.json"):
            entry_file.unlink()
            count += 1
        # Remove empty directory
        try:
            cache_dir.rmdir()
        except OSError:
            pass  # Not empty (shouldn't happen, but safe)
        return count

    def clear_stale(self, source_path: Path, current_source: str) -> int:
        """Remove only stale entries (source changed since cache time)."""
        cache_dir = self._cache_dir(source_path)
        if not cache_dir.exists():
            return 0
        src_hash = source_hash(current_source)
        count = 0
        for entry_file in cache_dir.glob("*.json"):
            if entry_file.name == "manifest.json":
                continue
            try:
                data = json.loads(entry_file.read_text())
                if data.get("source_hash") != src_hash:
                    entry_file.unlink()
                    count += 1
            except (json.JSONDecodeError, OSError):
                continue
        return count

    # ─── Internal ────────────────────────────────────────────────────────

    def _make_entry(
        self,
        key: str,
        operation: str,
        hole: Hole,
        result: dict,
        current_source: str,
        model: str,
    ) -> dict:
        """Build a cache entry dict."""
        return {
            "key": key,
            "operation": operation,
            "hole_description": hole.description,
            "hole_type": hole.expected_type,
            "hole_line": hole.line_no,
            "model": model,
            "source_hash": source_hash(current_source),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "result": result,
        }

    def _write_entry(self, source_path: Path, key: str, entry: dict) -> None:
        """Write a cache entry atomically (write-to-temp, then rename)."""
        cache_dir = self._ensure_dir(source_path)
        target = cache_dir / f"{key[:16]}.json"  # Truncate hash for filename
        tmp = target.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(entry, indent=2))
            tmp.rename(target)
        except OSError:
            # Cache write failure is non-fatal — log and continue
            try:
                tmp.unlink()
            except OSError:
                pass

    def _read_entry(self, source_path: Path, key: str) -> Optional[dict]:
        """Read a cache entry by key. Returns None if not found."""
        cache_dir = self._cache_dir(source_path)
        target = cache_dir / f"{key[:16]}.json"
        if not target.exists():
            return None
        try:
            data = json.loads(target.read_text())
            # Verify key matches (truncated filenames could collide theoretically)
            if data.get("key", "")[:16] != key[:16]:
                return None
            return data
        except (json.JSONDecodeError, OSError):
            return None


# ─── Cache Statistics ────────────────────────────────────────────────────────

@dataclass
class CacheStats:
    """Runtime cache hit/miss statistics for a session."""
    hits: int = 0
    misses: int = 0
    stale: int = 0

    @property
    def total_lookups(self) -> int:
        return self.hits + self.misses + self.stale

    @property
    def hit_rate(self) -> float:
        if self.total_lookups == 0:
            return 0.0
        return self.hits / self.total_lookups

    def summary(self) -> str:
        """Human-readable summary."""
        if self.total_lookups == 0:
            return "no cache lookups"
        return (
            f"{self.hits} hits, {self.misses} misses, {self.stale} stale "
            f"({self.hit_rate:.0%} hit rate)"
        )


@dataclass
class CacheStatus:
    """Status of cache for a specific source file."""
    path: Path
    total: int = 0
    fresh: int = 0
    stale: int = 0
    by_operation: dict[str, int] = field(default_factory=dict)

    @property
    def exists(self) -> bool:
        return self.total > 0

    def summary(self) -> str:
        """Human-readable status summary."""
        if not self.exists:
            return "no cache entries"
        parts = [f"{self.total} entries ({self.fresh} fresh, {self.stale} stale)"]
        if self.by_operation:
            ops = ", ".join(f"{k}: {v}" for k, v in sorted(self.by_operation.items()))
            parts.append(ops)
        return " · ".join(parts)


# ─── Module-Level Cache Instance ────────────────────────────────────────────

# Singleton cache manager — shared across all operations in a session.
# CLI creates this at startup and passes it through.
_default_cache: Optional[CacheManager] = None


def get_cache(cache_root: Optional[Path] = None) -> CacheManager:
    """Get or create the module-level cache manager."""
    global _default_cache
    if _default_cache is None:
        _default_cache = CacheManager(cache_root)
    return _default_cache


def set_cache(cache: Optional[CacheManager]) -> None:
    """Set the module-level cache manager (or None to disable caching)."""
    global _default_cache
    _default_cache = cache
