"""mole — Same-file few-shot example extraction.

Layer 5 of the context assembly system. Extracts completed (non-hole)
functions from the same file as style examples for the fill prompt.

Research basis: CoqPilot (ASE 2024) — GPT-4 fill accuracy from 34% → 51%
with same-file few-shot examples. The LLM learns naming conventions,
code patterns, and style from the human's own code.

Fallback chain:
  1. Current file: completed functions (no hole() calls)
  2. Imported files: completed functions from local imports
  3. Empty: graceful degradation (no examples)

Excludes @mole:filled-by functions — machine-generated code teaches
machine style, not human style. The point is human conventions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .types import Hole, FunctionHeader
from .backends import LanguageBackend, get_backend, detect_language


# ─── Data Types ──────────────────────────────────────────────────────────────

@dataclass
class FewShotExample:
    """One complete function extracted as a style example.

    Carries enough metadata to rank, filter, and format for the prompt.
    """
    signature: str          # the function's signature line (for ranking)
    body: str               # complete function text including signature
    source_file: Optional[str] = None  # filename (None = current file)
    relevance_score: int = 0  # type relevance to target hole (0-4)


# ─── Extraction ──────────────────────────────────────────────────────────────

def _is_completed_function(func_text: str, exclude_mole_filled: bool) -> bool:
    """Check if a function body is complete (no holes, not machine-filled).

    A function is complete if:
    - It contains no hole() calls
    - If exclude_mole_filled: it has no @mole:filled-by comments
    """
    if "hole(" in func_text:
        return False
    if exclude_mole_filled and "@mole:filled-by" in func_text:
        return False
    return True


def extract_completed_functions(
    source: str,
    backend: LanguageBackend,
    exclude_mole_filled: bool = True,
) -> list[FewShotExample]:
    """Extract complete function bodies that contain no hole() calls.

    Uses tree-sitter via the backend to find function declarations,
    then filters to only those that are fully implemented.

    Args:
        source: Full source code text.
        backend: Language backend for tree-sitter parsing.
        exclude_mole_filled: Skip functions with @mole:filled-by comments.

    Returns:
        List of FewShotExamples from this file.
    """
    headers = backend.extract_function_headers(source)
    if not headers:
        return []

    lines = source.splitlines()
    results: list[FewShotExample] = []

    for header in headers:
        # Find the definition line by matching def/function name(
        func_line = None
        pattern = re.compile(
            rf"\bdef\s+{re.escape(header.name)}\s*\("
            rf"|\bfunction\s+{re.escape(header.name)}\s*\("
        )
        for i, line in enumerate(lines, 1):
            if pattern.search(line):
                func_line = i
                break

        if func_line is None:
            continue

        # Get full function body via tree-sitter enclosing block
        body = backend.find_enclosing_block(source, func_line)
        if not body:
            continue

        if not _is_completed_function(body, exclude_mole_filled):
            continue

        results.append(FewShotExample(
            signature=header.signature,
            body=body,
        ))

    return results


def rank_examples(
    examples: list[FewShotExample],
    hole_target: Hole,
    max_examples: int = 3,
) -> list[FewShotExample]:
    """Rank examples by type relevance to the target hole.

    Uses FunctionHeader.type_relevance_score() for structured ranking.
    Mutates each example's relevance_score field, sorts descending.

    Args:
        examples: FewShotExamples to rank.
        hole_target: The hole being filled — used for type matching.
        max_examples: Maximum examples to return.

    Returns:
        Top-k examples ranked by type relevance, most relevant first.
    """
    if not examples:
        return []

    for ex in examples:
        # Parse return type from signature string (e.g. "def foo(x: int) -> str: ...")
        ret_match = re.search(r'->\s*(.+?)(?:\s*[:{]|\s*\.\.\.)', ex.signature)
        ret_type = ret_match.group(1).strip() if ret_match else None

        header = FunctionHeader(
            name=ex.signature.split("(")[0].split()[-1] if "(" in ex.signature else "",
            signature=ex.signature,
            return_type=ret_type,
        )
        ex.relevance_score = header.type_relevance_score(hole_target.expected_type)

    # Sort by relevance (descending), stable sort preserves original order for ties
    examples.sort(key=lambda e: -e.relevance_score)
    return examples[:max_examples]


def extract_cross_file_examples(
    source: str,
    path: Path,
    backend: LanguageBackend,
    max_examples: int = 3,
) -> list[FewShotExample]:
    """Fallback: extract examples from imported local files.

    Follows the same import resolution as _resolve_cross_file_types
    but extracts function bodies instead of type definitions.
    Skips stdlib/site-packages.

    Args:
        source: Current file source (to find import statements).
        path: Current file path (to resolve relative imports).
        backend: Language backend for parsing.
        max_examples: Max total examples across all imported files.

    Returns:
        FewShotExamples from imported modules, tagged with source_file.
    """
    # Deferred import to avoid circular dependency (context.py → few_shot.py → context.py)
    from .context import _is_stdlib_path, _resolve_stdlib_path

    imports = backend.extract_imports(source)
    base_dir = path.parent
    all_examples: list[FewShotExample] = []

    for module, _names in imports:
        resolved = backend.resolve_import_path(module, base_dir)

        # Fallback to importlib for non-relative imports
        if not resolved or not resolved.is_file():
            resolved = _resolve_stdlib_path(module)

        if not resolved or not resolved.is_file():
            continue

        # Skip stdlib/site-packages — not style examples
        if _is_stdlib_path(resolved):
            continue

        try:
            imported_source = resolved.read_text()
        except (OSError, UnicodeDecodeError):
            continue

        examples = extract_completed_functions(imported_source, backend)
        for ex in examples:
            ex.source_file = resolved.name
        all_examples.extend(examples)

        if len(all_examples) >= max_examples:
            break

    return all_examples[:max_examples]


# ─── Formatting ──────────────────────────────────────────────────────────────

def format_examples(examples: list[FewShotExample], from_imports: bool = False) -> str:
    """Format examples as a prompt section.

    Args:
        examples: Ranked examples to format.
        from_imports: True if examples came from imported files (changes header).

    Returns:
        Formatted prompt section, or empty string if no examples.
    """
    if not examples:
        return ""

    header = "EXAMPLES FROM IMPORTED FILES:" if from_imports else "EXAMPLES FROM THIS FILE:"
    parts = [header]
    for ex in examples:
        label = f"# From {ex.source_file}" if ex.source_file else f"# {ex.signature.split('(')[0].strip()}"
        parts.append(f"{label}\n```\n{ex.body}\n```")
    return "\n\n".join(parts)


# ─── Context Layer ───────────────────────────────────────────────────────────

class FewShotContextLayer:
    """Context layer that provides same-file function examples.

    Fallback chain: current file → imported files → empty.
    Examples are ranked by type relevance to the target hole.
    """
    name = "examples"

    def build(
        self,
        hole_target: Hole,
        source: str,
        path: Path,
        backend: Optional[LanguageBackend] = None,
    ) -> str:
        """Build few-shot examples section for the fill prompt."""
        if backend is None:
            backend = get_backend(detect_language(path))

        # Step 1: Try current file
        examples = extract_completed_functions(source, backend)
        from_imports = False

        if not examples:
            # Step 2: Fallback to imported files
            examples = extract_cross_file_examples(source, path, backend)
            from_imports = True

        if not examples:
            return ""  # Graceful degradation

        ranked = rank_examples(examples, hole_target)
        return format_examples(ranked, from_imports=from_imports)
