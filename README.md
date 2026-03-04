# 🕳 mole

**Dig holes. Fill code. Grow programs.**

Mole is a human-centred assisted programming tool. You write code with `hole()` placeholders where you don't know the implementation yet. Mole helps you:

- **Expand** holes into smaller sub-problems with structural code
- **Diversify** — explore 3 different approaches side-by-side
- **Fill** leaf-level holes with LLM-generated, type-verified code

You steer every decision. Mole assists, never decides.

## Prerequisites

- **Python 3.10+**
- **[Claude CLI](https://docs.anthropic.com/en/docs/claude-code/overview)** — mole uses Claude as its LLM backend

## Install

```bash
pip install mole-code
```

Or from source:

```bash
git clone https://github.com/xidequest/mole.git
cd mole
pip install -e .
```

For the pretty terminal UI (recommended):

```bash
pip install mole-code[pretty]
```

## Quick Start

### 1. Write a seed file with holes

```python
# my_tool.py
from mole import hole

def fetch_pages(urls: list[str]) -> list[dict]:
    """Fetch and parse multiple web pages."""
    pages: list[dict] = hole("fetch each URL, parse HTML, return list of {url, title, text}")
    return pages

def build_index(pages: list[dict]) -> dict[str, list[str]]:
    """Build an inverted search index from pages."""
    index: dict[str, list[str]] = hole("tokenize each page's text, build word -> [url] mapping")
    return index
```

### 2. Open the REPL

```bash
mole my_tool.py
```

```
  🕳 mole
  dig holes · fill code · grow programs

  my_tool.py — 2 hole(s)

  [1] L6  ○ unfilled  pages: list[dict]
      "fetch each URL, parse HTML, return list of {url, title, text}"

  [2] L11  ○ unfilled  index: dict[str, list[str]]
      "tokenize each page's text, build word -> [url] mapping"

  Commands: show · expand · diversify · fill · edit · propagate · groups · context · verify · apply · undo · reload · quit
```

### 3. Work with holes

```
mole> expand 1          # decompose into sub-holes
mole> diversify 1       # see 3 different approaches side-by-side
mole> fill 1            # generate code with type checking
mole> show 1            # inspect a hole's details
mole> context 1         # see what context the LLM receives
mole> config model opus # switch to opus mid-session
mole> apply             # write all fills back to file
```

## How It Works

```
                    ┌──────────┐
  seed file ──────▶ │ discover │ ← tree-sitter finds hole() calls
  with holes        └────┬─────┘
                         │
                    ┌────▼─────┐
                    │ expand   │ ← decompose into structural code + sub-holes
                    └────┬─────┘
                         │
                    ┌────▼─────┐
                    │ fill     │ ← LLM generates code for each hole
                    └────┬─────┘
                         │
                    ┌────▼─────┐
                    │ verify   │ ← type checker confirms correctness
                    └────┬─────┘
                         │
                    ┌────▼─────┐
                    │ apply    │ ← write verified code back to file
                    └──────────┘
```

**Three-tier type extraction:**
1. Tree-sitter reads type annotations directly from your code (instant)
2. Sentinel trick infers types from the type checker for unannotated holes

**Composable context layers:**
- **Types** — annotations, imported types, scope variables
- **Symbols** — function signatures ranked by type compatibility
- **Behavior** — `@mole:behavior`, `@mole:requires`, `@mole:ensures` specs
- **Code** — enclosing block, indentation style

## Behavioral Specs

Add structured specs above holes to guide generation:

```python
# @mole:behavior merge-sort with early termination for sorted runs
# @mole:requires input list has comparable elements
# @mole:ensures output is sorted and same length as input
sorted_items: list[int] = hole("sort the items efficiently")
```

## Batch Mode

```bash
mole --check my_tool.py         # show holes (no LLM calls)
mole --fill-all my_tool.py      # fill all holes
mole --expand-all my_tool.py    # expand all holes
mole --model opus my_tool.py    # use opus in REPL
```

## Configuration

In the REPL, use `config` to adjust settings on the fly:

```
mole> config                    # show current settings
mole> config model opus         # switch to opus
mole> config effort high        # extended thinking
mole> config streaming off      # disable streaming
mole> config temperature 0.5    # more creative
```

## Language Support

Mole uses tree-sitter for all languages. Full support (type checking + extraction):

- **Python** (pyright)
- **TypeScript/JavaScript** (tsc)

Tree-sitter parsing (hole detection, code structure) works for any language with a tree-sitter grammar.

## License

MIT
