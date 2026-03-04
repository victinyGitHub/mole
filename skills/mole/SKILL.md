---
name: mole
description: Use mole for typed-hole-driven code generation. Activate when writing code with hole() placeholders, expanding holes into sub-problems, or filling holes with type-verified LLM-generated code.
argument-hint: "[command] [file]"
---

# Mole — Typed-Hole-Driven Code Generation

Mole lets you write code with `hole()` placeholders and fill them with type-verified LLM-generated code. You control the architecture; the LLM handles implementation details.

## MCP Tools (Preferred — when available)

If the mole MCP server is connected, you have these tools:

| Tool | What it does |
|------|-------------|
| `mole_discover(file)` | Find all holes — returns structured list with types, lines, descriptions |
| `mole_context(file, line)` | Get assembled context for a hole — types, scope vars, functions, behavioral specs |
| `mole_types(file, line)` | Get just type definitions visible to a hole (lighter than full context) |
| `mole_fill(file, line, code?)` | Fill a hole and type-check. Provide `code` to verify your own, omit for LLM generation |
| `mole_verify(file)` | Type-check the entire file |
| `mole_apply(file, line, code)` | Splice code into hole position and write to disk |
| `mole_expand(file, line, approach?)` | Decompose a hole into structural code + smaller sub-holes |
| `mole_diversify(file, line, n?)` | Generate N different approaches (default 3) |

### MCP Workflow

**You are the architect. The tools are your scaffolding.**

1. **Discover** — `mole_discover(file)` to see all holes, their types, and status
2. **Understand** — `mole_context(file, line)` or `mole_types(file, line)` to see what types and functions are available
3. **Think** — reason about the best approach yourself. You ARE the ideator. No subprocess needed.
4. **Write** — write the code yourself using the context you read
5. **Verify** — `mole_fill(file, line, code=your_code)` to type-check your code against the hole's expected type
6. **Fix** — if type errors, read them, adjust your code, verify again
7. **Apply** — `mole_apply(file, line, code)` to write the verified code back to the file

**When to use diversify vs thinking yourself:**
- **Think yourself** for holes where you understand the domain and have the context
- **Use `mole_diversify`** when you want to explore structurally different approaches you might not have considered
- **Use `mole_expand`** when a hole is too complex — decompose it into sub-holes first, then fill each sub-hole

**Never fill-all blindly.** Work one hole at a time. Read the context. Understand the types. Write intentional code.

### Setup

```bash
# Register MCP server with Claude Code
claude mcp add mole -- mole --mcp

# Or with a specific model
claude mcp add mole -- mole --mcp --model opus
```

## Writing Holes

```python
from mole import hole

# Typed assignment — mole extracts the type and verifies fills against it
results: list[str] = hole("search pages for query, return matching URLs")

# Return hole — type comes from function signature
def search(q: str) -> list[str]:
    return hole("implement full-text search")

# Bare hole — side-effect, no return value
hole("write results to output.json")
```

**Type annotations are critical.** They're how mole verifies generated code is correct. Always annotate.

## Behavioral Specs

Add structured specs above holes to guide generation:

```python
# @mole:behavior merge-sort with early termination for sorted runs
# @mole:requires input list has comparable elements
# @mole:ensures output is sorted and same length as input
sorted_items: list[int] = hole("sort the items efficiently")
```

These are passed as constraints during fill, expand, and diversify.

## CLI REPL (Alternative — without MCP)

```bash
mole myfile.py          # Interactive REPL
mole --check myfile.py  # Show holes (no LLM calls)
mole --serve myfile.py  # Background server with HTTP API
mole --mcp              # Start MCP server for Claude Code
```

| Command | What it does |
|---------|-------------|
| `show <hole>` | Inspect hole details, type, behavioral specs, context |
| `fill <hole>` | Generate type-checked code for a hole |
| `expand <hole>` | Decompose into structural code + smaller sub-holes |
| `diversify <hole>` | Generate 3 different approaches side-by-side, pick one |
| `edit <hole>` | Change the hole's description |
| `context <hole>` | See the full context that gets sent to the LLM |
| `apply` | Write all fills back to the source file |
| `undo` | Revert the last fill or expand |
| `verify` | Type-check the current state |

## Strategy Guide

- **Expand before fill** for complex holes. If a hole does 3+ things, expand it first so each sub-hole is focused.
- **Diversify when unsure.** See 3 structurally different approaches, then pick one.
- **Fill for leaf holes.** Simple, well-typed holes with clear descriptions fill reliably in one shot.
- **Use behavioral specs** for constraints the type system can't express (algorithmic approach, edge cases, performance requirements).
- **Type annotations drive quality.** `list[dict[str, Any]]` gives worse fills than a proper dataclass/TypedDict. Define your types.
- **One hole at a time.** The value is in the dance between architecture and implementation.

## Language Support

Full support (type checking + extraction): **Python** (pyright), **TypeScript** (tsc)
Hole detection only: any language with a tree-sitter grammar.

## Install

```bash
git clone https://github.com/victinyGitHub/mole.git
cd mole
pip install -e ".[pretty]"
```

Requires Python 3.10+ and [Claude CLI](https://docs.anthropic.com/en/docs/claude-code/overview).
