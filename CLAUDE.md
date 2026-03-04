# Mole — Development Guide

Typed-hole-driven code generation CLI. See SPEC.md for full architecture spec.

## Architecture

```
mole/
  cli.py          — REPL + batch mode entry points
  operations.py   — core ops: discover, expand, diversify, fill, verify, apply
  context.py      — 4 composable context layers (types, symbols, behavior, code)
  prompts.py      — all LLM prompt templates (atomic fragments, composed)
  fillers.py      — Claude CLI subprocess (str→str pipe)
  display.py      — Rich terminal UI (streaming panels, diversify display)
  types.py        — Hole, FunctionHeader, Expansion, VerifyResult dataclasses
  protocol.py     — Filler protocol + FillerConfig
  backends/
    __init__.py    — LanguageBackend protocol
    python.py      — tree-sitter + pyright
    typescript.py  — tree-sitter + tsc
    generic.py     — fallback for unsupported languages
```

## Key Rules

- **Tree-sitter only.** No python `ast` module. All source analysis via tree-sitter.
- **All prompts in prompts.py.** Atomic fragments composed into templates.
- **Filler is a dumb pipe.** `str → str`. No context logic in fillers.
- **Context is 4 layers.** Types, Symbols, Behavior, Code. Each independently toggleable.

## Running

```bash
# REPL
python -m mole examples/test_projects/fizzbuzz.py

# Check holes (no LLM)
python -m mole --check examples/test_projects/fizzbuzz.py

# Tests
python -c "from mole import hole, discover, expand, fill; print('OK')"
```

## Dependencies

- tree-sitter-languages (AST parsing)
- rich (optional, terminal UI)
- Claude CLI must be installed for fills
