# Mole v3 Specification

## What Is Mole

A human-centred assisted programming tool. Humans write code with `hole()` placeholders where they don't know the implementation. Mole helps them decompose holes into smaller sub-problems (expand), explore multiple approaches (diversify), and fill leaf-level holes with LLM-generated code (fill). The human steers every decision — mole assists, never decides.

## Core Principles (NON-NEGOTIABLE)

1. **Language agnostic.** ALL source analysis uses tree-sitter. NO `ast.parse()`, NO `ast.walk()`, NO python stdlib AST anywhere. Python's ast module is BANNED. Tree-sitter is the only AST engine. Regex is only for: parsing LLM output, parsing type checker error messages, and as a last-resort fallback when tree-sitter grammar is unavailable.

2. **Expand is default, fill is leaf.** Human decomposes first, implements last. The primary workflow is: discover → expand → expand → ... → fill. Mass fill is a wrapper on single-hole fill.

3. **Single-hole granularity is the primitive.** Every operation targets one hole. Batch ops are thin wrappers.

4. **Behavioral specs travel with code.** @mole: structured comments in source, not separate spec files.

5. **Composable context assembly.** 4 toggleable layers (types/symbols/behavior/code). Each layer extracts info and formats a prompt fragment. Layers are independently on/off.

6. **Filler is a dumb str→str pipe.** No context logic. Receives assembled prompt, returns code string. Config exposed.

7. **Human agrees on constraints before implementation.** Anti-vibe-coding. Behavioral specs proposed by LLM during expand, reviewed/adjusted by human before fill.

## Architecture

```
CLI (REPL or batch)
  → operations.py (discover / expand / diversify / fill / verify / apply)
    → context.py (4 composable layers)
      → fillers.py (str→str pipes: Claude CLI / Manual / API)
    → backends/ (language-specific: hole finding, type extraction, verification)
      → python.py   (tree-sitter + pyright)
      → typescript.py (tree-sitter + tsc)
      → generic.py  (tree-sitter only, no type checker)
    → protocol.py (@mole: comment parser, tree-sitter based)
    → types.py (all data structures, no holes)
```

## Language Backend Protocol

Every language-specific operation goes through a `LanguageBackend`. This is the KEY abstraction that prevents python-hardcoding.

```python
class LanguageBackend(Protocol):
    """Language-specific source analysis. ALL operations use tree-sitter."""
    language: str  # "python", "typescript", etc.

    def find_holes(self, source: str) -> list[Hole]:
        """Find all hole() calls. Classify as assignment/return/bare.
        Extract var_name, expected_type from AST context."""
        ...

    def extract_types(self, path: Path, holes: list[Hole]) -> list[Hole]:
        """Run type checker (pyright/tsc) sentinel trick to infer types."""
        ...

    def verify(self, source: str, path: Path) -> list[str]:
        """Run type checker on source, return error strings."""
        ...

    def extract_type_definitions(self, source: str) -> str:
        """Extract class/interface/struct definitions as text blocks."""
        ...

    def extract_function_signatures(self, source: str) -> str:
        """Extract function/method signatures as concise reference."""
        ...

    def extract_scope_vars(self, source: str, line_no: int) -> list[tuple[str, str]]:
        """Extract typed variables in scope at line_no."""
        ...

    def extract_imports(self, source: str) -> list[tuple[str, str]]:
        """Extract import statements as (module, names) tuples."""
        ...

    def resolve_import_path(self, module: str, base_dir: Path) -> Optional[Path]:
        """Resolve an import to a file path on disk."""
        ...

    def find_enclosing_block(self, source: str, line_no: int) -> str:
        """Find the enclosing function/class/block around a line."""
        ...
```

### Backend Implementations

#### `backends/python.py`
- `find_holes`: tree-sitter walks for `call` nodes where function is `hole`
- `extract_types`: pyright sentinel trick (replace hole() with _MoleHole, parse errors)
- `verify`: pyright --outputjson subprocess
- `extract_type_definitions`: tree-sitter walks for `class_definition` nodes
- `extract_function_signatures`: tree-sitter walks for `function_definition` nodes
- `extract_scope_vars`: tree-sitter finds enclosing function, walks `typed_assignment` nodes
- `extract_imports`: tree-sitter walks for `import_from_statement` nodes
- `resolve_import_path`: Python module resolution (relative `.` imports, package `__init__.py`)
- `find_enclosing_block`: tree-sitter walks parents from hole line to nearest function/class

#### `backends/typescript.py`
- `find_holes`: tree-sitter walks for `call_expression` where function is `hole`
- `extract_types`: tsc sentinel trick (same concept, TypeScript type system)
- `verify`: tsc --noEmit subprocess
- `extract_type_definitions`: tree-sitter walks for `interface_declaration`, `type_alias_declaration`, `class_declaration`
- `extract_function_signatures`: tree-sitter walks for `function_declaration`, `method_definition`
- `extract_scope_vars`: tree-sitter finds enclosing function, walks `variable_declaration` with type annotation
- `extract_imports`: tree-sitter walks for `import_statement` nodes
- `resolve_import_path`: TypeScript module resolution (relative paths, index.ts, .ts/.tsx)
- `find_enclosing_block`: tree-sitter walks parents from hole line to nearest function/class

#### `backends/generic.py`
- Fallback for any tree-sitter-supported language
- `find_holes`: tree-sitter walks for function calls named `hole`
- `extract_types`: returns holes unchanged (no type checker)
- `verify`: returns empty list (no type checker)
- All other methods: tree-sitter generic implementations using common node patterns
- Less precise than language-specific backends but WORKS

### Backend Selection
```python
def get_backend(language: str) -> LanguageBackend:
    if language == "python": return PythonBackend()
    if language in ("typescript", "javascript"): return TypeScriptBackend()
    return GenericBackend(language)
```

Selected by file extension → language string → backend. operations.py calls `get_backend()` once per file.

## Tree-Sitter Usage Patterns

### Comment Node Types (by language)
- Python, TypeScript, JavaScript, Go, C, Lua, Ruby: `"comment"`
- Rust: `"line_comment"`, `"block_comment"`
- Set: `{"comment", "line_comment", "block_comment"}`

### Function Definition Node Types
- Python: `function_definition`, `decorated_definition`
- TypeScript/JS: `function_declaration`, `method_definition`, `arrow_function`
- Rust: `function_item`
- Go: `function_declaration`, `method_declaration`
- Generic fallback: any node type containing "function" or "method"

### Import Node Types
- Python: `import_from_statement`, `import_statement`
- TypeScript/JS: `import_statement`
- Rust: `use_declaration`
- Go: `import_declaration`

### Class/Interface Node Types
- Python: `class_definition`
- TypeScript/JS: `class_declaration`, `interface_declaration`, `type_alias_declaration`
- Rust: `struct_item`, `enum_item`, `trait_item`
- Go: `type_declaration`

### tree-sitter-languages Setup
```python
import warnings
warnings.filterwarnings("ignore", category=FutureWarning, module="tree_sitter")
from tree_sitter_languages import get_parser

# Compatible versions: tree-sitter==0.21.3 + tree-sitter-languages==1.10.2
# The 0.25+ API breaks tree-sitter-languages
```

## @mole: Comment Protocol

Language-agnostic structured comments. Parsed by tree-sitter (finds comment nodes) + regex (extracts tag/value from comment text).

### Tags
- `@mole:behavior` — what this code should do
- `@mole:requires` — preconditions
- `@mole:ensures` — postconditions
- `@mole:type` — expected type (auto-generated)
- `@mole:filled-by` — provenance (model@date)
- `@mole:expanded-from` — parent hole line
- `@mole:approach` — which expansion approach chosen

### Parsing
1. Tree-sitter finds comment nodes → `{line_no: comment_text}`
2. Walk source lines sequentially
3. Comment line → check for `@mole:tag value` via regex, accumulate
4. Code line → flush accumulated tags, map to this line number
5. Blank line → don't flush (allows gap between annotations and code)
6. Falls back to line-by-line regex if tree-sitter unavailable

## Context Assembly (4 Layers)

### Layer 1: Types
- Hole's expected type annotation
- Variable name and kind (assignment/return/bare)
- Import statements from this file
- Type definitions from this file (classes, interfaces, dataclasses)
- Transitively resolved type definitions from imported modules (depth 3)
- Scope variables at hole location with types

### Layer 2: Symbols
- Function signatures from this file
- Top-level constants
- Function signatures from imported local modules

### Layer 3: Behavior
- @mole:behavior, requires, ensures from structured comments
- OPTIONAL — can be toggled off

### Layer 4: Code
- Enclosing function/class/block source
- Sibling holes' descriptions
- Indent style detection
- Language identifier

### Assembly
```python
def assemble_context(hole, source, path, layers=DEFAULT_LAYERS, backend=None) -> str:
    # Each layer.build() returns a prompt fragment or ""
    # Non-empty fragments joined with section headers
```

## Filler Protocol

```python
class Filler(Protocol):
    config: FillerConfig
    def fill(self, prompt: str) -> str: ...
```

### Implementations
- **ClaudeCLIFiller**: subprocess to /usr/local/bin/claude. Clean env (pop CLAUDECODE). Semaphore-capped concurrency. stdin=DEVNULL.
- **ManualFiller**: prints prompt summary, reads code from stdin.
- **APIFiller**: REST API (DeepSeek, Gemini, OpenAI-compatible).

### Output Cleaning
- Strip markdown code fences (triple backtick)
- Strip inline single backtick wrapping
- Strip LLM reasoning leaks ("Let me think...", "Now I have...", etc.)

## Operations

### discover(path) → MoleFile
1. Read file, detect language from extension
2. `backend.find_holes(source)` — tree-sitter based
3. `backend.extract_types(path, holes)` — type checker sentinel trick
4. `parse_mole_comments(source)` — tree-sitter based
5. `attach_specs_to_holes(holes, comments)`
6. Return MoleFile

### expand(hole, source, path, filler, context_layers) → Expansion
1. Assemble context for the target hole
2. Build expand prompt: "decompose this hole into real code with typed sub-holes"
3. Call filler
4. Parse expansion: find new hole() calls in the output
5. Return Expansion with approach name, code, sub-holes

### diversify(hole, ..., count=3) → list[Expansion]
1. Generate idea hints (optional LLM call for creative approaches)
2. Call expand() N times with different idea hints
3. Return list of expansions for human to choose

### fill(hole, source, path, filler, context_layers) → str
1. Assemble context for the target hole
2. Build fill prompt: "implement this hole, return only code"
3. Call filler
4. Clean output (strip fences, reasoning leaks)
5. Return code string

### verify(fill_code, source, path) → VerifyResult
1. Substitute fill into source at hole location
2. `backend.verify(filled_source, path)` — type checker
3. Compare against baseline errors (pre-existing)
4. Return VerifyResult with success, new_errors

### apply(fill_code, hole, source, path) → str
1. Determine insertion strategy (assignment, return, bare)
2. Handle multi-statement fills (detect, insert as block)
3. Hoist #import: lines to file header
4. Add @mole:filled-by provenance comment
5. Return modified source

## CLI

### Interactive REPL
```
mole myfile.py

Loaded myfile.py — 5 holes found

[1] L15  ○ unfilled  result: list[User]
    "load users from database"
    behavior: query all active users, sort by name

[2] L23  ○ unfilled
    "save user to database"

Commands: show <n>, expand <n>, diversify <n>, fill <n>,
          context <n>, verify <n>, apply, undo, reload, quit
```

### Batch Modes
- `mole --check myfile.py` — show holes
- `mole --fill-all myfile.py` — fill all unfilled holes
- `mole --expand-all myfile.py` — expand all unfilled holes

### Flags
- `--filler <name>` — claude (default), claude-opus, deepseek, gemini, manual
- `--layers <list>` — types,symbols,behavior,code (default: all)
- `--language <lang>` — override auto-detection

## Test Requirements

### Test Files MUST Include Multiple Languages
The builder MUST create AND test with:
1. A Python test file (fizzbuzz or similar) — 2-3 holes
2. A TypeScript test file (string utility or similar) — 2-3 holes
3. Both must pass: hole discovery, type extraction (where available), fill, verify

### Verification
- Python: pyright subprocess
- TypeScript: tsc --noEmit subprocess
- Generic: skip (no type checker)

## Hard Rules

1. **NO python ast module.** `import ast` is BANNED. Use tree-sitter for everything.
2. **Every hole MUST be fully typed.** Minimum L2 (concrete types with field names). Warn on untyped.
3. **Minimal inline comments on all code.** Every non-trivial line or block gets a comment.
4. **Multi-language tests are MANDATORY.** Python + TypeScript tests must both pass.
5. **tree-sitter is the ONLY AST engine.** No language-specific stdlib parsers.
6. **Backend protocol is enforced.** All language-specific code goes through LanguageBackend. Operations and context layers call backend methods, never raw tree-sitter.

## What To Keep From v2

- `types.py` — data structures are solid, reuse as-is
- `fillers.py` — ClaudeCLI/Manual/API fillers work well, reuse as-is
- `protocol.py` — tree-sitter comment parser (already swapped), reuse as-is
- `cli.py` — REPL structure is good, adapt for backend abstraction
- The overall architecture diagram and operation signatures

## What To Rewrite From v2

- `operations.py` — replace all `ast.parse`/`ast.walk` with backend calls
- `context.py` — replace all `ast.parse`/`ast.walk` with backend calls
- NEW: `backends/__init__.py` — LanguageBackend protocol + get_backend()
- NEW: `backends/python.py` — tree-sitter + pyright implementation
- NEW: `backends/typescript.py` — tree-sitter + tsc implementation
- NEW: `backends/generic.py` — tree-sitter only fallback

## Spec Completeness Ladder

| Level | What fill sees | Composition rate |
|---|---|---|
| L0 (bare hole) | NL only | ~5% |
| L1 (type annotation) | `list[User]` | ~20-40% |
| L2 (+ interfaces) | `User{id,name,scores}` | ~85% |
| L3 (+ all symbols) | fn signatures in scope | ~85% |
| L4 (+ behavioral contract) | @mole:behavior/ensures | ~90% |
| L5 (+ expansion) | structural sub-holes | ~98% |

Target: L4 minimum for all holes before filling.

## Dependencies

```
tree-sitter==0.21.3
tree-sitter-languages==1.10.2
```

pyright and tsc expected on PATH (graceful degradation if missing).
