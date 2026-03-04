"""mole — human-centred assisted programming.

Language-agnostic via tree-sitter. No python ast module.

Usage:
    python -m mole myfile.py          # Interactive REPL
    python -m mole --check myfile.py  # Show holes

API:
    from mole.operations import discover, expand, fill, verify, apply
    from mole.backends import get_backend, detect_language
    from mole.fillers import get_filler
"""
from .types import (
    Hole, HoleStatus, BehaviorSpec, Expansion,
    FillerConfig, Filler, VerifyResult, MoleFile,
)
from .operations import discover, expand, diversify, fill, verify, apply, prefetch
from .backends import get_backend, detect_language, LanguageBackend
from .fillers import get_filler, FILLER_NAMES
from .context import assemble_context, DEFAULT_LAYERS
from .cache import CacheManager, get_cache, set_cache


# ─── User-facing API ─────────────────────────────────────────────────────────

def hole(description: str = "") -> any:
    """Typed hole placeholder. Write this where you want LLM-generated code.

    Usage:
        from mole import hole

        pages: list[str] = hole("fetch all pages from sitemap")
        result: dict = hole("group items by category")
        hole("write output to disk")  # bare hole (side-effect)

    The type annotation on the left side tells mole what type to generate.
    The description string tells the LLM what the code should do.
    """
    return None  # Runtime no-op — mole reads these via tree-sitter


__all__ = [
    # User API
    "hole",
    # Types
    "Hole", "HoleStatus", "BehaviorSpec", "Expansion",
    "FillerConfig", "Filler", "VerifyResult", "MoleFile",
    # Operations
    "discover", "expand", "diversify", "fill", "verify", "apply", "prefetch",
    # Cache
    "CacheManager", "get_cache", "set_cache",
    # Backends
    "get_backend", "detect_language", "LanguageBackend",
    # Fillers
    "get_filler", "FILLER_NAMES",
    # Context
    "assemble_context", "DEFAULT_LAYERS",
]
