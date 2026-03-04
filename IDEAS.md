# mole — Ideas & Future Directions

## 1. Retrieval-Augmented Fill (RAF): Collective Snippet Cache + Dynamic ICL

**Status:** Design sketch
**Date:** 2026-03-04
**Contributors:** xi, Samuel

### The Core Insight

If a hole's specification (type signature + natural language description + behavioral spec) is precise enough, the fill becomes a *search problem* before it's a *generation problem*. Someone, somewhere, has already written the code that satisfies `list[str] → "split CSV row into fields"`. Why regenerate it from scratch every time?

Two ideas that merge into one:

1. **Collective fill cache** — a shared corpus of (hole-spec, verified-fill) pairs. When a new hole matches an existing spec closely enough, retrieve the fill instead of generating it.

2. **Dynamic prompt retrieval for ICL** — use search engines (code search, GitHub, package indices) to find *examples* of code that solves similar problems, then feed those examples as in-context learning demonstrations. The LLM doesn't copy them — it uses them as domain expertise triggers.

### Why Sub-Holes Make This Tractable

Monolithic code generation is hard to cache — the search space is combinatorial. But mole's `expand` step decomposes problems into sub-holes, and sub-holes have two properties that make retrieval work:

1. **Small scope** — each sub-hole is a focused, well-typed task (`sort by key`, `filter nulls`, `parse date string`). These are the kind of tasks that appear across thousands of codebases.

2. **Type-indexed** — every sub-hole has a type signature from tree-sitter or the sentinel trick. `list[User] → dict[str, list[User]]` is a much more searchable query than "group users by department and return a mapping."

The expand step is doing *query decomposition* — breaking an unsearchable monolithic query into searchable atomic queries.

### Two Modes: Zero-Edit vs Minimal-Edit

**Zero-edit (Samuel's position):** If you're decomposing a problem well enough, each subproblem should require no context from other subproblems. The spec IS the interface contract. Good abstraction means code from any codebase, any style, should satisfy `(list[int]) -> int: "sum all elements"` without modification. Patterns are just programs with open holes.

**Minimal-edit (xi's position):** In practice, retrieved code needs light adaptation:
- Variable/function renames to match the local codebase
- Style normalization (indentation, naming conventions)
- Minor API adjustments (different library versions, etc.)

The key argument: **any small LLM can do minimal edits reliably.** ChatGPT-4 wrote correct obscure Hazel code when given the right examples (ChatLSP, OOPSLA 2024). Minimal-edit + example-driven ICL could push fill reliability to 90-100% because:
- The LLM isn't generating from scratch (high variance)
- It's adapting existing correct code (low variance)
- The type checker verifies the result regardless

**Readability matters (xi's key point).** Zero-edit retrieved code from a foreign codebase will have mangled naming, different conventions, unfamiliar patterns. Having mangled code style is horrific — and sure you could get everyone to rewrite code to be codebase-agnostic, but why do so when you could just minimally edit? You'll spend the same time reading it anyway — let the LLM refactor the existing minute bit of code and make sure it works too. ChatGPT-4 could write obscure Hazel code with the right examples (ChatLSP showed this) — so any small LLM can handle renames and style adaptation reliably.

### Architecture Sketch

```
Hole Spec → Spec Fingerprint
                ↓
        ┌───────┴───────┐
        │  Cache Lookup  │ ← local cache + shared registry
        └───────┬───────┘
                │
        hit?────┼────miss?
        │               │
        ▼               ▼
  Type-check      Search Phase
  retrieved     ┌─────────────┐
  fill          │ Code search  │ ← GitHub, SourceGraph, local corpus
        │       │ Snippet DB   │
        │       │ Package docs │
        │       └──────┬──────┘
        │              │
        │        Top-K examples
        │              │
        │              ▼
        │        ┌─────────────┐
        │        │ LLM Fill    │ ← examples as ICL demos
        │        │ (minimal    │    + hole spec + type context
        │        │  edit mode) │
        │        └──────┬──────┘
        │              │
        ▼              ▼
   ┌────────────────────────┐
   │  Type-Check Verify     │
   └────────┬───────────────┘
            │
            ▼
   ┌────────────────────────┐
   │  Cache the new fill    │ ← (spec, fill) pair added to corpus
   └────────────────────────┘
```

### Spec Fingerprinting

The search key for a hole isn't just the description — it's a structured fingerprint:

```python
@dataclass
class HoleFingerprint:
    expected_type: str           # "list[str]"
    return_type: str | None      # for return holes
    param_types: list[str]       # types available in scope
    description_embedding: vec   # semantic embedding of NL description
    behavioral_hash: str         # hash of @mole: specs
```

**Matching strategy:**
- **Exact type match + high description similarity** → cache hit (zero-edit)
- **Compatible type match + moderate similarity** → retrieve as ICL example (minimal-edit)
- **No match** → generate from scratch (current behavior)

Type compatibility uses the existing `_type_compat_score()` from context.py — the same scoring that ranks function headers can rank cached fills.

### The ICL Connection (xi's shower thought)

"Dynamic prompt retrieval using search engines to trigger ICL domain expertise" — the insight is that LLMs don't need to *know* a domain. They need to *see examples* from that domain in their prompt. This is why ChatLSP worked for Hazel: not because GPT-4 was trained on Hazel, but because the right examples in-context made it capable.

For mole, this means:
1. **Hole spec → search query** — convert the type + description into a code search query
2. **Search → top-K code snippets** — from GitHub, SourceGraph, local codebase, or a curated snippet DB
3. **Snippets → ICL examples in fill prompt** — inject retrieved code into the LLM's context as "here are similar implementations"
4. **LLM generates fill with examples** — not copying, but using the examples as structural templates

This turns the fill prompt from "generate code for this type" to "here are 3 examples of similar functions — now write one that satisfies this specific spec." The reliability difference is massive.

### Composability: Expand + Retrieve + Fill Pipeline

The full pipeline combines mole's existing expand with retrieval:

```
1. EXPAND: decompose hole into sub-holes
   └─ structural code + 3 sub-holes with type annotations

2. RETRIEVE: for each sub-hole, search for matches
   ├─ Sub-hole A: cache HIT → use directly (zero-edit)
   ├─ Sub-hole B: 2 similar examples found → ICL-augmented fill
   └─ Sub-hole C: no matches → standard generation

3. FILL: generate code for sub-holes B and C
   └─ Sub-hole B gets examples in prompt → higher reliability
   └─ Sub-hole C gets standard context → current behavior

4. VERIFY: type-check the composed result
   └─ cache all verified fills for future retrieval
```

### What This Means for Mole's Position

Mole becomes a *coordination layer* between:
- **Type system** (specifies what's needed)
- **Search** (finds existing solutions)
- **LLM** (adapts/generates when needed)
- **Verification** (ensures correctness)

The LLM is the *last resort*, not the first. The more the cache grows, the less generation is needed. Every verified fill makes future fills more likely to be cache hits. It's a flywheel.

### Open Questions

1. **Fingerprint granularity** — how specific should matching be? Too strict = no cache hits. Too loose = wrong code retrieved.
2. **Adaptation scope** — what counts as "minimal edit"? Renames are trivial. API changes are harder. Where's the line?
3. **Cache invalidation** — when a type definition changes, which cached fills are still valid?
4. **Privacy/licensing** — cached fills from other users' codebases have IP implications. Need clear provenance tracking.
5. **Cold start** — the cache is empty initially. How to bootstrap? Seed from open-source corpora?
6. **Search engine choice** — SourceGraph API? GitHub code search? Local embeddings? Multiple backends?

### Immediate Next Steps (if we build this)

1. **Spec fingerprinting** — implement `HoleFingerprint` dataclass with type + description embedding
2. **Local cache** — SQLite DB of (fingerprint, fill_code, verify_result) triples
3. **Cache-aware fill pipeline** — check cache before LLM, store result after verify
4. **ICL retrieval** — integrate a code search API (SourceGraph or GitHub) into the fill prompt
5. **Benchmark** — measure cache hit rate and fill quality improvement on the 16-hole benchmark
