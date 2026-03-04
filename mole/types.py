"""mole — Core type definitions.

All data structures for the hole-driven programming system.
These are CONCRETE — no holes here. Types are the protocol.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol
from enum import Enum
import re


# ─── Hole Status ──────────────────────────────────────────────────────────────

class HoleStatus(Enum):
    """Lifecycle of a hole."""
    UNFILLED = "unfilled"       # hole() exists, not yet touched
    EXPANDED = "expanded"       # decomposed into sub-holes
    FILLED = "filled"           # implementation inserted
    VERIFIED = "verified"       # filled + type-checked clean


# ─── Function Header ─────────────────────────────────────────────────────────

@dataclass
class FunctionHeader:
    """Structured representation of a function signature.

    Used by retrieveRelevantHeaders to rank functions by type compatibility
    with a hole's expected type. Inspired by ChatLSP (OOPSLA 2024) which
    showed that type-compatible function headers improve fill quality by 3x.
    """
    name: str                                   # function name
    signature: str                              # full signature text (for display)
    return_type: Optional[str] = None           # parsed return type string
    param_types: list[str] = field(default_factory=list)  # parameter type strings
    source_file: Optional[str] = None           # filename this came from (None = current file)

    def type_relevance_score(self, target_type: Optional[str]) -> int:
        """Score how relevant this function is for producing `target_type`.

        Scoring tiers (higher = more relevant):
          4 — return type is an exact match to target type
          3 — return type contains the target's base type (e.g. list[User] matches User)
          2 — a parameter type matches the target type (might transform/validate it)
          1 — return type or param shares a word with target (loose association)
          0 — no type relationship detected

        This is deliberately conservative — false negatives are better than
        false positives. A function ranked at 0 is still shown, just lower.
        """
        if not target_type:
            return 0  # No target type → can't rank

        target_normalized = _normalize_type(target_type)
        target_base = _extract_base_type(target_type)
        target_words = _type_words(target_type)

        # Score 4: exact return type match
        if self.return_type:
            ret_normalized = _normalize_type(self.return_type)
            if ret_normalized == target_normalized:
                return 4
            # Score 3: return type contains/is contained by target base type
            ret_base = _extract_base_type(self.return_type)
            if ret_base and target_base and (
                ret_base == target_base
                or ret_normalized in target_normalized
                or target_normalized in ret_normalized
            ):
                return 3

        # Score 2: parameter type matches target
        for pt in self.param_types:
            pt_normalized = _normalize_type(pt)
            if pt_normalized == target_normalized:
                return 2
            pt_base = _extract_base_type(pt)
            if pt_base and target_base and pt_base == target_base:
                return 2

        # Score 1: shared type words (loose association)
        all_my_types = ([self.return_type] if self.return_type else []) + self.param_types
        all_my_words: set[str] = set()
        for t in all_my_types:
            all_my_words.update(_type_words(t))
        if target_words & all_my_words:
            return 1

        return 0


def _normalize_type(type_str: str) -> str:
    """Normalize a type string for comparison.

    Strips Optional[], whitespace, and common wrappers. Case-insensitive.
    """
    s = type_str.strip()
    # Strip Optional[...] wrapper
    m = re.match(r'^Optional\[(.+)\]$', s, re.IGNORECASE)
    if m:
        s = m.group(1).strip()
    # Strip leading/trailing whitespace within brackets
    s = re.sub(r'\s+', ' ', s)
    return s.lower()


def _extract_base_type(type_str: str) -> Optional[str]:
    """Extract the outermost base type name.

    list[User] → list
    Optional[dict[str, int]] → dict
    User → User
    """
    s = type_str.strip()
    # Strip Optional wrapper first
    m = re.match(r'^Optional\[(.+)\]$', s, re.IGNORECASE)
    if m:
        s = m.group(1).strip()
    # Get the part before [
    bracket = s.find('[')
    if bracket > 0:
        return s[:bracket].strip().lower()
    # Get the part before <  (for generics like Array<string>)
    angle = s.find('<')
    if angle > 0:
        return s[:angle].strip().lower()
    return s.strip().lower()


def _type_words(type_str: str) -> set[str]:
    """Extract meaningful words from a type string.

    list[User] → {'list', 'user'}
    dict[str, ShortURL] → {'dict', 'str', 'shorturl'}
    Optional[Page] → {'page'}  (strips Optional as it's structural, not semantic)
    """
    if not type_str:
        return set()
    s = type_str.strip()
    # Remove Optional wrapper
    m = re.match(r'^Optional\[(.+)\]$', s, re.IGNORECASE)
    if m:
        s = m.group(1)
    # Split on brackets, commas, spaces, angle brackets
    tokens = re.split(r'[\[\]<>,\s|]+', s)
    # Filter out empty strings and common noise words
    noise = {'none', 'any', 'optional', 'union', 'type', ''}
    return {t.lower() for t in tokens if t.lower() not in noise}


# ─── Behavioral Spec ──────────────────────────────────────────────────────────

@dataclass
class BehaviorSpec:
    """Structured behavioral contract attached to a hole via @mole: comments.

    These travel WITH the code — they're structured comments, not separate files.
    Other tools (test generators, doc generators, composition checkers) can parse them.
    """
    behavior: Optional[str] = None      # what this code should do (human-readable)
    requires: Optional[str] = None      # preconditions this code assumes
    ensures: Optional[str] = None       # postconditions this code guarantees
    approach: Optional[str] = None      # which expansion approach was chosen


# ─── Hole ─────────────────────────────────────────────────────────────────────

@dataclass
class Hole:
    """A typed hole in source code — the atomic unit of mole.

    A hole is a placeholder for code the human hasn't written yet.
    It carries type information, behavioral specs, and provenance.
    """
    line_no: int                            # 1-indexed line in source
    description: str                        # natural language intent (from hole("..."))
    expected_type: Optional[str] = None     # inferred type annotation
    var_name: Optional[str] = None          # variable being assigned to
    status: HoleStatus = HoleStatus.UNFILLED
    behavior: BehaviorSpec = field(default_factory=BehaviorSpec)

    # Hierarchy
    parent_id: Optional[str] = None         # hole ID this was expanded from
    children: list[str] = field(default_factory=list)  # sub-hole IDs from expansion

    # Fill result
    fill_code: Optional[str] = None         # the implementation (when filled)
    filled_by: Optional[str] = None         # provenance: "sonnet@2026-03-03"

    # Context (populated at fill/expand time, not stored)
    context_lines: Optional[str] = None     # surrounding code
    is_bare: bool = False                   # side-effect hole (no assignment)
    is_return: bool = False                 # return hole

    # Content anchors — N lines before/after the hole, used for context-anchored
    # matching when line numbers shift after user edits.
    # Populated by discover() via _populate_hole_context().
    context_before: Optional[str] = None    # N lines before the hole (joined by \n)
    context_after: Optional[str] = None     # N lines after the hole (joined by \n)

    @property
    def id(self) -> str:
        """Unique identifier for this hole within a file."""
        return f"L{self.line_no}"


# ─── Expansion Result ─────────────────────────────────────────────────────────

@dataclass
class Expansion:
    """One possible decomposition of a hole into sub-holes.

    expand() produces one of these. diversify() produces several for the human to choose from.
    """
    approach_name: str              # short label: "regex-based", "state machine", etc.
    approach_description: str       # 1-2 sentence explanation
    expanded_code: str              # real code with hole() calls for sub-tasks
    sub_holes: list[Hole] = field(default_factory=list)  # holes found in expanded_code


# ─── Context Layer ────────────────────────────────────────────────────────────

class ContextLayer(Protocol):
    """One composable layer of context for prompt assembly.

    Each layer extracts one kind of information from the source and formats
    it as a prompt fragment. Layers are independently toggleable.
    """
    name: str

    def build(self, hole: Hole, source: str, path: Path) -> str:
        """Return a prompt fragment for this layer, or empty string to skip."""
        ...


# ─── Filler ───────────────────────────────────────────────────────────────────

@dataclass
class FillerConfig:
    """Exposed, tunable configuration for any filler.

    Every config field is visible and adjustable by the human.
    No hidden state.
    """
    model: str = "sonnet"
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout: int = 240
    effort: str = "medium"
    streaming: bool = True


class Filler(Protocol):
    """Dumb pipe. Takes a prompt string, returns a code string.

    Filler knows NOTHING about holes, types, context, or mole.
    It receives an assembled prompt and returns raw LLM output.
    All configuration is exposed via FillerConfig.
    """
    config: FillerConfig

    def fill(self, prompt: str) -> str:
        """Send prompt to LLM, return raw code string."""
        ...


# ─── Verify Result ────────────────────────────────────────────────────────────

@dataclass
class VerifyResult:
    """Result of type-checking a fill against the source."""
    success: bool
    errors: list[str] = field(default_factory=list)
    baseline_errors: list[str] = field(default_factory=list)  # pre-existing errors
    new_errors: list[str] = field(default_factory=list)       # errors introduced by fill


# ─── Hole Group ───────────────────────────────────────────────────────────────

@dataclass
class HoleGroup:
    """Group of holes sharing a structural type pattern (from anti-unification).

    antiunify_holes() produces a list of these when it finds holes that
    could potentially be served by a single polymorphic helper function.

    The 'pattern' is the anti-unified type string, e.g. "list[T0]" for a group
    of holes that all want list[str], list[int], list[bytes], etc.

    'type_vars' maps each type variable in the pattern to the concrete types seen
    across member holes, e.g. {"T0": ["str", "int", "bytes"]}.

    Design note: anti-unification reveals compositional opportunities. When N holes
    share pattern list[T0], a single generic helper `def collect(items: list[T]) -> list[T]`
    could serve all of them — the T variable is the axis of variation.
    """
    pattern: str                            # Anti-unified type pattern, e.g. "list[T0]"
    type_vars: dict[str, list[str]] = field(default_factory=dict)  # T0 → ["str", "int", ...]
    holes: list[Hole] = field(default_factory=list)                 # Member holes

    @property
    def size(self) -> int:
        return len(self.holes)

    @property
    def is_polymorphic(self) -> bool:
        """True if the pattern contains type variables (holes differ in concrete type)."""
        return bool(self.type_vars)

    @property
    def label(self) -> str:
        """Short human-readable label for CLI display."""
        names = [h.var_name or f"L{h.line_no}" for h in self.holes]
        return f"{self.pattern} × {len(self.holes)} [{', '.join(names)}]"


# ─── File State ───────────────────────────────────────────────────────────────

@dataclass
class MoleFile:
    """Complete state of a file being worked on with mole.

    Tracks all holes, their statuses, and the current source.
    """
    path: Path
    source: str
    holes: list[Hole] = field(default_factory=list)
    language: str = "python"  # detected from extension

    @property
    def unfilled(self) -> list[Hole]:
        return [h for h in self.holes if h.status == HoleStatus.UNFILLED]

    @property
    def filled(self) -> list[Hole]:
        return [h for h in self.holes if h.status in (HoleStatus.FILLED, HoleStatus.VERIFIED)]
