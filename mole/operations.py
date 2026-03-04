"""mole — Core operations.

These are the primitives that everything else is built on.
Each operates on a single hole. Batch operations are wrappers.

The operations:
  discover   — find all holes in a file, extract types
  expand     — decompose one hole into sub-holes (THE default operation)
  diversify  — generate N different expansions for human to choose from
  fill       — implement one leaf hole with LLM-generated code
  verify     — type-check a fill against the source
  apply      — insert a fill into the source, preserving @mole: comments
  edit_hole  — mark existing code for AI replacement (refactoring primitive)
  propagate  — change type → run pyright → auto-generate holes at error sites
  antiunify  — find structurally similar code → propose hole groups

NO python ast module. All source analysis uses language backends (tree-sitter).
"""
from __future__ import annotations

import re
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .types import (
    Hole, HoleStatus, BehaviorSpec, Expansion,
    Filler, VerifyResult, MoleFile, HoleGroup,
)
from .backends import LanguageBackend, get_backend, detect_language
from .context import assemble_context, DEFAULT_LAYERS
from .protocol import (
    parse_mole_comments, attach_specs_to_holes,
    comment_prefix_for_language, format_mole_comments,
)
from .prompts import (
    FILL_PROMPT_TEMPLATE, EXPAND_PROMPT_TEMPLATE,
    DIVERSIFY_PROMPT_TEMPLATE, fill_mode_hint,
)


# ─── discover ─────────────────────────────────────────────────────────────────

def discover(path: Path, backend: Optional[LanguageBackend] = None) -> MoleFile:
    """Find all holes in a file, extract types, return a MoleFile.

    Steps:
      1. Read file, detect language from extension
      2. Find all hole() calls via tree-sitter backend
      3. Extract expected types via type checker (pyright/tsc)
      4. Parse @mole: structured comments
      5. Attach specs to holes
      6. Return MoleFile with all holes populated
    """
    source = path.read_text()

    # Detect language from file extension
    language = detect_language(path)

    # Get appropriate backend
    if backend is None:
        backend = get_backend(language)

    # Find all hole() call sites via tree-sitter
    holes = backend.find_holes(source)

    # Extract expected types via type checker sentinel trick
    holes = backend.extract_types(path, holes)

    # Parse @mole: structured comments
    comment_prefix = comment_prefix_for_language(language)
    parsed_comments = parse_mole_comments(source, comment_prefix, language)

    # Attach behavioral specs to holes
    holes = attach_specs_to_holes(holes, parsed_comments)

    # Populate content anchors for all holes (enables context-anchored matching
    # when the user edits the file between discover() and fill/apply operations)
    holes = _populate_hole_context(holes, source, n=3)

    return MoleFile(path=path, source=source, holes=holes, language=language)


# ─── expand ───────────────────────────────────────────────────────────────────

def expand(
    hole_target: Hole,
    source: str,
    path: Path,
    filler: Filler,
    context_layers: Optional[list] = None,
    backend: Optional[LanguageBackend] = None,
    idea_hint: Optional[str] = None,
    on_chunk: Optional[callable] = None,
) -> Expansion:
    """Decompose one hole into real code with typed sub-holes.

    The result is readable code with smaller, more constrained holes.
    NOT closures. NOT pseudocode. Real, inline code that shares parent scope.
    """
    if context_layers is None:
        context_layers = DEFAULT_LAYERS
    if backend is None:
        backend = get_backend(detect_language(path))

    # Resolve hole position in case line numbers shifted since discover()
    _resolve_hole_idx(hole_target, source)

    language = backend.language
    comment_prefix = comment_prefix_for_language(language)

    # Assemble context for this hole (uses hole_target.line_no for scope/enclosing block)
    context = assemble_context(hole_target, source, path, context_layers, backend)

    # Build expand prompt — hint goes right after task description for maximum salience
    hint_text = f"\nMANDATORY APPROACH: You MUST use this specific approach — {idea_hint}\nDo NOT default to the obvious/simple solution. Implement EXACTLY this approach." if idea_hint else ""
    prompt = EXPAND_PROMPT_TEMPLATE.format(
        description=hole_target.description,
        context=context,
        comment_prefix=comment_prefix,
        valid_code=f"- The code must be valid {language} that would compile if holes were filled.",
        idea_hint=hint_text,
    )

    # Call filler (with streaming callback if provided)
    raw_expansion = filler.fill(prompt, on_chunk=on_chunk) if on_chunk else filler.fill(prompt)

    # Clean output
    raw_expansion = _strip_reasoning_leaks(raw_expansion)

    # Parse expansion — find sub-holes and extract approach name
    approach_name = "unnamed"
    approach_desc = ""

    # Extract approach name from first comment line
    for line in raw_expansion.splitlines():
        stripped = line.strip()
        if stripped.startswith(comment_prefix):
            content = stripped[len(comment_prefix):].strip()
            if content.lower().startswith("approach:"):
                approach_name = content.split(":", 1)[1].strip()
                break

    # Find sub-holes in the expanded code using the backend
    sub_holes = backend.find_holes(raw_expansion)

    # Parse @mole: comments in expanded code for sub-hole specs
    parsed = parse_mole_comments(raw_expansion, comment_prefix, language)
    sub_holes = attach_specs_to_holes(sub_holes, parsed)

    # Mark parent as expanded
    hole_target.status = HoleStatus.EXPANDED

    return Expansion(
        approach_name=approach_name,
        approach_description=approach_desc or idea_hint or "",
        expanded_code=raw_expansion,
        sub_holes=sub_holes,
    )


# ─── diversify ───────────────────────────────────────────────────────────────

def diversify(
    hole_target: Hole,
    source: str,
    path: Path,
    filler: Filler,
    context_layers: Optional[list] = None,
    backend: Optional[LanguageBackend] = None,
    n: int = 3,
    on_chunk: Optional[callable] = None,
    on_title: Optional[callable] = None,
) -> list[Expansion]:
    """Generate N different expansion approaches for the human to choose from.

    Two-step:
      1. idea_gen: cheap LLM call for N approach ideas
      2. expand: call expand() per approach with the idea as hint

    on_chunk(idx, text): called with streaming deltas for approach idx
    on_title(idx, name): called when approach name is known for idx
    """
    if context_layers is None:
        context_layers = DEFAULT_LAYERS
    if backend is None:
        backend = get_backend(detect_language(path))

    # Step 1: Generate approach ideas
    idea_prompt = DIVERSIFY_PROMPT_TEMPLATE.format(
        n=n,
        description=hole_target.description,
        expected_type=hole_target.expected_type or "unknown",
    )
    raw_ideas = filler.fill(idea_prompt)

    # Parse ideas from LLM output
    ideas = _parse_ideas(raw_ideas, n)

    # Step 2: Expand all approaches in parallel
    def _expand_one(idea: tuple[str, str], idx: int) -> Expansion:
        name, desc = idea
        if on_title:
            on_title(idx, name)
        # Create per-approach chunk callback
        chunk_cb = (lambda c: on_chunk(idx, c)) if on_chunk else None
        exp = expand(
            hole_target, source, path, filler,
            context_layers, backend,
            idea_hint=desc,
            on_chunk=chunk_cb,
        )
        exp.approach_name = name
        exp.approach_description = desc
        return exp

    expansions: list[Expansion] = [None] * len(ideas)  # type: ignore
    with ThreadPoolExecutor(max_workers=len(ideas)) as pool:
        futures = {pool.submit(_expand_one, idea, i): i for i, idea in enumerate(ideas)}
        for future in as_completed(futures):
            idx = futures[future]
            expansions[idx] = future.result()

    return expansions


def _parse_ideas(raw: str, n: int) -> list[tuple[str, str]]:
    """Parse approach ideas from LLM output.

    Expected format:
    APPROACH 1: <name>
    <description>
    """
    ideas: list[tuple[str, str]] = []
    pattern = re.compile(r'APPROACH\s+\d+:\s*(.+)')

    lines = raw.strip().splitlines()
    i = 0
    while i < len(lines):
        m = pattern.match(lines[i].strip())
        if m:
            name = m.group(1).strip()
            # Next non-empty line is description
            desc = ""
            if i + 1 < len(lines):
                desc = lines[i + 1].strip()
            ideas.append((name, desc))
        i += 1

    # Pad if we got fewer than expected
    while len(ideas) < n:
        ideas.append((f"approach-{len(ideas)+1}", "alternative approach"))

    return ideas[:n]


# ─── fill ────────────────────────────────────────────────────────────────────

def fill(
    hole_target: Hole,
    source: str,
    path: Path,
    filler: Filler,
    context_layers: Optional[list] = None,
    backend: Optional[LanguageBackend] = None,
    max_retries: int = 3,
    extra_imports: Optional[list[str]] = None,
    on_chunk: Optional[callable] = None,
) -> tuple[str, VerifyResult]:
    """Fill a single hole with LLM-generated code.

    Includes a verify-retry loop: fill → type-check → if errors, retry
    with error feedback.

    extra_imports: imports from other fills in a batch (deferred hoisting).
    These are included in verify so their absence doesn't cause false errors.

    Returns: (filled_code, verify_result)
    """
    if context_layers is None:
        context_layers = DEFAULT_LAYERS
    if backend is None:
        backend = get_backend(detect_language(path))

    # Resolve hole position in case line numbers shifted since discover()
    # (e.g. due to earlier fills in a batch inserting extra lines, or user edits)
    _resolve_hole_idx(hole_target, source)

    # Assemble context (uses hole_target.line_no for scope vars, enclosing block)
    context = assemble_context(hole_target, source, path, context_layers, backend)

    # Type and behavior constraints for the prompt
    type_text = ""
    if hole_target.expected_type:
        type_text = f"The result MUST have type: {hole_target.expected_type}"

    behavior_text = ""
    spec = hole_target.behavior
    if spec.behavior or spec.requires or spec.ensures:
        parts = []
        if spec.behavior:
            parts.append(f"BEHAVIOR: {spec.behavior}")
        if spec.requires:
            parts.append(f"REQUIRES: {spec.requires}")
        if spec.ensures:
            parts.append(f"ENSURES: {spec.ensures}")
        behavior_text = "\n".join(parts)

    # Build fill prompt
    prompt = FILL_PROMPT_TEMPLATE.format(
        description=hole_target.description,
        context=context,
        fill_mode_hint=fill_mode_hint(hole_target.is_bare),
        type_constraint=type_text,
        behavior_constraint=behavior_text,
    )

    # Fill-verify-retry loop
    best_code = ""
    best_result = VerifyResult(success=False)
    errors_feedback = ""

    for attempt in range(max_retries):
        # Append error feedback on retries
        current_prompt = prompt
        if errors_feedback:
            current_prompt += (
                f"\n\nPREVIOUS ATTEMPT FAILED with these type errors:\n"
                f"{errors_feedback}\n"
                f"Fix these errors. Return ONLY the corrected code."
            )

        # Call filler (stream only on first attempt — retries are corrections)
        if attempt == 0 and on_chunk:
            raw_code = filler.fill(current_prompt, on_chunk=on_chunk)
        else:
            raw_code = filler.fill(current_prompt)
        raw_code = _strip_reasoning_leaks(raw_code)

        # Extract #import: lines so they can be hoisted for verification
        clean_code, new_imports = _extract_fill_imports(raw_code)

        # Verify with import-aware substitution:
        # 1. Substitute fill into source FIRST (at correct line_no)
        # 2. THEN hoist imports (so line shift doesn't affect substitution)
        # extra_imports allows batch fills to include imports from previous fills
        all_verify_imports = new_imports + list(extra_imports or [])
        result = _verify_with_imports(
            hole_target, clean_code, all_verify_imports, source, path, backend
        )

        if result.success or len(result.new_errors) < len(best_result.new_errors) or not best_code:
            best_code = raw_code
            best_result = result

        if result.success:
            break  # Clean — no new type errors

        # Build error feedback for next retry
        errors_feedback = "\n".join(result.new_errors[:5])  # Cap at 5 errors

    # Update hole status
    hole_target.fill_code = best_code
    hole_target.status = HoleStatus.FILLED if best_result.success else HoleStatus.UNFILLED
    hole_target.filled_by = f"fill@{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"

    return best_code, best_result


# ─── verify ──────────────────────────────────────────────────────────────────

def verify(
    hole_target: Hole,
    filled_code: str,
    source: str,
    path: Path,
    backend: Optional[LanguageBackend] = None,
) -> VerifyResult:
    """Type-check a fill against the source.

    Inserts the fill into the source at the hole's location, runs the
    type checker, and compares against baseline errors (pre-existing).
    """
    if backend is None:
        backend = get_backend(detect_language(path))

    # Insert filled code into source at hole location
    test_source = _substitute_fill(hole_target, filled_code, source)

    # Run type checker on filled source
    all_errors = backend.verify(test_source, path)

    # Get baseline errors from original source
    baseline = backend.verify(source, path)

    # Find NEW errors (introduced by the fill)
    baseline_set = set(baseline)
    new_errors = [e for e in all_errors if e not in baseline_set]

    return VerifyResult(
        success=len(new_errors) == 0,
        errors=all_errors,
        baseline_errors=baseline,
        new_errors=new_errors,
    )


def _verify_with_imports(
    hole_target: Hole,
    clean_code: str,
    new_imports: list[str],
    source: str,
    path: Path,
    backend: Optional[LanguageBackend] = None,
) -> VerifyResult:
    """Verify a fill with proper import hoisting.

    Order matters: substitute fill FIRST (at correct line_no), THEN hoist
    imports. This prevents import insertion from shifting line numbers before
    the hole substitution.
    """
    if backend is None:
        backend = get_backend(detect_language(path))

    # Step 1: Substitute fill into source at the correct line
    test_source = _substitute_fill(hole_target, clean_code, source)

    # Step 2: Hoist imports into the already-substituted source
    if new_imports:
        lines = test_source.splitlines()
        insert_idx = _find_import_insert_point(lines, backend.language)
        for imp in reversed(new_imports):
            lines.insert(insert_idx, imp)
        test_source = "\n".join(lines)

    # Step 3: Run type checker
    all_errors = backend.verify(test_source, path)

    # Step 4: Baseline with imports hoisted too (so pre-existing import errors
    # from the hoisted imports don't count as "new")
    baseline_source = source
    if new_imports:
        bl = baseline_source.splitlines()
        insert_idx = _find_import_insert_point(bl, backend.language)
        for imp in reversed(new_imports):
            bl.insert(insert_idx, imp)
        baseline_source = "\n".join(bl)
    baseline = backend.verify(baseline_source, path)

    baseline_set = set(baseline)
    new_errors = [e for e in all_errors if e not in baseline_set]

    return VerifyResult(
        success=len(new_errors) == 0,
        errors=all_errors,
        baseline_errors=baseline,
        new_errors=new_errors,
    )


def _substitute_expand(hole_target: Hole, expanded_code: str, source: str) -> str:
    """Replace the ENTIRE line containing hole() with the expansion code block.

    This is for expand/diversify operations where the expansion is a complete
    code block (with its own variable assignments, sub-holes, etc.) that should
    replace the whole line — NOT splice into the hole() call site.

    The key difference from _substitute_fill:
    - fill: `code: str = hole(...)` → replace `hole(...)` with expression → `code: str = expr`
    - expand: `code: str = hole(...)` → replace whole line with expansion block

    Uses content-anchored matching to find the hole if line numbers have shifted.
    """
    lines = source.splitlines()
    hole_idx = _resolve_hole_idx(hole_target, source)

    if hole_idx < 0 or hole_idx >= len(lines):
        return source

    original_line = lines[hole_idx]
    indent = _get_indent(original_line)

    # The expansion code block replaces the entire line.
    # Each line of the expansion gets the same base indent as the original hole line.
    expanded_lines = expanded_code.strip().splitlines()
    # Dedent expansion to base level, then re-indent to match original
    dedented = textwrap.dedent("\n".join(expanded_lines)).splitlines()
    replacement = [indent + l if l.strip() else l for l in dedented]

    lines[hole_idx:hole_idx + 1] = replacement
    return "\n".join(lines)


def _substitute_fill(hole_target: Hole, filled_code: str, source: str) -> str:
    """Replace the hole() call with filled code in source.

    Language-agnostic: replaces just the hole(...) call text in the original
    line, preserving surrounding syntax (const, let, var, assignment, return).
    For multi-statement fills, inserts setup lines before and replaces hole()
    with the final expression.

    Uses content-anchored matching to find the hole if line numbers have shifted.
    """
    lines = source.splitlines()
    hole_idx = _resolve_hole_idx(hole_target, source)

    if hole_idx < 0 or hole_idx >= len(lines):
        return source

    original_line = lines[hole_idx]
    indent = _get_indent(original_line)

    # Find the hole(...) call in the original line using regex
    # Matches: hole("..."), hole('...'), hole("...", ...) etc.
    hole_pattern = re.compile(r'hole\s*\([^)]*\)')
    match = hole_pattern.search(original_line)
    if not match:
        # Fallback: replace entire line
        lines[hole_idx] = indent + filled_code.strip()
        return "\n".join(lines)

    # Split filled code into setup lines + final expression
    setup_lines, final_expr = _split_multi_statement_fill(filled_code)

    if not final_expr and not setup_lines:
        final_expr = filled_code.strip()

    # Replace hole(...) with the final expression in the original line
    if final_expr:
        new_line = original_line[:match.start()] + final_expr + original_line[match.end():]
    else:
        new_line = original_line[:match.start()] + filled_code.strip() + original_line[match.end():]

    # Detect and fix redundant double-assignment pattern:
    # e.g. `code: str = code` where the fill produced the same var as setup
    # In this case, the setup already has the real assignment — drop the
    # redundant original line and just use the setup lines directly.
    redundant_match = re.match(r'^(\s*\w+\s*(?::\s*\S+\s*)?)=\s*(\w+)\s*$', new_line)
    if redundant_match and setup_lines:
        var_in_assignment = redundant_match.group(2).strip()
        # Check if any setup line assigns to this same variable
        for sl in setup_lines:
            sl_assign = re.match(r'^(\w+)\s*(?::\s*[^=]+)?\s*=', sl.strip())
            if sl_assign and sl_assign.group(1) == var_in_assignment:
                # Setup already assigns to this var — skip the redundant line
                replacement_lines = []
                for sl2 in setup_lines:
                    replacement_lines.append(indent + sl2)
                lines[hole_idx:hole_idx + 1] = replacement_lines
                return "\n".join(lines)

    # Build replacement: setup lines (with preserved relative indent) + modified line
    replacement_lines = []
    for sl in setup_lines:
        # Preserve relative indentation: add base indent to each setup line
        replacement_lines.append(indent + sl)
    replacement_lines.append(new_line)

    lines[hole_idx:hole_idx + 1] = replacement_lines
    return "\n".join(lines)


def _split_multi_statement_fill(code: str) -> tuple[list[str], str]:
    """Split a multi-line fill into setup statements + final expression.

    Returns (setup_lines, final_expression).
    Setup lines preserve RELATIVE indentation (dedented to base level).
    If the fill is a single expression, returns ([], expression).
    """
    raw_lines = code.strip().splitlines()
    non_empty = [l for l in raw_lines if l.strip()]

    if not non_empty:
        return [], ""

    if len(non_empty) == 1:
        line = non_empty[0].strip()
        # If the single-line fill is itself a typed assignment (e.g. "code: str = digest[:length]"),
        # extract just the RHS expression. The original line already has the var name and type
        # annotation — substituting the whole thing creates "var: Type = var: Type = expr".
        typed_assign = re.match(r'^\w+\s*:\s*[^=]+\s*=\s*(.+)$', line)
        if typed_assign:
            return [], typed_assign.group(1).strip()
        # Also catch untyped assignment fills: "code = digest[:length]" → just "digest[:length]"
        plain_assign = re.match(r'^\w+\s*=\s*(.+)$', line)
        if plain_assign:
            return [], plain_assign.group(1).strip()
        return [], line

    # Dedent the whole block to normalize indentation
    dedented = textwrap.dedent("\n".join(raw_lines)).splitlines()
    # Filter empty lines but preserve order for relative indent
    dedented = [l for l in dedented if l.strip()]

    if not dedented:
        return [], ""

    # Check if the ENTIRE block is one multi-line expression (e.g. a chained
    # method call like `entries.flatMap(e => {...}).sort(...)`)
    # If the first line opens brackets that aren't closed until the last line,
    # it's all one expression — don't split.
    openers = {"(": ")", "[": "]", "{": "}"}
    closers_set = set(openers.values())
    total_depth = 0
    for line in dedented:
        for ch in line:
            if ch in openers:
                total_depth += 1
            elif ch in closers_set:
                total_depth -= 1
    # If braces balance AND first line opens something, it's one expression
    if total_depth == 0:
        first_depth = 0
        for ch in dedented[0]:
            if ch in openers:
                first_depth += 1
            elif ch in closers_set:
                first_depth -= 1
        if first_depth > 0:
            # First line opens brackets not closed on that line → multi-line expr
            return [], "\n".join(dedented)

    # Check if last line is an expression (not an assignment, not control flow)
    last = dedented[-1].strip()

    # If last line is an assignment (with or without type annotation), it's all
    # setup — use the variable name as the final expression.
    # Matches: `var = expr`, `var: Type = expr`, `var:Type=expr`
    assign_match = re.match(r'^(\w+)\s*(?::\s*[^=]+)?\s*=\s*', last)
    if assign_match and not last.startswith(("if ", "for ", "while ", "def ", "class ")):
        # All lines are setup, var name is the final expression
        return dedented, assign_match.group(1)

    # If last line starts with keywords, it's unsplittable — return all as-is
    if last.startswith(("if ", "for ", "while ", "def ", "class ", "return ", "yield ")):
        return [], "\n".join(dedented)

    # Handle multi-line expressions (closing brackets on their own line)
    if last in ("}", "]", ")"):
        depth = 0
        openers = {"(": ")", "[": "]", "{": "}"}
        closers = {v: k for k, v in openers.items()}
        expr_start = len(dedented) - 1
        for i in range(len(dedented) - 1, -1, -1):
            line = dedented[i].strip()
            for ch in reversed(line):
                if ch in closers:
                    depth += 1
                elif ch in openers:
                    depth -= 1
            if depth <= 0:
                expr_start = i
                break
        setup = dedented[:expr_start]
        expr = "\n".join(dedented[expr_start:])
        return setup, expr

    # Default: last line is the expression, rest is setup
    return dedented[:-1], last


# ─── apply ───────────────────────────────────────────────────────────────────

def apply(
    hole_target: Hole,
    filled_code: str,
    source: str,
    path: Path,
    backend: Optional[LanguageBackend] = None,
    mode: str = "fill",
) -> str:
    """Insert a fill or expansion into the source, preserving @mole: comments.

    mode="fill"   — expression substitution: replaces hole(...) call with expression
    mode="expand" — region substitution: replaces the entire line(s) with expansion code

    Returns the new source string.
    """
    if backend is None:
        backend = get_backend(detect_language(path))

    language = backend.language
    comment_prefix = comment_prefix_for_language(language)

    # Extract and hoist #import: lines
    filled_code, new_imports = _extract_fill_imports(filled_code)

    # Substitute based on mode
    if mode == "expand":
        new_source = _substitute_expand(hole_target, filled_code, source)
    else:
        new_source = _substitute_fill(hole_target, filled_code, source)

    # Add provenance comment above the filled code
    provenance = hole_target.filled_by or f"v3@{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    provenance_comment = f"{comment_prefix} @mole:filled-by {provenance}"

    # Insert provenance on the line before the fill
    new_lines = new_source.splitlines()
    fill_line_idx = hole_target.line_no - 1  # 0-indexed
    if 0 <= fill_line_idx < len(new_lines):
        indent = _get_indent(new_lines[fill_line_idx])
        new_lines.insert(fill_line_idx, f"{indent}{provenance_comment}")

    # Hoist imports to top of file
    if new_imports:
        # Find insertion point after existing imports
        insert_idx = _find_import_insert_point(new_lines, language)
        for imp in reversed(new_imports):
            new_lines.insert(insert_idx, imp)

    # Mark hole as filled
    hole_target.status = HoleStatus.FILLED

    return "\n".join(new_lines)


def _extract_fill_imports(code: str) -> tuple[str, list[str]]:
    """Extract #import: lines from fill code, return cleaned code + imports."""
    imports: list[str] = []
    clean_lines: list[str] = []

    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith("#import:"):
            imp = stripped[len("#import:"):].strip()
            imports.append(imp)
        else:
            clean_lines.append(line)

    return "\n".join(clean_lines), imports


def _find_import_insert_point(lines: list[str], language: str) -> int:
    """Find the line index after the last import statement."""
    last_import = 0
    import_patterns = {
        "python": re.compile(r'^\s*(import |from .+ import )'),
        "typescript": re.compile(r'^\s*(import |require\()'),
        "javascript": re.compile(r'^\s*(import |require\()'),
    }
    pattern = import_patterns.get(language, import_patterns["python"])

    for i, line in enumerate(lines):
        if pattern.match(line):
            last_import = i + 1
        # Also skip module docstrings / shebangs at top
        if i < 5 and (line.strip().startswith('"""') or line.strip().startswith("#!")):
            last_import = max(last_import, i + 1)

    return last_import


# ─── Content-Anchored Hole Matching ──────────────────────────────────────────

_CONTEXT_N = 3  # lines of context to capture above/below each hole


def _populate_hole_context(holes: list[Hole], source: str, n: int = _CONTEXT_N) -> list[Hole]:
    """Populate context_before and context_after on every hole.

    These content anchors allow holes to be re-located in modified source
    even when line numbers have shifted due to user edits.

    Called once by discover() immediately after holes are found.
    """
    lines = source.splitlines()
    for h in holes:
        idx = h.line_no - 1  # 0-indexed
        before_start = max(0, idx - n)
        after_end = min(len(lines), idx + n + 1)
        h.context_before = "\n".join(lines[before_start:idx])
        h.context_after = "\n".join(lines[idx + 1:after_end])
    return holes


def _context_similarity(
    cand_before: list[str],
    cand_after: list[str],
    expected_before: list[str],
    expected_after: list[str],
) -> int:
    """Score how well a candidate location's context matches expected context.

    Closer lines (nearer to the hole) receive higher weight.
    Comparison is on stripped (whitespace-normalized) lines.
    Returns a non-negative integer score. Higher = better match.
    """
    score = 0
    # before context: compare from closest line outward
    for i, line in enumerate(reversed(cand_before)):
        expected_idx = len(expected_before) - 1 - i
        if expected_idx >= 0 and line.strip() == expected_before[expected_idx].strip():
            score += (len(cand_before) - i)  # weight by proximity
    # after context: compare from closest line outward
    for i, line in enumerate(cand_after):
        if i < len(expected_after) and line.strip() == expected_after[i].strip():
            score += (len(cand_after) - i)  # weight by proximity
    return score


def relocate_hole(hole: Hole, source: str, n: int = _CONTEXT_N) -> Optional[int]:
    """Find a hole's current line number in modified source using context anchors.

    When the user edits a file between discover() and fill/apply, line numbers
    shift. This function re-locates the hole by:
      1. Fast path: check if hole() is still at hole.line_no (no shift)
      2. Content-anchored search: scan all hole() lines, score by context similarity
      3. Description match: fall back to matching by hole description string

    Returns the new 1-indexed line number, or None if the hole can't be found.
    """
    lines = source.splitlines()
    total = len(lines)

    # Fast path: hole still at same line
    if 0 < hole.line_no <= total and "hole(" in lines[hole.line_no - 1]:
        return hole.line_no

    # Content-anchored search
    # Find all lines that contain a hole() call
    hole_line_indices: list[int] = [i for i, l in enumerate(lines) if "hole(" in l]

    if not hole_line_indices:
        return None

    expected_before: list[str] = hole.context_before.splitlines() if hole.context_before else []
    expected_after: list[str] = hole.context_after.splitlines() if hole.context_after else []

    best_line_no: Optional[int] = None
    best_score: int = -1

    # Also check description — a hole with a unique description is a strong signal
    desc_fragment = hole.description[:30] if hole.description else ""

    for idx in hole_line_indices:
        cand_line = lines[idx]

        # Bonus: description match
        desc_bonus = 1 if desc_fragment and desc_fragment in cand_line else 0

        cand_before = lines[max(0, idx - n): idx]
        cand_after = lines[idx + 1: min(total, idx + n + 1)]

        score = _context_similarity(cand_before, cand_after, expected_before, expected_after)
        score += desc_bonus * (2 * n)  # description match weighted heavily

        if score > best_score:
            best_score = score
            best_line_no = idx + 1  # 1-indexed

    # Return the best match only if we found at least one matching context line
    return best_line_no if best_score > 0 else None


def _resolve_hole_idx(hole_target: Hole, source: str) -> int:
    """Return the 0-indexed line index for a hole in source.

    Tries the stored line_no first; falls back to content-anchored search.
    Updates hole_target.line_no if relocation changes the line number.

    Returns -1 if the hole cannot be found.
    """
    lines = source.splitlines()
    idx = hole_target.line_no - 1  # 0-indexed

    # Check if the hole is where we expect it
    if 0 <= idx < len(lines) and "hole(" in lines[idx]:
        return idx

    # Try content-anchored relocation
    new_line_no = relocate_hole(hole_target, source)
    if new_line_no is not None:
        hole_target.line_no = new_line_no
        return new_line_no - 1

    return -1


def resync(dfile: MoleFile) -> MoleFile:
    """Re-locate all holes in a MoleFile against the current file contents.

    Call this after the user has edited the file externally (added/removed lines).
    Uses content-anchored matching to find each hole's new position.

    Workflow:
      1. Re-read the file from disk
      2. For each hole, use context anchors to find its new line number
      3. Update context anchors to reflect the new positions
      4. Remove holes that can no longer be found (deleted by user)

    Returns the updated MoleFile (mutates in place AND returns).
    """
    # Re-read file from disk
    if dfile.path.is_file():
        dfile.source = dfile.path.read_text()

    surviving: list[Hole] = []
    stale_count = 0

    for h in dfile.holes:
        new_idx = _resolve_hole_idx(h, dfile.source)
        if new_idx >= 0:
            surviving.append(h)
        else:
            stale_count += 1

    # Update context anchors for surviving holes (they may have shifted)
    _populate_hole_context(surviving, dfile.source)

    dfile.holes = surviving

    if stale_count > 0:
        print(f"  resync: {stale_count} hole(s) could not be relocated (removed from tracking)")
    print(f"  resync: {len(surviving)} hole(s) re-located successfully")

    return dfile


# ─── edit_hole ───────────────────────────────────────────────────────────────

def edit_hole(
    source: str,
    line_no: int,
    description: str,
    path: Path,
    backend: Optional[LanguageBackend] = None,
) -> str:
    """Mark existing code at line_no for AI replacement by wrapping it in hole().

    This is the refactoring primitive: instead of writing new holes in blank
    spots, you point at existing code and say "replace this with something
    better." The original code is preserved in the hole description so the
    LLM has it as reference context.

    Steps:
      1. Read the line at line_no
      2. Detect the assignment pattern (var: Type = expr, var = expr, or bare expr)
      3. Replace the expression with hole("description | original: <code>")
      4. If there's a type annotation, preserve it
      5. Return the modified source

    The | original: suffix gives the LLM the existing implementation as
    reference — it can improve, rewrite, or take a completely different approach.

    Args:
        source: Current file source.
        line_no: 1-indexed line number of the code to replace.
        description: What the replacement should do.
        path: File path (for language detection).
        backend: Language backend (auto-detected if None).

    Returns:
        Modified source with hole() at the target line.
    """
    if backend is None:
        backend = get_backend(detect_language(path))

    lines = source.splitlines()
    idx = line_no - 1  # 0-indexed

    if idx < 0 or idx >= len(lines):
        raise ValueError(f"Line {line_no} out of range (file has {len(lines)} lines)")

    target_line = lines[idx]
    indent = _get_indent(target_line)
    stripped = target_line.strip()

    # Detect the pattern and extract the expression to replace
    # Pattern 1: typed assignment — "var: Type = expr"
    typed_assign = re.match(r'^(\w+)\s*:\s*([^=]+?)\s*=\s*(.+)$', stripped)
    # Pattern 2: plain assignment — "var = expr"
    plain_assign = re.match(r'^(\w+)\s*=\s*(.+)$', stripped)
    # Pattern 3: return — "return expr"
    return_match = re.match(r'^return\s+(.+)$', stripped)

    # Escape double quotes in description
    safe_desc = description.replace('"', '\\"')

    if typed_assign:
        var_name = typed_assign.group(1)
        type_ann = typed_assign.group(2).strip()
        original = typed_assign.group(3).strip()
        safe_orig = original.replace('"', '\\"')
        new_line = f'{indent}{var_name}: {type_ann} = hole("{safe_desc} | original: {safe_orig}")'
    elif return_match:
        original = return_match.group(1).strip()
        safe_orig = original.replace('"', '\\"')
        new_line = f'{indent}return hole("{safe_desc} | original: {safe_orig}")'
    elif plain_assign:
        var_name = plain_assign.group(1)
        original = plain_assign.group(2).strip()
        safe_orig = original.replace('"', '\\"')
        new_line = f'{indent}{var_name} = hole("{safe_desc} | original: {safe_orig}")'
    else:
        # Bare expression or statement — wrap entirely
        safe_orig = stripped.replace('"', '\\"')
        new_line = f'{indent}hole("{safe_desc} | original: {safe_orig}")'

    lines[idx] = new_line
    return "\n".join(lines)


# ─── propagate ───────────────────────────────────────────────────────────────

def propagate(
    path: Path,
    backend: Optional[LanguageBackend] = None,
) -> list[Hole]:
    """Run type checker and auto-generate hole() stubs at error sites.

    This automates the "change a type → fix all errors" refactoring pattern:
      1. Run pyright/tsc on the file
      2. Parse each error to extract: line number, expected type, message
      3. At each error site, wrap the offending expression in hole()
         with a description derived from the error message

    The user workflow:
      1. Change a function signature or type definition by hand
      2. Run `mole propagate myfile.py`
      3. Errors become holes — now fill them with `mole fill`

    Returns the list of newly created holes.
    """
    if backend is None:
        backend = get_backend(detect_language(path))

    source = path.read_text()
    errors = backend.verify(source, path)

    if not errors:
        return []

    # Parse errors into (line_no, message) pairs
    error_sites: list[tuple[int, str]] = []
    # Common error format: "file.py:LINE:COL - error: message"
    # or pyright JSON: various formats
    for err in errors:
        # Try "path:line:col" format (pyright text output, tsc)
        m = re.match(r'.*?:(\d+):\d+\s*[-–]\s*(?:error|warning):\s*(.+)', err)
        if not m:
            # Try "L<N>: message" format (our backend's verify output)
            m = re.match(r'^L(\d+):\s*(.+)', err)
        if not m:
            # Try "line N" format
            m = re.match(r'.*?[Ll]ine\s+(\d+).*?:\s*(.+)', err)
        if not m:
            # Try bare "N: message" format
            m = re.match(r'^\s*(\d+)\s*:\s*(.+)', err)
        if m:
            line_no = int(m.group(1))
            message = m.group(2).strip()
            error_sites.append((line_no, message))

    if not error_sites:
        return []

    # Deduplicate by line number (keep first error per line)
    seen_lines: set[int] = set()
    unique_sites: list[tuple[int, str]] = []
    for line_no, msg in error_sites:
        if line_no not in seen_lines:
            seen_lines.add(line_no)
            unique_sites.append((line_no, msg))

    # Sort bottom-up (highest line first) so insertions don't shift later targets
    unique_sites.sort(key=lambda x: -x[0])

    lines = source.splitlines()
    new_holes: list[Hole] = []

    for line_no, error_msg in unique_sites:
        idx = line_no - 1
        if idx < 0 or idx >= len(lines):
            continue

        target = lines[idx]
        stripped = target.strip()
        indent = _get_indent(target)

        # Skip lines that already have hole()
        if "hole(" in stripped:
            continue

        # Skip blank lines, comments, imports, class/function definitions
        if not stripped or stripped.startswith(("#", "//", "/*", "import ", "from ")):
            continue
        if stripped.startswith(("def ", "class ", "async def ", "function ", "interface ")):
            continue

        # Derive a description from the error message
        # Clean up pyright-style messages for readability
        desc = error_msg
        # Truncate long messages
        if len(desc) > 80:
            desc = desc[:77] + "..."
        safe_desc = desc.replace('"', '\\"')

        # Detect pattern and wrap in hole()
        typed_assign = re.match(r'^(\w+)\s*:\s*([^=]+?)\s*=\s*(.+)$', stripped)
        plain_assign = re.match(r'^(\w+)\s*=\s*(.+)$', stripped)
        return_match = re.match(r'^return\s+(.+)$', stripped)

        if typed_assign:
            var_name = typed_assign.group(1)
            type_ann = typed_assign.group(2).strip()
            new_line = f'{indent}{var_name}: {type_ann} = hole("fix: {safe_desc}")'
        elif return_match:
            new_line = f'{indent}return hole("fix: {safe_desc}")'
        elif plain_assign:
            var_name = plain_assign.group(1)
            new_line = f'{indent}{var_name} = hole("fix: {safe_desc}")'
        else:
            new_line = f'{indent}hole("fix: {safe_desc}")  # was: {stripped}'

        lines[idx] = new_line
        new_holes.append(Hole(
            line_no=line_no,
            description=f"fix: {desc}",
            status=HoleStatus.UNFILLED,
        ))

    # Write modified source back
    modified_source = "\n".join(lines)
    path.write_text(modified_source)

    # Reverse so they're in top-down order
    new_holes.reverse()

    return new_holes


# ─── antiunify ───────────────────────────────────────────────────────────────

def antiunify(
    path: Path,
    backend: Optional[LanguageBackend] = None,
    min_group_size: int = 2,
) -> list[HoleGroup]:
    """Find structurally similar holes and group them by anti-unified type pattern.

    Anti-unification finds the most specific generalization of two types:
      - list[str] ∧ list[int] → list[T0]  (shared container, different element)
      - dict[str, User] ∧ dict[str, Event] → dict[str, T0]  (shared key type)
      - str ∧ int → T0  (no shared structure)

    Groups reveal compositional opportunities: when N holes share a pattern
    like list[T0], a single generic helper could serve all of them.

    Args:
        path: File to analyze.
        backend: Language backend (auto-detected if None).
        min_group_size: Minimum holes per group (default: 2).

    Returns:
        List of HoleGroups, sorted by size (largest first).
    """
    dfile = discover(path, backend)
    holes = dfile.holes

    # Filter to holes with type annotations (can't antiunify without types)
    typed_holes = [h for h in holes if h.expected_type]
    if len(typed_holes) < min_group_size:
        return []

    # Build groups by anti-unifying type strings
    groups: dict[str, HoleGroup] = {}

    for h in typed_holes:
        pattern, type_vars = _antiunify_type(h.expected_type)

        if pattern not in groups:
            groups[pattern] = HoleGroup(
                pattern=pattern,
                type_vars={k: [] for k in type_vars},
            )

        group = groups[pattern]
        group.holes.append(h)

        # Track concrete types for each type variable
        for var, concrete in type_vars.items():
            if var not in group.type_vars:
                group.type_vars[var] = []
            if concrete not in group.type_vars[var]:
                group.type_vars[var].append(concrete)

    # Filter by min group size and sort by size
    result = [g for g in groups.values() if g.size >= min_group_size]
    result.sort(key=lambda g: -g.size)

    return result


def _antiunify_type(type_str: str) -> tuple[str, dict[str, str]]:
    """Extract the structural pattern from a type string.

    Returns (pattern, type_vars) where:
      - pattern is the generalized form: "list[T0]", "dict[str, T0]", etc.
      - type_vars maps each variable to its concrete type

    Strategy:
      - Container types (list, dict, set, tuple, Optional) preserve the container
      - Inner types that are user-defined (CamelCase) become type variables T0, T1...
      - Primitive types (str, int, float, bool, bytes) stay concrete
      - Multiple generic params: dict[K, V] → each gets own variable if user-defined

    Examples:
      "list[User]"           → ("list[T0]", {"T0": "User"})
      "list[str]"            → ("list[str]", {})            — no generalization needed
      "dict[str, Event]"     → ("dict[str, T0]", {"T0": "Event"})
      "Optional[Page]"       → ("Optional[T0]", {"T0": "Page"})
      "str"                  → ("str", {})
      "User"                 → ("T0", {"T0": "User"})       — bare user type
    """
    s = type_str.strip()

    # Primitives — no generalization
    PRIMITIVES = {'str', 'int', 'float', 'bool', 'bytes', 'None', 'Any',
                  'string', 'number', 'boolean', 'void', 'never', 'unknown'}
    if s in PRIMITIVES or s.lower() in PRIMITIVES:
        return (s, {})

    # Bare user-defined type (CamelCase, not a builtin)
    if re.match(r'^[A-Z][A-Za-z0-9]*$', s) and s not in PRIMITIVES:
        return ("T0", {"T0": s})

    # Container with generic params: list[X], dict[X, Y], Optional[X], etc.
    bracket = s.find('[')
    if bracket > 0 and s.endswith(']'):
        container = s[:bracket]
        inner = s[bracket + 1:-1]

        # Split generic params (respecting nested brackets)
        params = _split_generic_params(inner)
        type_vars: dict[str, str] = {}
        var_idx = 0
        generalized_params: list[str] = []

        for param in params:
            param = param.strip()
            # User-defined type → generalize
            if re.match(r'^[A-Z][A-Za-z0-9]*$', param) and param not in PRIMITIVES:
                var_name = f"T{var_idx}"
                var_idx += 1
                type_vars[var_name] = param
                generalized_params.append(var_name)
            else:
                # Primitive or complex nested type — keep as-is
                generalized_params.append(param)

        pattern = f"{container}[{', '.join(generalized_params)}]"
        return (pattern, type_vars)

    # Fallback: no structure recognized
    return (s, {})


def _split_generic_params(inner: str) -> list[str]:
    """Split comma-separated generic parameters, respecting nested brackets.

    "str, int"          → ["str", "int"]
    "str, list[int]"    → ["str", "list[int]"]
    "dict[str, int], X" → ["dict[str, int]", "X"]
    """
    params: list[str] = []
    depth = 0
    current: list[str] = []

    for ch in inner:
        if ch in ('[', '<'):
            depth += 1
            current.append(ch)
        elif ch in (']', '>'):
            depth -= 1
            current.append(ch)
        elif ch == ',' and depth == 0:
            params.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)

    if current:
        params.append(''.join(current).strip())

    return params


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get_indent(line: str) -> str:
    """Extract leading whitespace from a line."""
    return line[:len(line) - len(line.lstrip())]


def _strip_reasoning_leaks(code: str) -> str:
    """Strip LLM reasoning leaks from fill output.

    Removes leading chain-of-thought text before actual code.
    Preserves code comments and #import: lines.
    """
    lines = code.strip().splitlines()
    if not lines:
        return code

    # Patterns that indicate LLM reasoning (not code)
    reasoning_patterns = [
        re.compile(r'^(Now |Let me |Here\'s |I\'ll |First,|Wait,|OK,|Sure)', re.IGNORECASE),
        re.compile(r'^(The |This |We |My |To |Since |Because )', re.IGNORECASE),
        re.compile(r'^(Looking at|Thinking about|Considering)', re.IGNORECASE),
    ]

    # Find first line that looks like code
    code_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        # These are always code/valid
        if stripped.startswith("#import:"):
            code_start = i
            break
        if stripped.startswith(("//", "#")) and "@mole:" in stripped:
            code_start = i
            break
        # Check if it's reasoning
        is_reasoning = False
        for pat in reasoning_patterns:
            if pat.match(stripped):
                is_reasoning = True
                break
        if not is_reasoning and stripped:
            code_start = i
            break

    return "\n".join(lines[code_start:])
