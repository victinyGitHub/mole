"""mole — MCP server exposing typed-hole primitives as tools.

Exposes mole's core operations (discover, context, fill, verify, apply,
expand, diversify) as MCP tools over stdio. This lets LLM agents use mole's
type-checking and context assembly as native tool calls instead of shelling
out to the CLI.

Usage:
    mole --mcp                    # start MCP server over stdio
    claude mcp add mole -- mole --mcp   # register with Claude Code

The LLM becomes the ideator — it reads context, thinks about approaches,
and uses fill/verify/apply as mechanical scaffolding. No subprocess overhead,
no parsing text output.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .types import Hole, HoleStatus, BehaviorSpec, MoleFile
from .operations import discover, expand, diversify, fill, verify, apply
from .context import assemble_context, DEFAULT_LAYERS
from .backends import get_backend, detect_language
from .fillers import get_filler
from .cache import CacheManager, get_cache, set_cache


# ─── Server Setup ──────────────────────────────────────────────────────────

mcp = FastMCP(
    "mole",
    instructions=(
        "Mole is a typed-hole-driven code generation tool. "
        "Use these tools to discover holes in source files, "
        "read the assembled context for each hole, generate "
        "type-verified fills, and apply them back to the source. "
        "The workflow is: discover → context → think → fill → verify → apply. "
        "You control the architecture; the tools handle type checking."
    ),
)

# Module-level state — set during run()
_filler = None
_cache = None


def _get_filler():
    global _filler
    if _filler is None:
        _filler = get_filler("claude")
    return _filler


def _get_cache():
    global _cache
    if _cache is None:
        _cache = CacheManager()
        set_cache(_cache)
    return _cache


def _hole_to_dict(h: Hole) -> dict:
    """Serialize a Hole to a clean JSON-friendly dict."""
    return {
        "id": h.id,
        "line": h.line_no,
        "description": h.description,
        "type": h.expected_type,
        "var_name": h.var_name,
        "status": h.status.value,
        "is_bare": h.is_bare,
        "is_return": h.is_return,
        "behavior": {
            "behavior": h.behavior.behavior,
            "requires": h.behavior.requires,
            "ensures": h.behavior.ensures,
        } if h.behavior else None,
    }


def _find_hole(path: Path, line: int) -> tuple[Hole, str]:
    """Find a hole at or near the given line number. Returns (hole, source)."""
    mole_file = discover(path)
    source = mole_file.source

    # Exact match first
    for h in mole_file.holes:
        if h.line_no == line:
            return h, source

    # Fuzzy: closest hole within 5 lines
    closest = None
    min_dist = 999
    for h in mole_file.holes:
        dist = abs(h.line_no - line)
        if dist < min_dist and dist <= 5:
            min_dist = dist
            closest = h

    if closest:
        return closest, source

    raise ValueError(
        f"No hole found at or near line {line} in {path.name}. "
        f"Holes are at lines: {[h.line_no for h in mole_file.holes]}"
    )


# ─── Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def mole_discover(file: str) -> str:
    """Find all holes in a source file.

    Returns a structured list of holes with their types, descriptions,
    behavioral specs, and line numbers. Use this first to understand
    what needs to be filled.

    Args:
        file: Path to the source file (absolute or relative).
    """
    path = Path(file).resolve()
    if not path.exists():
        return json.dumps({"error": f"File not found: {file}"})

    mole_file = discover(path)
    holes = [_hole_to_dict(h) for h in mole_file.holes]

    return json.dumps({
        "file": str(path),
        "language": mole_file.language,
        "total_holes": len(holes),
        "unfilled": len(mole_file.unfilled),
        "holes": holes,
    }, indent=2)


@mcp.tool()
def mole_context(file: str, line: int) -> str:
    """Get the full assembled context for a specific hole.

    This is what gets sent to the LLM during fill — type definitions,
    scope variables, function signatures, behavioral specs, and
    surrounding code. Read this to understand what the hole needs.

    Args:
        file: Path to the source file.
        line: Line number of the hole (1-indexed).
    """
    path = Path(file).resolve()
    hole, source = _find_hole(path, line)
    backend = get_backend(detect_language(path))

    ctx = assemble_context(hole, source, path, backend=backend)

    return json.dumps({
        "hole": _hole_to_dict(hole),
        "context": ctx,
        "context_length": len(ctx),
    }, indent=2)


@mcp.tool()
def mole_fill(file: str, line: int, code: Optional[str] = None) -> str:
    """Fill a hole with code and type-check the result.

    If 'code' is provided, it's used directly (you wrote it yourself).
    If 'code' is omitted, the LLM filler generates code based on context.

    Returns the fill code and verification result (pass/fail + errors).

    Args:
        file: Path to the source file.
        line: Line number of the hole (1-indexed).
        code: Optional code to fill the hole with. If omitted, LLM generates it.
    """
    path = Path(file).resolve()
    hole, source = _find_hole(path, line)
    backend = get_backend(detect_language(path))

    if code is not None:
        # User-provided code — just verify it
        vr = verify(hole, code, source, path, backend=backend)
        return json.dumps({
            "hole": _hole_to_dict(hole),
            "code": code,
            "verified": vr.success,
            "errors": vr.new_errors,
            "baseline_errors": vr.baseline_errors,
        }, indent=2)
    else:
        # LLM-generated fill with verify-retry loop
        filled_code, vr = fill(
            hole, source, path,
            filler=_get_filler(),
            backend=backend,
            cache=_get_cache(),
        )
        return json.dumps({
            "hole": _hole_to_dict(hole),
            "code": filled_code,
            "verified": vr.success,
            "errors": vr.new_errors,
        }, indent=2)


@mcp.tool()
def mole_verify(file: str) -> str:
    """Type-check the entire file and report errors.

    Runs pyright (Python) or tsc (TypeScript) on the file and returns
    all type errors. Use this after manual edits to check correctness.

    Args:
        file: Path to the source file.
    """
    path = Path(file).resolve()
    if not path.exists():
        return json.dumps({"error": f"File not found: {file}"})

    backend = get_backend(detect_language(path))
    source = path.read_text()
    errors = backend.verify(source, path)

    return json.dumps({
        "file": str(path),
        "success": len(errors) == 0,
        "error_count": len(errors),
        "errors": errors,
    }, indent=2)


@mcp.tool()
def mole_apply(file: str, line: int, code: str, mode: str = "fill") -> str:
    """Apply code to a hole, writing the result back to the file.

    This splices the code into the source at the hole's position,
    handles import hoisting, and writes the result to disk.

    Args:
        file: Path to the source file.
        line: Line number of the hole (1-indexed).
        code: The code to insert.
        mode: "fill" for expression substitution, "expand" for region replacement.
    """
    path = Path(file).resolve()
    hole, source = _find_hole(path, line)
    backend = get_backend(detect_language(path))

    new_source = apply(hole, code, source, path, backend=backend, mode=mode)

    # Write back to file
    path.write_text(new_source)

    # Re-verify after apply
    errors = backend.verify(new_source, path)

    return json.dumps({
        "file": str(path),
        "applied": True,
        "mode": mode,
        "hole": _hole_to_dict(hole),
        "remaining_errors": len(errors),
        "errors": errors[:5],  # first 5 only
    }, indent=2)


@mcp.tool()
def mole_expand(file: str, line: int, approach: Optional[str] = None) -> str:
    """Expand a hole into structural code with smaller sub-holes.

    Decomposes one complex hole into real, inline code with typed
    sub-holes. The result is readable code that shares parent scope.

    Args:
        file: Path to the source file.
        line: Line number of the hole (1-indexed).
        approach: Optional hint for the expansion approach (e.g. "recursive", "iterative").
    """
    path = Path(file).resolve()
    hole, source = _find_hole(path, line)

    expansion = expand(
        hole, source, path,
        filler=_get_filler(),
        idea_hint=approach,
        cache=_get_cache(),
    )

    return json.dumps({
        "hole": _hole_to_dict(hole),
        "approach": expansion.approach_name,
        "description": expansion.approach_description,
        "code": expansion.expanded_code,
        "sub_holes": [_hole_to_dict(sh) for sh in expansion.sub_holes],
    }, indent=2)


@mcp.tool()
def mole_diversify(file: str, line: int, n: int = 3) -> str:
    """Generate multiple expansion approaches for a hole.

    Returns N structurally different approaches so you can compare
    and pick the best one. Each approach has a name, description,
    and expanded code with sub-holes.

    Args:
        file: Path to the source file.
        line: Line number of the hole (1-indexed).
        n: Number of approaches to generate (default 3).
    """
    path = Path(file).resolve()
    hole, source = _find_hole(path, line)

    expansions = diversify(
        hole, source, path,
        filler=_get_filler(),
        n=n,
        cache=_get_cache(),
    )

    approaches = []
    for e in expansions:
        approaches.append({
            "name": e.approach_name,
            "description": e.approach_description,
            "code": e.expanded_code,
            "sub_holes": [_hole_to_dict(sh) for sh in e.sub_holes],
        })

    return json.dumps({
        "hole": _hole_to_dict(hole),
        "approaches": approaches,
        "count": len(approaches),
    }, indent=2)


@mcp.tool()
def mole_types(file: str, line: int) -> str:
    """Get just the type definitions visible to a specific hole.

    Returns the type context layer only — imported types, local types,
    and scope variables. Lighter than full context. Use this when you
    want to understand what types are available before writing code.

    Args:
        file: Path to the source file.
        line: Line number of the hole (1-indexed).
    """
    path = Path(file).resolve()
    hole, source = _find_hole(path, line)
    backend = get_backend(detect_language(path))

    from .context import TypeContextLayer
    type_layer = TypeContextLayer()
    type_ctx = type_layer.build(hole, source, path, backend=backend)

    return json.dumps({
        "hole": _hole_to_dict(hole),
        "types": type_ctx,
    }, indent=2)


# ─── Entry Point ───────────────────────────────────────────────────────────

def run_mcp(filler_name: str = "claude", model: Optional[str] = None):
    """Start the MCP server over stdio.

    Called by `mole --mcp` CLI flag.
    """
    global _filler, _cache

    from .types import FillerConfig
    config = FillerConfig()
    if model:
        config.model = model
    _filler = get_filler(filler_name, config)
    _cache = CacheManager()
    set_cache(_cache)

    mcp.run(transport="stdio")
