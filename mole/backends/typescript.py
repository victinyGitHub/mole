"""mole — TypeScript/JavaScript language backend.

Tree-sitter for AST analysis, tsc for type checking.
NO python ast module. Tree-sitter is the only AST engine.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import warnings
from pathlib import Path
from typing import Optional

from ..types import Hole, FunctionHeader

# Suppress tree-sitter FutureWarning
warnings.filterwarnings("ignore", category=FutureWarning, module="tree_sitter")

try:
    from tree_sitter_languages import get_parser as _ts_get_parser
    _PARSER_TS = _ts_get_parser("typescript")
    _PARSER_JS = _ts_get_parser("javascript")
except ImportError:
    _PARSER_TS = None
    _PARSER_JS = None


# ─── Tree-sitter Helpers ─────────────────────────────────────────────────────

def _parse(source: str, language: str = "typescript"):
    """Parse TypeScript/JavaScript source into tree-sitter AST."""
    parser = _PARSER_JS if language == "javascript" else _PARSER_TS
    if parser is None:
        raise RuntimeError("tree-sitter-languages not installed")
    return parser.parse(source.encode("utf-8"))


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


def _find_child_by_type(node, child_type: str):
    """Find first direct child of a given type."""
    for child in node.children:
        if child.type == child_type:
            return child
    return None


def _find_child_by_field(node, field_name: str):
    """Find child node by field name."""
    return node.child_by_field_name(field_name)


# ─── TypeScript Backend ──────────────────────────────────────────────────────

class TypeScriptBackend:
    """TypeScript/JavaScript backend — tree-sitter + tsc."""
    language: str = "typescript"

    def __init__(self, lang: str = "typescript"):
        """Initialize with specific language variant."""
        self.language = lang

    def find_holes(self, source: str) -> list[Hole]:
        """Find all hole() calls in TypeScript source via tree-sitter.

        Classifies each as assignment, return, or bare.
        """
        tree = _parse(source, self.language)
        # Find all call_expression nodes
        call_nodes = _walk_nodes(tree.root_node, "call_expression")
        holes: list[Hole] = []

        for call in call_nodes:
            # Check if function name is 'hole'
            func_node = _find_child_by_field(call, "function")
            if func_node is None or _node_text(func_node) != "hole":
                continue

            # Extract description from first argument
            args_node = _find_child_by_field(call, "arguments")
            description = ""
            if args_node:
                for arg_child in args_node.children:
                    if arg_child.type == "string":
                        # Strip quotes — TS strings use "..." or '...'
                        raw = _node_text(arg_child)
                        description = raw.strip("'\"")
                        break

            line_no = call.start_point[0] + 1  # 1-indexed

            # Classify by walking up the parent chain
            is_bare = True
            is_return = False
            var_name: Optional[str] = None
            expected_type: Optional[str] = None

            parent = call.parent
            if parent and parent.type == "variable_declarator":
                # const x: T = hole(...) — assignment
                is_bare = False
                name_node = _find_child_by_field(parent, "name")
                if name_node:
                    var_name = _node_text(name_node)
                # Check for type annotation
                type_anno = _find_child_by_type(parent, "type_annotation")
                if type_anno:
                    # type_annotation contains the actual type as a child
                    expected_type = _node_text(type_anno).lstrip(": ").strip()

            elif parent and parent.type == "return_statement":
                # return hole(...) — return hole
                is_bare = False
                is_return = True
                expected_type = self._find_return_type(parent)

            elif parent and parent.type == "expression_statement":
                is_bare = True

            holes.append(Hole(
                line_no=line_no,
                description=description,
                expected_type=expected_type,
                var_name=var_name,
                is_bare=is_bare,
                is_return=is_return,
            ))

        return holes

    def _find_return_type(self, node) -> Optional[str]:
        """Walk up from return_statement to find enclosing function's return type."""
        current = node.parent
        func_types = {
            "function_declaration", "method_definition",
            "arrow_function", "function",
        }
        while current:
            if current.type in func_types:
                # Look for return type annotation
                ret_type = _find_child_by_type(current, "type_annotation")
                if ret_type:
                    return _node_text(ret_type).lstrip(": ").strip()
                return None
            current = current.parent
        return None

    def get_annotation(self, source: str, line_no: int) -> Optional[str]:
        """Extract type annotation at a hole site via tree-sitter.

        Three cases handled:
        - Assignment: `const x: string[] = hole(...)` → returns "string[]"
        - Return: `return hole(...)` inside `function f(): string {` → returns "string"
        - Bare: `hole(...)` → returns None

        This is Tier 2 of the type extraction chain.
        """
        tree = _parse(source, self.language)
        # Find call_expression nodes at the target line
        call_nodes = _walk_nodes(tree.root_node, "call_expression")

        for call in call_nodes:
            if call.start_point[0] + 1 != line_no:
                continue
            func_node = _find_child_by_field(call, "function")
            if func_node is None or _node_text(func_node) != "hole":
                continue

            # Found the hole() at this line
            parent = call.parent
            if parent and parent.type == "variable_declarator":
                type_anno = _find_child_by_type(parent, "type_annotation")
                if type_anno:
                    return _node_text(type_anno).lstrip(": ").strip()
            elif parent and parent.type == "return_statement":
                return self._find_return_type(parent)

        return None

    def extract_types(self, path: Path, holes: list[Hole]) -> list[Hole]:
        """Three-tier type extraction:
          Tier 2: tree-sitter get_annotation() (fast, no subprocess)
          Tier 3: tsc sentinel trick (for unannotated holes)
        """
        if not holes:
            return holes

        source = path.read_text()

        # Tier 2: tree-sitter direct annotation extraction
        for h in holes:
            if not h.expected_type:
                ann = self.get_annotation(source, h.line_no)
                if ann:
                    h.expected_type = ann

        untyped = [h for h in holes if not h.expected_type]
        if not untyped:
            return holes

        # Add sentinel type and replace hole() calls
        sentinel_def = "class _MoleHole { constructor(...args: any[]) {} }\n"
        sentinel_source = sentinel_def + source

        # Replace hole(...) with new _MoleHole(...)
        sentinel_source = re.sub(
            r'\bhole\s*\(',
            'new _MoleHole(',
            sentinel_source,
        )
        # Remove the declare function hole line if present
        sentinel_source = re.sub(
            r'declare\s+function\s+hole\s*\([^)]*\)\s*:\s*any\s*;?\s*\n?',
            '',
            sentinel_source,
        )

        line_offset = 1  # Added 1 line at top

        # Write temp file and run tsc
        suffix = ".ts" if self.language == "typescript" else ".js"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, delete=False, dir=path.parent
        ) as tmp:
            tmp.write(sentinel_source)
            tmp_path = Path(tmp.name)

        try:
            result = subprocess.run(
                ["tsc", "--noEmit", "--strict",
                 "--lib", "es2015,dom", "--target", "es2015",
                 str(tmp_path)],
                capture_output=True, text=True, timeout=30,
            )
            if result.stdout:
                self._apply_tsc_types(holes, result.stdout, line_offset, tmp_path.name)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass  # Graceful degradation
        finally:
            tmp_path.unlink(missing_ok=True)

        return holes

    def _apply_tsc_types(
        self,
        holes: list[Hole],
        tsc_output: str,
        line_offset: int,
        filename: str,
    ) -> None:
        """Extract expected types from tsc error output.

        tsc errors look like:
        file.ts(5,10): error TS2322: Type '_MoleHole' is not assignable to type 'string'.
        """
        # Pattern: file(line,col): error TSxxxx: Type '_MoleHole' is not assignable to type 'X'.
        type_pattern = re.compile(
            r"\((\d+),\d+\):\s*error\s+TS\d+:\s*Type\s+'_MoleHole'\s+is\s+not\s+assignable\s+to\s+type\s+'(.+?)'"
        )

        for m in type_pattern.finditer(tsc_output):
            error_line = int(m.group(1))
            expected = m.group(2)
            # Adjust for sentinel offset
            original_line = error_line - line_offset

            for h in holes:
                if h.line_no == original_line and not h.expected_type:
                    h.expected_type = expected
                    break

    def verify(self, source: str, path: Path) -> list[str]:
        """Run tsc --noEmit on source, return error strings."""
        suffix = ".ts" if self.language == "typescript" else ".js"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, delete=False, dir=path.parent
        ) as tmp:
            tmp.write(source)
            tmp_path = Path(tmp.name)

        try:
            result = subprocess.run(
                ["tsc", "--noEmit", "--strict",
                 "--lib", "es2015,dom", "--target", "es2015",
                 str(tmp_path)],
                capture_output=True, text=True, timeout=30,
            )
            errors: list[str] = []
            if result.stdout:
                # Parse tsc error format: file(line,col): error TSxxxx: message
                err_pattern = re.compile(r"\((\d+),(\d+)\):\s*error\s+(TS\d+):\s*(.+)")
                for m in err_pattern.finditer(result.stdout):
                    line = m.group(1)
                    msg = m.group(4)
                    # Filter out "hole" related errors
                    if "hole" in msg.lower() and "not defined" in msg.lower():
                        continue
                    errors.append(f"L{line}: {msg}")
            return errors
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []  # Graceful degradation
        finally:
            tmp_path.unlink(missing_ok=True)

    def extract_type_definitions(self, source: str) -> str:
        """Extract interface/type/class definitions via tree-sitter."""
        tree = _parse(source, self.language)
        type_defs = _walk_any_type(
            tree.root_node,
            {
                "interface_declaration",
                "type_alias_declaration",
                "class_declaration",
                "enum_declaration",
            },
        )
        blocks: list[str] = []
        for node in type_defs:
            blocks.append(_node_text(node))
        return "\n\n".join(blocks)

    def extract_function_signatures(self, source: str) -> str:
        """Extract function/method signatures via tree-sitter."""
        tree = _parse(source, self.language)
        func_nodes = _walk_any_type(
            tree.root_node,
            {"function_declaration", "method_definition"},
        )
        sigs: list[str] = []
        for node in func_nodes:
            # Extract just the signature line (up to the opening brace)
            text = _node_text(node)
            # Find the opening brace — sig is everything before it
            brace_idx = text.find("{")
            if brace_idx > 0:
                sig = text[:brace_idx].strip() + " { ... }"
            else:
                sig = text.split("\n")[0] + " ..."
            sigs.append(sig)
        return "\n".join(sigs)

    def extract_function_headers(self, source: str) -> list[FunctionHeader]:
        """Extract structured function headers with parsed type info via tree-sitter.

        Parses each function/method declaration to extract:
        - name: function name
        - signature: truncated sig (up to opening brace)
        - return_type: parsed return type annotation (or None)
        - param_types: list of parameter type annotations
        """
        tree = _parse(source, self.language)
        func_nodes = _walk_any_type(
            tree.root_node,
            {"function_declaration", "method_definition"},
        )
        headers: list[FunctionHeader] = []

        for node in func_nodes:
            # Extract function name
            name_node = _find_child_by_field(node, "name")
            if not name_node:
                # method_definition may use property_identifier
                name_node = _find_child_by_type(node, "property_identifier")
            if not name_node:
                continue

            name = _node_text(name_node)

            # Build signature string (truncated at opening brace)
            text = _node_text(node)
            brace_idx = text.find("{")
            if brace_idx > 0:
                sig = text[:brace_idx].strip() + " { ... }"
            else:
                sig = text.split("\n")[0] + " ..."

            # Extract return type annotation from the function node
            return_type: Optional[str] = None
            # TypeScript: return type is a type_annotation child of the function
            ret_anno = _find_child_by_type(node, "type_annotation")
            if ret_anno:
                # The type_annotation contains ": ReturnType"
                # But for functions, we need the one that's NOT inside formal_parameters
                # Check: is this type_annotation a direct child (return type) or nested (param)?
                if ret_anno.parent == node:
                    return_type = _node_text(ret_anno).lstrip(": ").strip()
                else:
                    # Look for type_annotation that's a direct child of the func node
                    for child in node.children:
                        if child.type == "type_annotation":
                            return_type = _node_text(child).lstrip(": ").strip()
                            break

            # Extract parameter types from formal_parameters
            param_types: list[str] = []
            params_node = _find_child_by_type(node, "formal_parameters")
            if params_node:
                param_nodes = _walk_any_type(
                    params_node,
                    {"required_parameter", "optional_parameter"},
                )
                for p in param_nodes:
                    type_anno = _find_child_by_type(p, "type_annotation")
                    if type_anno:
                        param_types.append(
                            _node_text(type_anno).lstrip(": ").strip()
                        )

            headers.append(FunctionHeader(
                name=name,
                signature=sig,
                return_type=return_type,
                param_types=param_types,
            ))

        return headers

    def extract_scope_vars(self, source: str, line_no: int) -> list[tuple[str, str]]:
        """Extract typed variables in scope at line_no."""
        tree = _parse(source, self.language)
        enclosing = self._find_enclosing_func_node(tree.root_node, line_no)
        if not enclosing:
            enclosing = tree.root_node

        # Find variable_declarator nodes with type annotations
        declarators = _walk_nodes(enclosing, "variable_declarator")
        scope_vars: list[tuple[str, str]] = []

        for decl in declarators:
            decl_line = decl.start_point[0] + 1
            if decl_line >= line_no:
                continue  # Only vars before the hole

            name_node = _find_child_by_field(decl, "name")
            type_anno = _find_child_by_type(decl, "type_annotation")
            if name_node and type_anno:
                name = _node_text(name_node)
                type_str = _node_text(type_anno).lstrip(": ").strip()
                scope_vars.append((name, type_str))

        # Also extract function parameters
        params = self._extract_params(enclosing)
        scope_vars.extend(params)

        return scope_vars

    def _extract_params(self, func_node) -> list[tuple[str, str]]:
        """Extract typed parameters from enclosing function."""
        params: list[tuple[str, str]] = []
        # Find formal_parameters or required_parameter nodes
        param_nodes = _walk_any_type(
            func_node,
            {"required_parameter", "optional_parameter"},
        )
        for p in param_nodes:
            # Only direct params of this function, not nested
            if p.parent and p.parent.type == "formal_parameters":
                name_node = _find_child_by_type(p, "identifier")
                type_anno = _find_child_by_type(p, "type_annotation")
                if name_node and type_anno:
                    params.append((
                        _node_text(name_node),
                        _node_text(type_anno).lstrip(": ").strip(),
                    ))
        return params

    def extract_imports(self, source: str) -> list[tuple[str, str]]:
        """Extract import statements as (module, names) tuples."""
        tree = _parse(source, self.language)
        import_nodes = _walk_nodes(tree.root_node, "import_statement")
        imports: list[tuple[str, str]] = []

        for imp in import_nodes:
            # Extract source module (the string after 'from')
            source_node = _find_child_by_field(imp, "source")
            module = ""
            if source_node:
                module = _node_text(source_node).strip("'\"")

            # Extract imported names
            names: list[str] = []
            clause = _find_child_by_type(imp, "import_clause")
            if clause:
                # Named imports: import { A, B } from '...'
                named = _find_child_by_type(clause, "named_imports")
                if named:
                    specs = _walk_nodes(named, "import_specifier")
                    for spec in specs:
                        name_node = _find_child_by_field(spec, "name")
                        if name_node:
                            names.append(_node_text(name_node))
                # Default import: import X from '...'
                default = _find_child_by_type(clause, "identifier")
                if default:
                    names.append(_node_text(default))
                # Namespace import: import * as X from '...'
                ns = _find_child_by_type(clause, "namespace_import")
                if ns:
                    names.append("*")

            imports.append((module, ", ".join(names) if names else "*"))

        return imports

    def resolve_import_path(self, module: str, base_dir: Path) -> Optional[Path]:
        """Resolve TypeScript import to file path.

        Handles relative paths (./ and ../), tries .ts, .tsx, /index.ts extensions.
        """
        if not module or not module.startswith("."):
            return None  # Only resolve relative imports

        # Resolve relative path
        candidate = (base_dir / module).resolve()

        # Try common TypeScript extensions
        for ext in [".ts", ".tsx", ".js", ".jsx"]:
            if candidate.with_suffix(ext).is_file():
                return candidate.with_suffix(ext)

        # Try index file
        for ext in [".ts", ".tsx", ".js"]:
            index = candidate / f"index{ext}"
            if index.is_file():
                return index

        return None

    def find_enclosing_block(self, source: str, line_no: int) -> str:
        """Find enclosing function/class block around a line."""
        tree = _parse(source, self.language)
        node = self._find_enclosing_func_node(tree.root_node, line_no)
        if node and node != tree.root_node:
            return _node_text(node)
        # Fallback: context window
        return self._context_window(source, line_no, radius=10)

    def _find_enclosing_func_node(self, root, line_no: int):
        """Find innermost function/class node containing line_no."""
        best = None
        func_types = {
            "function_declaration", "method_definition",
            "arrow_function", "class_declaration",
        }

        def _search(node):
            nonlocal best
            start = node.start_point[0] + 1
            end = node.end_point[0] + 1
            if start <= line_no <= end:
                if node.type in func_types:
                    best = node
                for child in node.children:
                    _search(child)

        _search(root)
        return best

    def _context_window(self, source: str, line_no: int, radius: int = 10) -> str:
        """Get a window of lines around line_no."""
        lines = source.splitlines()
        start = max(0, line_no - 1 - radius)
        end = min(len(lines), line_no + radius)
        return "\n".join(lines[start:end])
