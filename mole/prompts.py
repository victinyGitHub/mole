"""mole — LLM prompt templates.

All prompts that go to the LLM live here. Atomic fragments that can be
composed, tested, and refined independently.

Design principles:
  - Each fragment does ONE thing (role, task, output format, constraint)
  - Templates compose fragments via str.format() or concatenation
  - Fragments are exposed as module-level constants for easy iteration
  - No business logic here — just text assembly

When refining prompts:
  1. Change the fragment here
  2. Run `mole --check` to verify templates still format correctly
  3. Test with `mole fill` on a known file to see output quality
"""
from __future__ import annotations


# ─── Atomic Fragments ────────────────────────────────────────────────────────
# Each fragment is a self-contained instruction unit.

# Roles — who is the LLM?
ROLE_GENERATOR = "You are a code generator. Implement EXACTLY what is described below."
ROLE_ARCHITECT = "You are a code architect. Decompose this hole into REAL code with typed sub-holes."
ROLE_IDEATOR = "You are a software designer proposing structurally different approaches."

# Output format — how should the response be structured?
FORMAT_CODE_ONLY = "- Return ONLY code. No explanations, no markdown fences."
FORMAT_MULTI_STATEMENT = "- You may use MULTIPLE statements. Write clear, readable code."
FORMAT_NO_HOLES = "- Do NOT use `hole()` in your output — implement everything directly."
FORMAT_IMPORTS = "- If you need imports, prefix each with `#import: ` on its own line at the top."
FORMAT_STYLE_MATCH = "- Match the surrounding code style (indent, naming conventions)."
FORMAT_NO_CLOSURES = "- Do NOT use closures or wrapper functions — inline code only."

# Fill mode — what kind of output is expected?
FILL_VALUE_MODE = (
    "- The LAST LINE of your response must be a bare expression that produces the result value.\n"
    "- All lines before it are setup statements."
)
FILL_SIDEEFFECT_MODE = (
    "- This is a SIDE-EFFECT hole (mutation, I/O, storing). Write statements that perform the action.\n"
    "- Do NOT return a value. Just write the statements directly."
)

# Hole classification — how should sub-holes be written?
HOLES_TYPED = (
    "- When a sub-hole PRODUCES A VALUE, use a typed assignment: `result: Type = hole(\"...\")`\n"
    "- When a sub-hole is a SIDE EFFECT (mutation, I/O, storing), use a bare statement: `hole(\"...\")`\n"
    "  Do NOT write `_: None = hole(...)` — just write `hole(...)` on its own line."
)

# Behavioral annotations
HOLES_BEHAVIOR = "- Add `{comment_prefix} @mole:behavior ...` comment above each sub-hole."
HOLES_APPROACH = "- Name your approach on the FIRST LINE as a comment: `{comment_prefix} approach: <name>`"

# Validity
VALID_CODE = "- The code must be valid {language} that would compile if holes were filled."

# Diversify format
DIVERSIFY_FORMAT = """\
For each approach, give:
1. A short name (2-4 words)
2. A one-sentence description

Format EXACTLY as:
APPROACH 1: <name>
<description>

APPROACH 2: <name>
<description>

...

Make approaches genuinely different in structure, not just wording."""


# ─── Composed Templates ──────────────────────────────────────────────────────
# These assemble fragments into complete prompts.
# {placeholders} are filled at call time by operations.py.

FILL_PROMPT_TEMPLATE = f"""\
{ROLE_GENERATOR}

TASK: {{description}}

{{context}}

RULES:
{FORMAT_CODE_ONLY}
{FORMAT_MULTI_STATEMENT}
{{fill_mode_hint}}
{FORMAT_NO_HOLES}
{FORMAT_IMPORTS}
{FORMAT_STYLE_MATCH}
{{type_constraint}}
{{behavior_constraint}}
"""

EXPAND_PROMPT_TEMPLATE = f"""\
{ROLE_ARCHITECT}

TASK: {{description}}
{{idea_hint}}
{{context}}

RULES:
- Write real, runnable code that decomposes the task into sub-tasks.
- For each sub-task, use `hole("description of sub-task")` as a placeholder.
{HOLES_TYPED}
{HOLES_BEHAVIOR}
{HOLES_APPROACH}
{{valid_code}}
{FORMAT_NO_CLOSURES}
{FORMAT_CODE_ONLY}
"""

DIVERSIFY_PROMPT_TEMPLATE = f"""\
Propose {{n}} structurally different approaches to implement this task.

TASK: {{description}}
Type: {{expected_type}}

{DIVERSIFY_FORMAT}
"""


# ─── Helper ──────────────────────────────────────────────────────────────────

def fill_mode_hint(is_bare: bool) -> str:
    """Return the appropriate fill mode instruction for value vs side-effect holes."""
    return FILL_SIDEEFFECT_MODE if is_bare else FILL_VALUE_MODE
