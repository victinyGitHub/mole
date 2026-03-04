"""mole — Generic language backend (tree-sitter only).

Fallback for any tree-sitter-supported language. No type checker.
Less precise than Python/TypeScript backends but works for any language.
"""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import Optional

from ..types import Hole, FunctionHeader

# Suppress tree-sitter FutureWarning
warnings.filterwarnings("ignore", category=FutureWarning, module="tree_sitter")

try:
    from tree_sitter_languages import get_parser as _ts_get_parser
    HAS_TS = True
except ImportError:
    HAS_TS = False


# ─── Tree-sitter Helpers ─────────────────────────────────────────────────────

def _get_parser(language: str):
    """Get tree-sitter parser for a language."""
    if not HAS_TS:
        raise RuntimeError("tree-sitter-languages not installed")
    # Map common names to tree-sitter grammar names
    lang_map = {
        "shell": "bash",
        "cpp": "cpp",
    }
    ts_lang = lang_map.get(language, language)
    return _ts_get_parser(ts_lang)


def _walk_nodes(node, node_type: str) -> list:
    """Recursively collect all nodes of a given type."""
    results = []
    if node.type == node_type:
        results.append(node)
    for child in node.children:
        results.extend(_walk_nodes(child, node_type))
    return results


def _walk_any_type(node, types: set[str]) -> list:
    """Recursively collect nodes matching any of the given types."""
    results = []
    if node.type in types:
        results.append(node)
    for child in node.children:
        results.extend(_walk_any_type(child, types))
    return results


def _node_text(node) -> str:
    """Get decoded text of a tree-sitter node."""
    return node.text.decode("utf-8")


# ─── Node type patterns for various languages ─────────────────────────────────

# Function-like node types across languages
FUNC_NODE_TYPES = {
    "function_definition",       # Python
    "function_declaration",      # TS/JS/Go
    "method_definition",         # TS/JS
    "arrow_function",           # TS/JS
    "function_item",            # Rust
    "method_declaration",       # Go
    "function",                 # Generic
}

# Class/interface/struct-like types
TYPE_DEF_NODE_TYPES = {
    "class_definition",          # Python
    "class_declaration",         # TS/JS
    "interface_declaration",     # TS
    "type_alias_declaration",    # TS
    "struct_item",              # Rust
    "enum_item",                # Rust
    "trait_item",               # Rust
    "type_declaration",         # Go
}

# Import-like types
IMPORT_NODE_TYPES = {
    "import_from_statement",     # Python
    "import_statement",          # Python / TS/JS
    "use_declaration",          # Rust
    "import_declaration",       # Go
}

# Comment types
COMMENT_NODE_TYPES = {"comment", "line_comment", "block_comment"}


# ─── Generic Backend ─────────────────────────────────────────────────────────

class GenericBackend:
    """Generic tree-sitter backend — works for any supported language.

    No type checker. Uses heuristic node matching for hole discovery.
    """

    def __init__(self, language: str):
        self.language = language
        try:
            self._parser = _get_parser(language)
        except Exception:
            self._parser = None

    def _parse(self, source: str):
        """Parse source with the language-specific parser."""
        if self._parser is None:
            raise RuntimeError(f"No tree-sitter parser for {self.language}")
        return self._parser.parse(source.encode("utf-8"))

    def find_holes(self, source: str) -> list[Hole]:
        """Find all hole() calls via tree-sitter.

        Uses generic call node detection — looks for any function call
        where the function name is 'hole'.
        """
        if self._parser is None:
            # Last resort: regex fallback
            return self._find_holes_regex(source)

        tree = self._parse(source)
        # Search for any call-like node
        call_types = {"call", "call_expression", "function_call"}
        call_nodes = _walk_any_type(tree.root_node, call_types)
        holes: list[Hole] = []

        for call in call_nodes:
            # Check if it's a hole() call by checking text
            text = _node_text(call)
            if not text.startswith("hole(") and not text.startswith("hole ("):
                continue

            line_no = call.start_point[0] + 1

            # Try to extract description string
            description = self._extract_description(call)

            holes.append(Hole(
                line_no=line_no,
                description=description,
                is_bare=True,  # Generic backend can't reliably classify
            ))

        return holes

    def _find_holes_regex(self, source: str) -> list[Hole]:
        """Regex fallback for hole discovery when parser unavailable."""
        import re
        pattern = re.compile(r'hole\(\s*["\'](.+?)["\']\s*\)')
        holes: list[Hole] = []
        for i, line in enumerate(source.splitlines()):
            m = pattern.search(line)
            if m:
                holes.append(Hole(
                    line_no=i + 1,
                    description=m.group(1),
                    is_bare=True,
                ))
        return holes

    def _extract_description(self, call_node) -> str:
        """Try to extract description string from hole() call arguments."""
        # Walk children looking for string nodes
        for child in call_node.children:
            if "argument" in child.type or "parameter" in child.type:
                for arg in child.children:
                    if "string" in arg.type:
                        raw = _node_text(arg)
                        return raw.strip("'\"")
            if "string" in child.type:
                raw = _node_text(child)
                return raw.strip("'\"")
        return ""

    def extract_types(self, path: Path, holes: list[Hole]) -> list[Hole]:
        """No type checker available — return holes unchanged."""
        return holes

    def verify(self, source: str, path: Path) -> list[str]:
        """No type checker — return empty errors list."""
        return []

    def extract_type_definitions(self, source: str) -> str:
        """Extract type/class definitions using generic node matching."""
        if self._parser is None:
            return ""
        tree = self._parse(source)
        type_nodes = _walk_any_type(tree.root_node, TYPE_DEF_NODE_TYPES)
        blocks: list[str] = []
        for node in type_nodes:
            blocks.append(_node_text(node))
        return "\n\n".join(blocks)

    def extract_function_signatures(self, source: str) -> str:
        """Extract function signatures using generic node matching."""
        if self._parser is None:
            return ""
        tree = self._parse(source)
        func_nodes = _walk_any_type(tree.root_node, FUNC_NODE_TYPES)
        sigs: list[str] = []
        for node in func_nodes:
            # Take first line as signature approximation
            text = _node_text(node)
            first_line = text.split("\n")[0]
            sigs.append(first_line + " ...")
        return "\n".join(sigs)

    def extract_function_headers(self, source: str) -> list[FunctionHeader]:
        """Extract function headers generically — limited type info.

        Generic backend can find function nodes but can't reliably parse
        parameter types or return types across all languages. Returns headers
        with signature text only, no parsed types. These will score 0 in
        type-compatibility ranking but are still useful as reference.
        """
        if self._parser is None:
            return []
        tree = self._parse(source)
        func_nodes = _walk_any_type(tree.root_node, FUNC_NODE_TYPES)
        headers: list[FunctionHeader] = []
        for node in func_nodes:
            text = _node_text(node)
            first_line = text.split("\n")[0]
            sig = first_line + " ..."
            # Try to extract function name from first line
            import re
            name_match = re.match(r'(?:def|func|fn|function)\s+(\w+)', first_line)
            name = name_match.group(1) if name_match else first_line.split("(")[0].strip()
            headers.append(FunctionHeader(
                name=name,
                signature=sig,
                return_type=None,
                param_types=[],
            ))
        return headers

    def extract_scope_vars(self, source: str, line_no: int) -> list[tuple[str, str]]:
        """Generic scope var extraction — limited precision."""
        # Without a type system, best effort is regex on assignments
        import re
        vars_found: list[tuple[str, str]] = []
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if i + 1 >= line_no:
                break
            # Try to match typed assignment patterns
            # Python: name: type = ...
            m = re.match(r'\s*(\w+)\s*:\s*(\S+)\s*=', line)
            if m:
                vars_found.append((m.group(1), m.group(2)))
                continue
            # TypeScript: const/let/var name: type = ...
            m = re.match(r'\s*(?:const|let|var)\s+(\w+)\s*:\s*(\S+)\s*=', line)
            if m:
                vars_found.append((m.group(1), m.group(2)))
        return vars_found

    def extract_imports(self, source: str) -> list[tuple[str, str]]:
        """Extract import-like nodes generically."""
        if self._parser is None:
            return []
        tree = self._parse(source)
        import_nodes = _walk_any_type(tree.root_node, IMPORT_NODE_TYPES)
        imports: list[tuple[str, str]] = []
        for node in import_nodes:
            # Use the full import text as both module and names
            text = _node_text(node).strip()
            imports.append((text, ""))
        return imports

    def resolve_import_path(self, module: str, base_dir: Path) -> Optional[Path]:
        """Generic import resolution — no language-specific logic."""
        return None  # Can't resolve without language-specific rules

    def find_enclosing_block(self, source: str, line_no: int) -> str:
        """Find enclosing function/class block generically."""
        if self._parser is None:
            return self._context_window(source, line_no)

        tree = self._parse(source)
        best = None

        def _search(node):
            nonlocal best
            start = node.start_point[0] + 1
            end = node.end_point[0] + 1
            if start <= line_no <= end:
                if node.type in FUNC_NODE_TYPES | TYPE_DEF_NODE_TYPES:
                    best = node
                for child in node.children:
                    _search(child)

        _search(tree.root_node)
        if best:
            return _node_text(best)
        return self._context_window(source, line_no)

    def _context_window(self, source: str, line_no: int, radius: int = 10) -> str:
        """Get a window of lines around line_no."""
        lines = source.splitlines()
        start = max(0, line_no - 1 - radius)
        end = min(len(lines), line_no + radius)
        return "\n".join(lines[start:end])
