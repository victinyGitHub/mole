"""mole — Python language backend.

Tree-sitter for AST analysis, pyright for type checking.
NO python ast module. Tree-sitter is the only AST engine.
"""
from __future__ import annotations

import json
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
    _PARSER = _ts_get_parser("python")
except ImportError:
    _PARSER = None


# ─── Tree-sitter Helpers ─────────────────────────────────────────────────────

def _parse(source: str):
    """Parse Python source into tree-sitter AST."""
    if _PARSER is None:
        raise RuntimeError("tree-sitter-languages not installed")
    return _PARSER.parse(source.encode("utf-8"))


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


# ─── Python Backend ──────────────────────────────────────────────────────────

class PythonBackend:
    """Python language backend — tree-sitter + pyright."""
    language: str = "python"

    def find_holes(self, source: str) -> list[Hole]:
        """Find all hole() calls in Python source via tree-sitter.

        Classifies each as assignment, return, or bare based on AST context.
        """
        tree = _parse(source)
        # Find all call nodes where function is 'hole'
        call_nodes = _walk_nodes(tree.root_node, "call")
        holes: list[Hole] = []

        for call in call_nodes:
            # Check if function name is 'hole'
            func_node = _find_child_by_field(call, "function")
            if func_node is None or _node_text(func_node) != "hole":
                continue

            # Extract description string from first argument
            args_node = _find_child_by_field(call, "arguments")
            description = ""
            if args_node:
                # First string child in argument_list
                for arg_child in args_node.children:
                    if arg_child.type == "string":
                        # Strip quotes from string content
                        raw = _node_text(arg_child)
                        description = raw.strip("'\"")
                        break

            # 1-indexed line number
            line_no = call.start_point[0] + 1

            # Classify: check parent node to determine hole kind
            parent = call.parent
            is_bare = True
            is_return = False
            var_name: Optional[str] = None
            expected_type: Optional[str] = None

            if parent and parent.type == "assignment":
                # x: T = hole(...) — assignment hole
                is_bare = False
                left = _find_child_by_field(parent, "left")
                if left:
                    var_name = _node_text(left)
                # Check for type annotation on the assignment
                type_node = _find_child_by_field(parent, "type")
                if type_node:
                    expected_type = _node_text(type_node)

            elif parent and parent.type == "return_statement":
                # return hole(...) — return hole
                is_bare = False
                is_return = True
                # Walk up to find enclosing function's return type
                expected_type = self._find_return_type(parent)

            elif parent and parent.type == "expression_statement":
                # Bare hole() as a statement
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
        """Walk up from a return_statement to find enclosing function's return type."""
        current = node.parent
        while current:
            if current.type in ("function_definition", "decorated_definition"):
                func = current
                if current.type == "decorated_definition":
                    # The actual function_definition is a child
                    func = _find_child_by_type(current, "function_definition")
                    if not func:
                        return None
                # Look for return_type field
                ret_type = _find_child_by_field(func, "return_type")
                if ret_type:
                    return _node_text(ret_type)
                return None
            current = current.parent
        return None

    def get_annotation(self, source: str, line_no: int) -> Optional[str]:
        """Extract type annotation at a hole site via tree-sitter.

        Three cases handled:
        - Assignment: `x: list[int] = hole(...)` → returns "list[int]"
        - Return: `return hole(...)` inside `def f() -> str:` → returns "str"
        - Bare: `hole(...)` → returns None

        This is Tier 2 of the type extraction chain (Tier 1 = LSP hover for
        context enrichment, Tier 3 = sentinel trick for unannotated holes).
        """
        tree = _parse(source)
        # Find call nodes at the target line where function is 'hole'
        call_nodes = _walk_nodes(tree.root_node, "call")

        for call in call_nodes:
            if call.start_point[0] + 1 != line_no:
                continue
            func_node = _find_child_by_field(call, "function")
            if func_node is None or _node_text(func_node) != "hole":
                continue

            # Found the hole() at this line — extract type from parent
            parent = call.parent
            if parent and parent.type == "assignment":
                type_node = _find_child_by_field(parent, "type")
                if type_node:
                    return _node_text(type_node)
            elif parent and parent.type == "return_statement":
                return self._find_return_type(parent)

        return None

    def extract_types(self, path: Path, holes: list[Hole]) -> list[Hole]:
        """Three-tier type extraction:
          Tier 2: tree-sitter get_annotation() (fast, no subprocess)
          Tier 3: pyright sentinel trick (for unannotated holes)
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

        # For holes that already have types (from AST or Tier 2), skip sentinel
        untyped = [h for h in holes if not h.expected_type]
        if not untyped:
            return holes

        # Build sentinel source: replace hole() calls with _MoleHole()
        sentinel_source = source
        # Add sentinel class definition at top
        sentinel_def = (
            "class _MoleHole:\n"
            "    def __init__(self, *args, **kwargs): ...\n"
        )
        sentinel_source = sentinel_def + sentinel_source

        # Offset line numbers by 2 (we added 2 lines at top)
        line_offset = 2

        # Replace hole(...) with _MoleHole(...)
        sentinel_source = re.sub(
            r'\bhole\s*\(',
            '_MoleHole(',
            sentinel_source,
        )

        # Write to temp file and run pyright
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, dir=path.parent
        ) as tmp:
            tmp.write(sentinel_source)
            tmp_path = Path(tmp.name)

        try:
            result = subprocess.run(
                ["pyright", "--outputjson", str(tmp_path)],
                capture_output=True, text=True, timeout=30,
            )
            if result.stdout:
                data = json.loads(result.stdout)
                diagnostics = data.get("generalDiagnostics", [])
                self._apply_sentinel_types(holes, diagnostics, line_offset)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass  # Graceful degradation — return holes with whatever types we have
        finally:
            tmp_path.unlink(missing_ok=True)

        return holes

    def _apply_sentinel_types(
        self,
        holes: list[Hole],
        diagnostics: list[dict],
        line_offset: int,
    ) -> None:
        """Extract expected types from pyright diagnostics about _MoleHole.

        Pyright errors will say something like:
        "Expression of type '_MoleHole' is incompatible with declared type 'str'"
        """
        # Pattern to extract expected type from incompatible-type diagnostics
        type_pattern = re.compile(
            r"cannot be assigned to declared type [\"'](.+?)[\"']"
            r"|incompatible with declared type [\"'](.+?)[\"']"
            r"|expected type [\"'](.+?)[\"']"
            r"|\"_MoleHole\" is not assignable to type [\"'](.+?)[\"']"
            r"|Type \"_MoleHole\" is not assignable to type \"(.+?)\""
        )

        for diag in diagnostics:
            diag_line = diag.get("range", {}).get("start", {}).get("line", -1)
            # Pyright uses 0-indexed lines; adjust for sentinel offset
            original_line = diag_line - line_offset + 1  # back to 1-indexed

            msg = diag.get("message", "")
            m = type_pattern.search(msg)
            if m:
                # First non-None group is the expected type
                expected = next((g for g in m.groups() if g), None)
                if expected and expected != "_MoleHole":
                    # Find matching hole by line number
                    for h in holes:
                        if h.line_no == original_line and not h.expected_type:
                            h.expected_type = expected
                            break

    def verify(self, source: str, path: Path) -> list[str]:
        """Run pyright on source, return error strings."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, dir=path.parent
        ) as tmp:
            tmp.write(source)
            tmp_path = Path(tmp.name)

        try:
            result = subprocess.run(
                ["pyright", "--outputjson", str(tmp_path)],
                capture_output=True, text=True, timeout=30,
            )
            errors: list[str] = []
            if result.stdout:
                data = json.loads(result.stdout)
                for diag in data.get("generalDiagnostics", []):
                    severity = diag.get("severity", "")
                    if severity == "error":
                        line = diag.get("range", {}).get("start", {}).get("line", 0)
                        msg = diag.get("message", "")
                        # Filter out "hole" is not defined errors
                        if '"hole" is not defined' in msg:
                            continue
                        errors.append(f"L{line + 1}: {msg}")
            return errors
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            return []  # Graceful degradation
        finally:
            tmp_path.unlink(missing_ok=True)

    def extract_type_definitions(self, source: str) -> str:
        """Extract class definitions (including dataclasses) via tree-sitter."""
        tree = _parse(source)
        # Find class_definition and decorated_definition (for @dataclass)
        class_nodes = _walk_any_type(
            tree.root_node,
            {"class_definition", "decorated_definition"},
        )
        blocks: list[str] = []
        for node in class_nodes:
            # For decorated_definition, check if it wraps a class
            if node.type == "decorated_definition":
                has_class = _find_child_by_type(node, "class_definition")
                if not has_class:
                    continue  # It's a decorated function, skip
            blocks.append(_node_text(node))

        return "\n\n".join(blocks)

    def extract_function_signatures(self, source: str) -> str:
        """Extract function signatures (def lines + docstrings) via tree-sitter."""
        tree = _parse(source)
        func_nodes = _walk_any_type(
            tree.root_node,
            {"function_definition", "decorated_definition"},
        )
        sigs: list[str] = []
        for node in func_nodes:
            func = node
            decorator_text = ""
            if node.type == "decorated_definition":
                # Extract decorator text
                for child in node.children:
                    if child.type == "decorator":
                        decorator_text += _node_text(child) + "\n"
                func = _find_child_by_type(node, "function_definition")
                if not func:
                    continue

            # Build signature: def name(params) -> return_type: ...
            name_node = _find_child_by_field(func, "name")
            params_node = _find_child_by_field(func, "parameters")
            ret_node = _find_child_by_field(func, "return_type")

            if name_node and params_node:
                sig = f"def {_node_text(name_node)}{_node_text(params_node)}"
                if ret_node:
                    sig += f" -> {_node_text(ret_node)}"
                sig += ": ..."
                if decorator_text:
                    sig = decorator_text + sig
                sigs.append(sig)

        return "\n".join(sigs)

    def extract_function_headers(self, source: str) -> list[FunctionHeader]:
        """Extract structured function headers with parsed type info via tree-sitter.

        Parses each function definition to extract:
        - name: function name
        - signature: full def line (for display)
        - return_type: parsed return type annotation (or None)
        - param_types: list of parameter type annotations
        """
        tree = _parse(source)
        func_nodes = _walk_any_type(
            tree.root_node,
            {"function_definition", "decorated_definition"},
        )
        headers: list[FunctionHeader] = []

        for node in func_nodes:
            func = node
            decorator_text = ""
            if node.type == "decorated_definition":
                for child in node.children:
                    if child.type == "decorator":
                        decorator_text += _node_text(child) + "\n"
                func = _find_child_by_type(node, "function_definition")
                if not func:
                    continue

            # Extract function name
            name_node = _find_child_by_field(func, "name")
            params_node = _find_child_by_field(func, "parameters")
            ret_node = _find_child_by_field(func, "return_type")

            if not name_node or not params_node:
                continue

            name = _node_text(name_node)

            # Skip dunder methods and private methods (less likely to be called)
            # but keep them in the list — just don't extract type info for ranking
            # (they'll score 0 and appear at the bottom)

            # Build signature string for display
            sig = f"def {name}{_node_text(params_node)}"
            return_type: Optional[str] = None
            if ret_node:
                return_type = _node_text(ret_node)
                sig += f" -> {return_type}"
            sig += ": ..."
            if decorator_text:
                sig = decorator_text + sig

            # Extract parameter types from typed_parameter nodes
            param_types: list[str] = []
            typed_params = _walk_nodes(params_node, "typed_parameter")
            for tp in typed_params:
                type_node = _find_child_by_field(tp, "type")
                if type_node:
                    param_types.append(_node_text(type_node))

            headers.append(FunctionHeader(
                name=name,
                signature=sig,
                return_type=return_type,
                param_types=param_types,
            ))

        return headers

    def extract_scope_vars(self, source: str, line_no: int) -> list[tuple[str, str]]:
        """Extract typed variables in scope at line_no via tree-sitter.

        Walks the enclosing function to find typed assignments before line_no.
        """
        tree = _parse(source)
        # Find enclosing function
        enclosing = self._find_enclosing_func_node(tree.root_node, line_no)
        if not enclosing:
            # Module-level: scan all assignments before line_no
            enclosing = tree.root_node

        # Find all assignments with type annotations within the enclosing scope
        assignments = _walk_nodes(enclosing, "assignment")
        # Also check function parameters
        params = self._extract_params(enclosing)

        scope_vars: list[tuple[str, str]] = list(params)

        for assign in assignments:
            assign_line = assign.start_point[0] + 1
            if assign_line >= line_no:
                continue  # Only vars defined BEFORE the hole

            left = _find_child_by_field(assign, "left")
            type_node = _find_child_by_field(assign, "type")
            if left and type_node:
                scope_vars.append((_node_text(left), _node_text(type_node)))

        return scope_vars

    def _extract_params(self, func_node) -> list[tuple[str, str]]:
        """Extract typed parameters from a function definition."""
        params: list[tuple[str, str]] = []
        # Find the parameters node
        param_nodes = _walk_nodes(func_node, "typed_parameter")
        for p in param_nodes:
            name_node = _find_child_by_type(p, "identifier")
            type_node = _find_child_by_field(p, "type")
            if name_node and type_node:
                params.append((_node_text(name_node), _node_text(type_node)))
        return params

    def extract_imports(self, source: str) -> list[tuple[str, str]]:
        """Extract imports as (module, names) tuples via tree-sitter."""
        tree = _parse(source)
        imports: list[tuple[str, str]] = []

        # import_from_statement: from X import Y, Z
        from_imports = _walk_nodes(tree.root_node, "import_from_statement")
        for imp in from_imports:
            module_node = _find_child_by_field(imp, "module_name")
            module = _node_text(module_node) if module_node else ""
            # Collect imported names
            names = []
            for child in imp.children:
                if child.type == "dotted_name" and child != module_node:
                    names.append(_node_text(child))
                elif child.type == "aliased_import":
                    name_node = _find_child_by_field(child, "name")
                    if name_node:
                        names.append(_node_text(name_node))
            imports.append((module, ", ".join(names) if names else "*"))

        # import_statement: import X
        plain_imports = _walk_nodes(tree.root_node, "import_statement")
        for imp in plain_imports:
            for child in imp.children:
                if child.type == "dotted_name":
                    imports.append((_node_text(child), ""))

        return imports

    def resolve_import_path(self, module: str, base_dir: Path) -> Optional[Path]:
        """Resolve Python import to file path.

        Handles:
        - Relative imports (. and ..)
        - Package __init__.py
        - Direct module files
        """
        if not module:
            return None

        # Count leading dots for relative imports
        dots = 0
        for ch in module:
            if ch == '.':
                dots += 1
            else:
                break

        if dots > 0:
            # Relative import
            rel_module = module[dots:]
            parent = base_dir
            for _ in range(dots - 1):
                parent = parent.parent

            if rel_module:
                parts = rel_module.split(".")
                candidate = parent / "/".join(parts)
            else:
                candidate = parent
        else:
            # Absolute import — try relative to base_dir first
            parts = module.split(".")
            candidate = base_dir / "/".join(parts)

        # Try: module.py, module/__init__.py
        if candidate.with_suffix(".py").is_file():
            return candidate.with_suffix(".py")
        if (candidate / "__init__.py").is_file():
            return candidate / "__init__.py"

        return None

    def find_enclosing_block(self, source: str, line_no: int) -> str:
        """Find enclosing function/class block around a line."""
        tree = _parse(source)
        node = self._find_enclosing_func_node(tree.root_node, line_no)
        if node and node != tree.root_node:
            return _node_text(node)
        # Fallback: return a window around the line
        return self._context_window(source, line_no, radius=10)

    def _find_enclosing_func_node(self, root, line_no: int):
        """Find the innermost function/class node containing line_no."""
        # DFS — find deepest matching container
        best = None
        func_types = {"function_definition", "decorated_definition", "class_definition"}

        def _search(node):
            nonlocal best
            start = node.start_point[0] + 1  # 1-indexed
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
