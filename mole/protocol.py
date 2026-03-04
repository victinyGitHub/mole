"""mole — @mole: comment protocol.

Language-agnostic structured comments that carry behavioral specs
with the code. Every language has comments — this is the universal protocol.

Format:
    # @mole:behavior parse each row, skip header, validate email
    # @mole:requires file exists and is valid CSV
    # @mole:ensures all records have non-empty .email
    # @mole:type list[Record]
    # @mole:filled-by sonnet@2026-03-03
    # @mole:expanded-from L15
    # @mole:approach "csv stdlib + manual validation"

The @mole: prefix is the protocol identifier.
Tags after the colon are the structured data.
Everything after the tag is freeform text.
"""
from __future__ import annotations

import re
import warnings
from typing import Optional

from .types import Hole, BehaviorSpec

# Suppress tree-sitter FutureWarning about Language(path, name) deprecation
warnings.filterwarnings("ignore", category=FutureWarning, module="tree_sitter")

try:
    from tree_sitter_languages import get_parser as _ts_get_parser
    HAS_TREE_SITTER = True
except ImportError:
    HAS_TREE_SITTER = False

# Comment node types vary by language grammar
# Most use "comment", rust uses "line_comment" / "block_comment"
COMMENT_NODE_TYPES = {"comment", "line_comment", "block_comment"}

# Map language names to tree-sitter grammar names
# (most are identity, but some differ)
TS_LANGUAGE_MAP: dict[str, str] = {
    "python": "python",
    "typescript": "typescript",
    "javascript": "javascript",
    "rust": "rust",
    "go": "go",
    "c": "c",
    "cpp": "cpp",
    "java": "java",
    "ruby": "ruby",
    "lua": "lua",
    "shell": "bash",
}


# ─── Known Tags ───────────────────────────────────────────────────────────────

MOLE_TAGS = {
    "behavior",       # what this code should do
    "requires",       # preconditions
    "ensures",        # postconditions
    "type",           # expected type (auto-generated)
    "filled-by",      # provenance
    "expanded-from",  # parent hole
    "approach",       # which expansion approach
}


# ─── Parser ───────────────────────────────────────────────────────────────────

def parse_mole_comments(
    source: str,
    comment_prefix: str = "#",
    language: str = "python",
) -> dict[int, dict[str, str]]:
    """Parse all @mole: comments from source using tree-sitter AST.

    Uses tree-sitter to find comment nodes (language-agnostic), then extracts
    @mole: tags via regex on the comment text. Falls back to line-based
    regex scanning if tree-sitter is unavailable.

    Returns a dict mapping line_no → {tag: value} for each annotated location.
    A location is the first non-comment, non-blank line AFTER a block of
    @mole: comments.

    Args:
        source: The source code to parse.
        comment_prefix: Language-specific comment prefix ("#" for Python,
                       "//" for TS/JS/Rust/Go/C++, etc.)
        language: Language name for tree-sitter grammar selection.

    Returns:
        {line_no: {"behavior": "...", "ensures": "...", ...}}
    """
    # Try tree-sitter first, fall back to regex
    ts_lang: str = TS_LANGUAGE_MAP.get(language, language)
    if HAS_TREE_SITTER and ts_lang:
        try:
            return _parse_with_tree_sitter(source, ts_lang)
        except Exception:
            pass  # Fall through to regex fallback

    return _parse_with_regex(source, comment_prefix)


def _parse_with_tree_sitter(
    source: str,
    ts_language: str,
) -> dict[int, dict[str, str]]:
    """Parse @mole: comments using tree-sitter AST traversal.

    Walks the AST to collect comment nodes, extracts @mole: tags,
    and maps each tag block to the next non-comment code line.
    """
    # Parse source into AST
    parser = _ts_get_parser(ts_language)
    tree = parser.parse(source.encode("utf-8"))

    # Collect all comment nodes from the AST (recursive walk)
    comment_nodes: list[tuple[int, str]] = []
    _collect_comments(tree.root_node, comment_nodes)

    # Sort by line number (tree-sitter walk order is usually correct,
    # but sort to be safe)
    comment_nodes.sort(key=lambda x: x[0])

    # Pattern to extract @mole:tag value from comment text
    # Works regardless of comment prefix — we're matching the content
    mole_re: re.Pattern[str] = re.compile(
        r"@mole:(\S+)\s*(.*?)\s*$"
    )

    # Build a lookup: line_no → comment text (from tree-sitter AST)
    comment_map: dict[int, str] = {ln: txt for ln, txt in comment_nodes}
    # Set of lines that are comments (for skipping during code-line search)
    comment_line_set: set[int] = set(comment_map.keys())

    # Walk ALL source lines sequentially — same logic as regex fallback
    # but using tree-sitter's comment detection instead of prefix matching
    result: dict[int, dict[str, str]] = {}
    source_lines: list[str] = source.splitlines()
    pending_tags: dict[str, str] = {}

    for i, line in enumerate(source_lines):
        line_no: int = i + 1  # 1-indexed
        stripped: str = line.strip()

        # Is this line a comment? (tree-sitter told us)
        if line_no in comment_map:
            # Check if it contains a @mole: tag
            m = mole_re.search(comment_map[line_no])
            if m:
                tag: str = m.group(1)
                value: str = m.group(2)
                if tag in MOLE_TAGS:
                    pending_tags[tag] = value
            # Either way, it's a comment — don't flush
            continue

        # Blank line — don't flush, part of the block gap
        if stripped == "":
            continue

        # Code line — flush any accumulated tags to this line
        if pending_tags:
            result[line_no] = dict(pending_tags)
            pending_tags = {}

    return result


def _collect_comments(
    node: object,  # tree_sitter.Node — using object for import-safety
    out: list[tuple[int, str]],
) -> None:
    """Recursively walk AST and collect comment nodes as (line_no, text)."""
    # Check if this node is a comment type
    if hasattr(node, "type") and node.type in COMMENT_NODE_TYPES:  # type: ignore
        # 1-indexed line number, decoded comment text
        line_no: int = node.start_point[0] + 1  # type: ignore
        text: str = node.text.decode("utf-8")  # type: ignore
        out.append((line_no, text))
    # Recurse into children
    for child in node.children:  # type: ignore
        _collect_comments(child, out)


def _find_next_code_line(
    lines: list[str],
    after_line: int,
    comment_lines: set[int],
) -> int:
    """Find the next non-comment, non-blank line after after_line (1-indexed).

    Returns the 1-indexed line number, or 0 if none found.
    """
    for i in range(after_line, len(lines)):
        line_no: int = i + 1  # 1-indexed
        stripped: str = lines[i].strip()
        # Skip blank lines and lines that are comments
        if stripped == "" or line_no in comment_lines:
            continue
        return line_no
    return 0


def _parse_with_regex(
    source: str,
    comment_prefix: str,
) -> dict[int, dict[str, str]]:
    """Fallback parser using line-by-line regex scanning.

    Used when tree-sitter is not available for the target language.
    """
    # Escape the comment prefix for regex (handles // etc.)
    escaped_prefix: str = re.escape(comment_prefix)
    # Pattern: <prefix> @mole:<tag> <value>
    mole_re: re.Pattern[str] = re.compile(
        rf"^\s*{escaped_prefix}\s*@mole:(\S+)\s*(.*?)\s*$"
    )

    result: dict[int, dict[str, str]] = {}
    lines: list[str] = source.splitlines()
    # Accumulate @mole: tags in the current comment block
    pending_tags: dict[str, str] = {}

    for i, line in enumerate(lines):
        line_no: int = i + 1  # 1-indexed
        stripped: str = line.strip()

        m = mole_re.match(line)
        if m:
            # This line is a @mole: comment — accumulate the tag
            tag: str = m.group(1)
            value: str = m.group(2)
            if tag in MOLE_TAGS:
                pending_tags[tag] = value
            # Unknown tags are silently ignored
            continue

        # Check if this is a regular comment or blank line (part of the block)
        if stripped == "" or stripped.startswith(comment_prefix):
            # Blank or non-@mole comment — don't break the block
            continue

        # First non-comment, non-blank line — this is the annotated location
        if pending_tags:
            result[line_no] = dict(pending_tags)
            pending_tags = {}

    return result


def format_mole_comments(
    spec: BehaviorSpec,
    comment_prefix: str = "#",
    extra_tags: Optional[dict[str, str]] = None,
) -> str:
    """Format a BehaviorSpec as @mole: comment block.

    Args:
        spec: The behavioral spec to format.
        comment_prefix: Language-specific comment prefix.
        extra_tags: Additional tags to include (e.g. filled-by, type).

    Returns:
        Multi-line string of structured comments, ready to insert above code.
    """
    lines = []

    if spec.behavior:
        lines.append(f"{comment_prefix} @mole:behavior {spec.behavior}")
    if spec.requires:
        lines.append(f"{comment_prefix} @mole:requires {spec.requires}")
    if spec.ensures:
        lines.append(f"{comment_prefix} @mole:ensures {spec.ensures}")
    if spec.approach:
        lines.append(f"{comment_prefix} @mole:approach {spec.approach}")

    if extra_tags:
        for tag, value in extra_tags.items():
            lines.append(f"{comment_prefix} @mole:{tag} {value}")

    return "\n".join(lines)


def attach_specs_to_holes(
    holes: list[Hole],
    parsed_comments: dict[int, dict[str, str]],
) -> list[Hole]:
    """Attach parsed @mole: specs to their corresponding holes.

    Matches by line number — checks exact line, then searches a small
    window above (up to 5 lines) to handle cases where @mole: comments
    are above the enclosing function, not directly above the hole.
    """
    for h in holes:
        # Check exact line match first
        specs = parsed_comments.get(h.line_no, {})

        # If no exact match, search a window above the hole (up to 5 lines)
        if not specs:
            for offset in range(1, 6):
                candidate = h.line_no - offset
                if candidate in parsed_comments:
                    specs = parsed_comments[candidate]
                    break

        if specs:
            h.behavior = BehaviorSpec(
                behavior=specs.get("behavior"),
                requires=specs.get("requires"),
                ensures=specs.get("ensures"),
                approach=specs.get("approach"),
            )
            if "type" in specs:
                h.expected_type = specs["type"]
    return holes


def comment_prefix_for_language(language: str) -> str:
    """Return the comment prefix for a language.

    Simple lookup — covers the common cases.
    """
    return {
        "python": "#",
        "typescript": "//",
        "javascript": "//",
        "rust": "//",
        "go": "//",
        "c": "//",
        "cpp": "//",
        "java": "//",
        "ruby": "#",
        "shell": "#",
        "lua": "--",
    }.get(language, "#")
