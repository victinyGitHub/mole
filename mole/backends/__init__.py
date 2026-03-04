"""mole backends — language-specific source analysis via tree-sitter.

Every backend implements the LanguageBackend protocol.
NO python ast module. Tree-sitter is the only AST engine.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol

from ..types import Hole, FunctionHeader


class LanguageBackend(Protocol):
    """Language-specific source analysis. ALL implementations use tree-sitter."""
    language: str

    def find_holes(self, source: str) -> list[Hole]:
        """Find all hole() calls. Classify as assignment/return/bare."""
        ...

    def get_annotation(self, source: str, line_no: int) -> Optional[str]:
        """Extract type annotation at hole site via tree-sitter.

        Annotated assignment → annotation type.
        Return hole → enclosing function's return type.
        Returns None for unannotated/bare holes (falls through to sentinel).
        """
        ...

    def extract_types(self, path: Path, holes: list[Hole]) -> list[Hole]:
        """Infer expected types: tree-sitter annotations first, then sentinel fallback."""
        ...

    def verify(self, source: str, path: Path) -> list[str]:
        """Run type checker, return error strings."""
        ...

    def extract_type_definitions(self, source: str) -> str:
        """Extract class/interface/struct defs as text blocks."""
        ...

    def extract_function_signatures(self, source: str) -> str:
        """Extract function/method signatures as concise reference."""
        ...

    def extract_function_headers(self, source: str) -> list[FunctionHeader]:
        """Extract structured function headers with parsed type info.

        Returns FunctionHeader objects with name, signature, return_type,
        and param_types parsed out. Used by retrieveRelevantHeaders for
        type-compatible ranking (ChatLSP OOPSLA 2024).
        """
        ...

    def extract_scope_vars(self, source: str, line_no: int) -> list[tuple[str, str]]:
        """Extract typed variables in scope at line_no."""
        ...

    def extract_imports(self, source: str) -> list[tuple[str, str]]:
        """Extract import statements as (module, names) tuples."""
        ...

    def resolve_import_path(self, module: str, base_dir: Path) -> Optional[Path]:
        """Resolve import module string to file path on disk."""
        ...

    def find_enclosing_block(self, source: str, line_no: int) -> str:
        """Find enclosing function/class/block around a line."""
        ...


# ─── Backend Registry ─────────────────────────────────────────────────────────

# Extension → language name
EXTENSION_MAP: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".rs": "rust", ".go": "go",
    ".c": "c", ".cpp": "cpp", ".h": "c",
    ".java": "java", ".rb": "ruby",
    ".sh": "shell", ".lua": "lua",
}


def detect_language(path: Path) -> str:
    """Detect language from file extension."""
    return EXTENSION_MAP.get(path.suffix.lower(), "python")


def get_backend(language: str) -> LanguageBackend:
    """Get the appropriate backend for a language.

    Returns language-specific backend if available,
    falls back to generic tree-sitter backend.
    """
    if language == "python":
        from .python import PythonBackend
        return PythonBackend()
    if language in ("typescript", "javascript"):
        from .typescript import TypeScriptBackend
        return TypeScriptBackend()
    from .generic import GenericBackend
    return GenericBackend(language)
