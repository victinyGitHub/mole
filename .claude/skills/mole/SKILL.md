---
name: mole
description: Use mole for typed-hole-driven code generation. Activate when writing code with hole() placeholders, expanding holes into sub-problems, or filling holes with type-verified LLM-generated code.
argument-hint: "[command] [file]"
---

# Mole — Typed-Hole-Driven Code Generation

Mole lets you write code with `hole()` placeholders and fill them with type-verified LLM-generated code. You control the architecture; the LLM handles implementation details.

## Core Workflow

1. **Write a seed file** with `hole()` where you want generated code
2. **Open it**: `mole myfile.py`
3. **Expand/fill/diversify** holes in the REPL
4. **Apply** to write fills back to the file

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

These are passed to the LLM as constraints during fill, expand, and diversify.

## REPL Commands

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
| `groups` | Show hole groups and hierarchy |
| `reload` | Re-read the file from disk |
| `config` | Show/change settings (model, effort, streaming, etc.) |
| `verify` | Type-check the current state |

`<hole>` is the hole number shown in the REPL listing.

## Config Options

```
config model <haiku|sonnet|opus>    # switch Claude model
config effort <low|medium|high>     # thinking depth (high = extended thinking)
config streaming <on|off>           # live token streaming
config temperature <0.0-1.0>       # creativity
config timeout <seconds>           # per-fill timeout
```

## Batch Mode

```bash
mole --check myfile.py        # show holes without LLM calls
mole --fill-all myfile.py     # fill all holes non-interactively
mole --expand-all myfile.py   # expand all holes
mole --model opus myfile.py   # open REPL with specific model
```

## Strategy Guide

- **Expand before fill** for complex holes. If a hole does 3+ things, expand it first so each sub-hole is focused.
- **Diversify when unsure.** See 3 structurally different approaches streaming side-by-side, then pick one.
- **Fill for leaf holes.** Simple, well-typed holes with clear descriptions fill reliably in one shot.
- **Use behavioral specs** for constraints the type system can't express (algorithmic approach, edge cases, performance requirements).
- **Type annotations drive quality.** `list[dict[str, Any]]` gives worse fills than a proper dataclass/TypedDict. Define your types.

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
