# 🕳 mole

**Write code with holes. Let AI fill them in.**

Mole is a CLI tool for writing programs incrementally. Instead of asking an AI to write everything at once, you write the structure yourself and leave `hole()` placeholders where you want generated code. You stay in control of every decision.

## Install

You need **Python 3.10+** and **[Claude CLI](https://docs.anthropic.com/en/docs/claude-code/overview)** (handles auth for you).

```bash
pip install "mole-code[pretty] @ git+https://github.com/victinyGitHub/mole.git"
```

Or clone and install locally:

```bash
git clone https://github.com/victinyGitHub/mole.git
cd mole
pip install -e ".[pretty]"
```

Verify it works:

```bash
mole --check examples/test_projects/fizzbuzz.py
```

You should see 2 holes detected with their types.

### Claude Code plugin

If you use [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview), install mole as a plugin so Claude knows how to use it:

```
/plugin marketplace add victinyGitHub/claude-plugins
/plugin install mole@xi-plugins
```

Then run `/mole:setup` to install the CLI, or `/mole:mole` for full usage reference.

> **PATH not working?** If `mole` isn't found after install, add pip's bin to your PATH:
> ```bash
> echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
> ```

## How it works

**1. Write a file with holes:**

```python
# search.py
from mole import hole

def search(pages: list[dict], query: str) -> list[str]:
    """Find pages matching a search query."""
    results: list[str] = hole("search pages for query, return matching URLs")
    return results
```

The type annotation (`list[str]`) tells mole what the generated code needs to return. The string inside `hole()` describes what you want.

**2. Open it in mole:**

```bash
mole search.py
```

**3. Use the REPL to work with your holes:**

| Command | What it does |
|---------|-------------|
| `show <hole>` | See hole details and context |
| `fill <hole>` | Generate code for that hole (type-checked automatically) |
| `expand <hole>` | Break a big hole into smaller sub-holes |
| `diversify <hole>` | See 3 different approaches side-by-side |
| `edit <hole>` | Change the hole description |
| `apply` | Write all generated code back to your file |
| `undo` | Revert last fill/expand |
| `config model opus` | Switch to a different Claude model |

`<hole>` is the hole number shown in the REPL (e.g. `fill 1` fills hole 1).

That's the core loop: **write holes → fill/expand → review → apply**.

## Tips

- **Type annotations matter.** `results: list[str] = hole(...)` gives much better fills than just `hole(...)` with no type hint. The type is what mole uses to verify the generated code is correct.

- **Expand before fill** for complex holes. If a hole is doing too many things, `expand` breaks it into structural code + smaller sub-holes that are easier to fill individually.

- **Diversify when you're unsure.** `diversify 1` shows 3 different approaches streaming side-by-side so you can pick the one you like.

- **Behavioral specs** give extra guidance:
  ```python
  # @mole:behavior use binary search, not linear scan
  # @mole:ensures returns -1 if not found
  index: int = hole("find the target value")
  ```

## Batch mode

```bash
mole --check file.py       # just show holes, no LLM calls
mole --fill-all file.py    # fill everything in one shot
mole --model opus file.py  # open REPL with opus
```

## Languages

Works best with **Python** and **TypeScript** (full type checking). Hole detection works for any language with a tree-sitter grammar.

## License

MIT
