"""mole — Composable context assembly.

The prompt sent to the LLM is assembled from modular layers.
Each layer extracts one kind of information via the language backend
and formats it as a prompt fragment. Layers are independently toggleable.

The filler sees ONLY the assembled prompt string. It knows nothing about
holes, types, or context. This is the separation of concerns.

4 layers:
  1. Types    — type annotations, imported types, scope variables
  2. Symbols  — function signatures, constants
  3. Behavior — @mole: behavioral specs
  4. Code     — enclosing block, sibling holes, indent style
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .types import Hole, FunctionHeader, ContextLayer
from .backends import LanguageBackend, get_backend, detect_language


# ─── Type Compatibility Scoring ───────────────────────────────────────────────
#
# Used by TypeContextLayer for scope variable ranking. Based on OOPSLA 2024
# ChatLSP: providing type-compatible context improves fill quality 3x.
#
# Function header ranking uses FunctionHeader.type_relevance_score() instead —
# it has parsed return_type/param_types and doesn't need regex extraction.

def _type_compat_score(candidate_type: str, expected_type: str) -> int:
    """Score type compatibility between candidate and expected types.

    Returns:
        3 — exact match (normalized, Optional-unwrapped)
        2 — base type match (same container) or substring overlap
        1 — significant shared type name tokens
        0 — no meaningful connection
    """
    c = candidate_type.strip().lower()
    e = expected_type.strip().lower()

    if not c or not e:
        return 0

    # Exact match
    if c == e:
        return 3

    # Unwrap Optional[X] → X for comparison
    def _unwrap_optional(t: str) -> str:
        m = re.match(r'^optional\[(.+)\]$', t.strip())
        return m.group(1).strip() if m else t

    c_inner = _unwrap_optional(c)
    e_inner = _unwrap_optional(e)

    if c_inner == e_inner:
        return 3  # Same after Optional-unwrap

    # Base type (container kind): list[str] → list, dict[str, int] → dict
    def _base_type(t: str) -> str:
        bracket = t.find('[')
        return t[:bracket].strip() if bracket != -1 else t

    c_base = _base_type(c_inner)
    e_base = _base_type(e_inner)

    # Same container kind (non-trivial types only)
    TRIVIAL_BASES = {'any', 'unknown', 'object', 'never', 'void', ''}
    if c_base == e_base and c_base not in TRIVIAL_BASES:
        return 2

    # One is a substring of the other (catches Optional[T] vs T, list[X] vs X, etc.)
    if e_inner in c_inner or c_inner in e_inner:
        return 2

    # Significant shared tokens: "User" in both "list[User]" and "dict[str, User]"
    GENERIC_TOKENS = {
        'list', 'dict', 'set', 'tuple', 'optional', 'union', 'any',
        'str', 'int', 'float', 'bool', 'none', 'bytes', 'type',
        'sequence', 'mapping', 'iterable', 'callable', 'generator',
        'iterator', 'awaitable', 'coroutine', 'asynciterator',
        # TypeScript generics
        'array', 'record', 'promise', 'readonly', 'partial', 'required',
        'string', 'number', 'boolean', 'unknown', 'object', 'never', 'void',
    }
    c_tokens = set(re.findall(r'[a-z][a-z0-9]*', c_inner))
    e_tokens = set(re.findall(r'[a-z][a-z0-9]*', e_inner))
    significant_c = c_tokens - GENERIC_TOKENS
    significant_e = e_tokens - GENERIC_TOKENS

    if significant_c and significant_e and significant_c & significant_e:
        return 1

    return 0


# ─── Layer 1: Type Context ────────────────────────────────────────────────────

class TypeContextLayer:
    """Type annotations, imported types, scope variable types.

    This is the minimum context needed for a fill to type-check.
    At spec completeness Level 2 (name-completeness), this includes
    full interface/struct definitions so the LLM can't hallucinate
    property names.
    """
    name = "types"

    def build(
        self,
        hole_target: Hole,
        source: str,
        path: Path,
        backend: Optional[LanguageBackend] = None,
    ) -> str:
        # Auto-detect backend if not provided
        if backend is None:
            backend = get_backend(detect_language(path))

        parts: list[str] = []

        # Hole's own type annotation
        if hole_target.expected_type:
            kind = "return" if hole_target.is_return else (
                "bare" if hole_target.is_bare else "assignment"
            )
            parts.append(f"Expected type: {hole_target.expected_type}")
            if hole_target.var_name:
                parts.append(f"Variable: {hole_target.var_name}: {hole_target.expected_type}")
            parts.append(f"Hole kind: {kind}")

        # Type definitions from this file
        type_defs = backend.extract_type_definitions(source)
        if type_defs:
            parts.append(f"TYPE DEFINITIONS (this file):\n{type_defs}")

        # Transitively resolved types from imports (up to depth 3)
        cross_types = self._resolve_cross_file_types(
            source, path, backend, max_depth=3,
        )
        if cross_types:
            parts.append(f"IMPORTED TYPE DEFINITIONS:\n{cross_types}")

        # Scope variables at hole location — filtered by type compatibility
        scope_vars = backend.extract_scope_vars(source, hole_target.line_no)
        if scope_vars:
            ranked_vars = self.retrieve_relevant_scope_vars(
                scope_vars, hole_target.expected_type
            )
            var_lines = [f"  {name}: {typ}" for name, typ in ranked_vars]
            parts.append("VARIABLES IN SCOPE:\n" + "\n".join(var_lines))

        # Import statements (so LLM knows what's available)
        imports = backend.extract_imports(source)
        if imports:
            imp_lines = []
            for module, names in imports:
                if names:
                    imp_lines.append(f"  from {module} import {names}")
                else:
                    imp_lines.append(f"  import {module}")
            parts.append("IMPORTS:\n" + "\n".join(imp_lines))

        return "\n\n".join(parts)

    def retrieve_relevant_scope_vars(
        self,
        scope_vars: list[tuple[str, str]],
        expected_type: Optional[str],
        max_vars: int = 10,
    ) -> list[tuple[str, str]]:
        """Filter and rank scope variables by type compatibility with expected_type.

        Companion to SymbolContextLayer.retrieve_relevant_headers.
        Variables whose types are compatible with the hole's expected type are
        ranked first — reduces noise and keeps the prompt focused.

        Scoring:
          - _type_compat_score(var_type, expected_type) — 0-3 pts
          - +1 pt if variable name contains the expected base type name
            (e.g., "users" is relevant when expecting "list[User]")

        If expected_type is None or there are few variables, returns all unchanged.

        Args:
            scope_vars: List of (name, type) tuples from the backend.
            expected_type: The hole's expected type annotation (may be None).
            max_vars: Maximum number of variables to include in output.

        Returns:
            Ranked list of (name, type) tuples, relevant first.
        """
        if not expected_type or not scope_vars:
            return scope_vars

        # Short list: no filtering needed
        if len(scope_vars) <= max_vars:
            # Still sort by relevance, but keep all
            max_vars = len(scope_vars)

        expected_base = expected_type.split('[')[0].strip().lower()

        scored: list[tuple[int, tuple[str, str]]] = []
        for var_name, var_type in scope_vars:
            score = _type_compat_score(var_type, expected_type)

            # Bonus: variable name hints at the type
            # e.g. "users" when expected is "list[User]", "user_map" when "dict[str, User]"
            if expected_base and len(expected_base) > 3:
                if expected_base in var_name.lower():
                    score += 1

            scored.append((score, (var_name, var_type)))

        # Stable sort: higher score first, original order preserved for ties
        scored.sort(key=lambda x: -x[0])

        # Separate relevant (score > 0) from others
        relevant = [var for sc, var in scored if sc > 0]
        others = [var for sc, var in scored if sc == 0]

        # Build output: all relevant first, then fill up to max_vars with others
        result = relevant[:max_vars]
        remaining = max_vars - len(result)
        if remaining > 0:
            result.extend(others[:remaining])

        return result

    def _resolve_cross_file_types(
        self,
        source: str,
        path: Path,
        backend: LanguageBackend,
        max_depth: int = 3,
        _visited: Optional[set[str]] = None,
    ) -> str:
        """Transitively resolve type definitions from imported modules.

        Follows import chains up to max_depth with cycle detection.
        """
        if _visited is None:
            _visited = set()
        if max_depth <= 0:
            return ""

        # Prevent cycles
        path_key = str(path.resolve())
        if path_key in _visited:
            return ""
        _visited.add(path_key)

        base_dir = path.parent
        imports = backend.extract_imports(source)
        all_type_defs: list[str] = []

        for module, _names in imports:
            # Resolve import to file path
            resolved = backend.resolve_import_path(module, base_dir)
            if resolved and resolved.is_file():
                try:
                    imported_source = resolved.read_text()
                except (OSError, UnicodeDecodeError):
                    continue

                # Extract type definitions from the imported file
                type_defs = backend.extract_type_definitions(imported_source)
                if type_defs:
                    all_type_defs.append(
                        f"# From {resolved.name}:\n{type_defs}"
                    )

                # Recurse into the imported file's imports
                transitive = self._resolve_cross_file_types(
                    imported_source, resolved, backend,
                    max_depth=max_depth - 1,
                    _visited=_visited,
                )
                if transitive:
                    all_type_defs.append(transitive)

        return "\n\n".join(all_type_defs)


# ─── Layer 2: Symbol Context ────────────────────────────────────────────────

class SymbolContextLayer:
    """Available functions, constants, cross-file signatures.

    The LLM should use symbols listed here — prevents hallucinated function names.
    Signatures are filtered and ranked by type compatibility with the hole's
    expected type, based on OOPSLA 2024 ChatLSP (3x fill quality improvement).

    Uses FunctionHeader dataclass with parsed return_type/param_types for
    structured scoring via type_relevance_score(), replacing the previous
    regex-based extraction from raw signature strings.
    """
    name = "symbols"

    def build(
        self,
        hole_target: Hole,
        source: str,
        path: Path,
        backend: Optional[LanguageBackend] = None,
    ) -> str:
        if backend is None:
            backend = get_backend(detect_language(path))

        parts: list[str] = []

        # Function headers from this file — ranked by type compatibility
        headers = backend.extract_function_headers(source)
        if headers:
            ranked_text = self.retrieve_relevant_headers(
                headers, hole_target.expected_type
            )
            parts.append(f"AVAILABLE FUNCTIONS (this file):\n{ranked_text}")

        # Cross-file function headers from imports — also ranked
        cross_headers = self._resolve_cross_file_headers(source, path, backend)
        if cross_headers:
            ranked_cross = self.retrieve_relevant_headers(
                cross_headers, hole_target.expected_type
            )
            parts.append(f"IMPORTED FUNCTIONS:\n{ranked_cross}")

        return "\n\n".join(parts)

    def retrieve_relevant_headers(
        self,
        headers: list[FunctionHeader],
        expected_type: Optional[str],
        max_relevant: int = 8,
        max_total: int = 15,
    ) -> str:
        """Filter and rank function headers by type compatibility with expected_type.

        Based on OOPSLA 2024 ChatLSP: providing type-compatible function headers
        improves fill quality 3x over unfiltered signature dumps.

        Uses FunctionHeader.type_relevance_score() for structured scoring —
        no regex extraction needed since return_type and param_types are
        already parsed by the backend.

        Ranking strategy:
          - Score via FunctionHeader.type_relevance_score(expected_type) — 0-4
          - Headers with score > 0 are "relevant" and shown first
          - Up to max_total headers shown total (filling with unmatched)

        If expected_type is None (bare/void holes), returns all headers unranked —
        no filtering applied since we have no type signal to filter on.

        Groups cross-file headers under "# From <file>:" section headers.

        Args:
            headers: Structured function headers from extract_function_headers.
            expected_type: The hole's expected type annotation (may be None).
            max_relevant: Max headers with score > 0 to include.
            max_total: Max total headers (relevant + unmatched filler).

        Returns:
            Filtered, ranked signature text ready to include in prompt.
        """
        if not headers:
            return ""

        if not expected_type:
            # No type signal → return all unranked, grouped by source file
            return self._format_headers(headers)

        # Score each header using structured type_relevance_score
        scored: list[tuple[int, FunctionHeader]] = [
            (h.type_relevance_score(expected_type), h)
            for h in headers
        ]

        # Stable sort: higher score first (original order preserved for ties)
        scored.sort(key=lambda x: -x[0])

        # Partition into relevant (score > 0) and unmatched
        relevant = [(sc, h) for sc, h in scored if sc > 0]
        unmatched = [(sc, h) for sc, h in scored if sc == 0]

        out_parts: list[str] = []

        # Add top relevant headers (score > 0)
        if relevant:
            out_parts.append("# Ranked by return-type compatibility:")
            relevant_headers = [h for _sc, h in relevant[:max_relevant]]
            out_parts.append(self._format_headers(relevant_headers))

        # Fill remaining slots with unmatched headers (fallback context)
        added_relevant = min(len(relevant), max_relevant)
        remaining_slots = max_total - added_relevant
        if remaining_slots > 0 and unmatched:
            if relevant:
                out_parts.append("# (other available functions)")
            filler_headers = [h for _sc, h in unmatched[:remaining_slots]]
            out_parts.append(self._format_headers(filler_headers))

        result = "\n".join(out_parts).strip()
        return result

    @staticmethod
    def _format_headers(headers: list[FunctionHeader]) -> str:
        """Format a list of FunctionHeaders as prompt text, grouped by source file.

        Headers from the same source_file are grouped under a "# From <file>:"
        section header. Headers with no source_file (current file) are listed
        without a section header.
        """
        if not headers:
            return ""

        # Group by source_file, preserving order
        groups: dict[Optional[str], list[FunctionHeader]] = {}
        for h in headers:
            key = h.source_file
            if key not in groups:
                groups[key] = []
            groups[key].append(h)

        parts: list[str] = []
        for source_file, group in groups.items():
            if source_file:
                parts.append(f"# From {source_file}:")
            for h in group:
                parts.append(h.signature)

        return "\n".join(parts)

    def _resolve_cross_file_headers(
        self,
        source: str,
        path: Path,
        backend: LanguageBackend,
    ) -> list[FunctionHeader]:
        """Extract structured function headers from imported local modules.

        Populates FunctionHeader.source_file so the prompt shows provenance.
        """
        base_dir = path.parent
        imports = backend.extract_imports(source)
        all_headers: list[FunctionHeader] = []

        for module, _names in imports:
            resolved = backend.resolve_import_path(module, base_dir)
            if resolved and resolved.is_file():
                try:
                    imported_source = resolved.read_text()
                except (OSError, UnicodeDecodeError):
                    continue

                headers = backend.extract_function_headers(imported_source)
                # Tag each header with its source file for prompt grouping
                for h in headers:
                    h.source_file = resolved.name
                all_headers.extend(headers)

        return all_headers


# ─── Layer 3: Behavioral Context ────────────────────────────────────────────

class BehaviorContextLayer:
    """@mole:behavior, requires, ensures from structured comments.

    This layer is OPTIONAL — can be toggled off for minimal prompts.
    When present, it gives the LLM semantic intent beyond just types.
    """
    name = "behavior"

    def build(
        self,
        hole_target: Hole,
        source: str,
        path: Path,
        backend: Optional[LanguageBackend] = None,
    ) -> str:
        spec = hole_target.behavior
        if not spec.behavior and not spec.requires and not spec.ensures:
            return ""

        parts: list[str] = []
        if spec.behavior:
            parts.append(f"BEHAVIOR: {spec.behavior}")
        if spec.requires:
            parts.append(f"REQUIRES: {spec.requires}")
        if spec.ensures:
            parts.append(f"ENSURES: {spec.ensures}")

        return "\n".join(parts)


# ─── Layer 4: Code Context ──────────────────────────────────────────────────

class CodeContextLayer:
    """Surrounding code, sibling holes, conventions.

    Gives the LLM the immediate code neighborhood so it matches
    style, indentation, and naming patterns.
    """
    name = "code"

    def build(
        self,
        hole_target: Hole,
        source: str,
        path: Path,
        backend: Optional[LanguageBackend] = None,
    ) -> str:
        if backend is None:
            backend = get_backend(detect_language(path))

        parts: list[str] = []

        # Language identifier
        parts.append(f"Language: {backend.language}")

        # Enclosing function/class block
        enclosing = backend.find_enclosing_block(source, hole_target.line_no)
        if enclosing:
            parts.append(f"ENCLOSING BLOCK:\n```\n{enclosing}\n```")

        # Detect indent style from source
        indent = self._detect_indent(source)
        parts.append(f"Indent style: {indent}")

        return "\n\n".join(parts)

    def _detect_indent(self, source: str) -> str:
        """Detect indentation style from source code."""
        tab_count = 0
        space_count = 0
        for line in source.splitlines():
            if line.startswith("\t"):
                tab_count += 1
            elif line.startswith("  "):
                space_count += 1

        if tab_count > space_count:
            return "tabs"
        # Try to detect spaces per level
        space_sizes: dict[int, int] = {}
        for line in source.splitlines():
            stripped = line.lstrip(" ")
            indent_len = len(line) - len(stripped)
            if indent_len > 0:
                space_sizes[indent_len] = space_sizes.get(indent_len, 0) + 1
        if space_sizes:
            # Most common indent level
            common = min(space_sizes.keys())
            return f"{common} spaces"
        return "4 spaces"  # Default


# ─── Assembler ────────────────────────────────────────────────────────────────

DEFAULT_LAYERS = [
    TypeContextLayer(),
    SymbolContextLayer(),
    BehaviorContextLayer(),
    CodeContextLayer(),
]


def assemble_context(
    hole_target: Hole,
    source: str,
    path: Path,
    layers: Optional[list] = None,
    backend: Optional[LanguageBackend] = None,
) -> str:
    """Compose prompt from selected context layers.

    Each layer produces a prompt fragment. Fragments are joined with
    section headers. Empty fragments are skipped.

    Args:
        layers: Which layers to include. Default: all four.
        backend: Language backend (auto-detected if not provided).
    """
    if layers is None:
        layers = DEFAULT_LAYERS

    if backend is None:
        backend = get_backend(detect_language(path))

    fragments: list[str] = []
    for layer in layers:
        # Pass backend to layer if it accepts it
        try:
            fragment = layer.build(hole_target, source, path, backend=backend)
        except TypeError:
            # Fallback for layers that don't accept backend kwarg
            fragment = layer.build(hole_target, source, path)

        if fragment and fragment.strip():
            fragments.append(f"## {layer.name.upper()}\n{fragment}")

    return "\n\n".join(fragments)
