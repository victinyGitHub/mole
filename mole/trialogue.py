"""mole — Trialogue error loop for fill operations.

Replaces the simple "append errors to prompt" retry with a structured
conversation format. The LLM sees its own previous attempt + specific
type errors in a clear turn-based format.

Research basis: ChatLSP (OOPSLA 2024) ablation study:
  - No context:                    ~5% pass rate
  - Types only:                   ~20%
  - Types + headers:              ~60%
  - Types + headers + error loop: ~85-90%

Mode: Structured single-prompt (works with any filler — dumb pipe preserved).
Formats the conversation as role-separated sections within one prompt.
The filler protocol stays `fill(str) -> str`.

Max 2 correction rounds — research shows diminishing returns after that.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .types import Hole, Filler, VerifyResult


# ─── Turn Tracking ───────────────────────────────────────────────────────────

@dataclass
class TrialogueTurn:
    """One turn in the trialogue conversation."""
    attempt_code: str           # the code the LLM generated
    errors: list[str]           # type errors found in this attempt
    error_count: int = 0        # len(errors) — cached for comparison
    was_improvement: bool = True  # fewer errors than previous turn?


@dataclass
class TrialogueState:
    """Full state of a trialogue fill session.

    Tracks all attempts so we can:
    - Show the LLM its own history
    - Detect if corrections are making things worse (bail early)
    - Pick the best attempt even if none are perfect
    """
    initial_prompt: str
    turns: list[TrialogueTurn] = field(default_factory=list)
    max_corrections: int = 2
    best_code: str = ""
    best_error_count: Optional[int] = None  # None = no attempts yet

    @property
    def total_attempts(self) -> int:
        return len(self.turns)

    @property
    def should_continue(self) -> bool:
        """Whether another correction round is worth attempting.

        Stops if:
        - Max correction rounds reached
        - Last attempt was clean (0 errors)
        - Last correction was a regression (more errors than previous)
        """
        if not self.turns:
            return False  # No attempts yet — need initial fill first
        if self.total_attempts > self.max_corrections:
            return False  # Exhausted correction budget
        last = self.turns[-1]
        if last.error_count == 0:
            return False  # Clean — no corrections needed
        if self.total_attempts >= 2 and not last.was_improvement:
            return False  # Regression — corrections making it worse
        return True

    def record_turn(self, code: str, errors: list[str]) -> None:
        """Record an attempt and update best-so-far tracking."""
        error_count = len(errors)
        prev_count = self.turns[-1].error_count if self.turns else error_count + 1
        was_improvement = error_count < prev_count

        turn = TrialogueTurn(
            attempt_code=code,
            errors=errors,
            error_count=error_count,
            was_improvement=was_improvement,
        )

        if self.best_error_count is None or error_count < self.best_error_count:
            self.best_code = code
            self.best_error_count = error_count

        self.turns.append(turn)


# ─── Prompt Formatting ───────────────────────────────────────────────────────

def format_error_summary(errors: list[str], max_errors: int = 5) -> str:
    """Format type errors for the correction prompt.

    Groups similar errors (same message, different lines), caps at
    max_errors. Includes count of omitted errors if truncated.
    """
    if not errors:
        return ""

    # Deduplicate by error message (strip line numbers for comparison)
    seen_messages: set[str] = set()
    unique: list[str] = []
    for err in errors:
        # Extract message part after "L<num>: "
        msg = err.split(": ", 1)[-1] if ": " in err else err
        if msg not in seen_messages:
            seen_messages.add(msg)
            unique.append(err)

    # Truncate and format
    shown = unique[:max_errors]
    lines = [f"- {err}" for err in shown]

    omitted = len(errors) - len(shown)
    if omitted > 0:
        lines.append(f"... and {omitted} more errors")

    return "\n".join(lines)


def format_correction_prompt(state: TrialogueState) -> str:
    """Build a structured correction prompt from trialogue state.

    Formats the conversation as role-separated sections within one prompt:

    ## INITIAL TASK
    {original context + constraints}

    ## YOUR PREVIOUS ATTEMPT
    ```
    {code from last turn}
    ```

    ## TYPE CHECKER FEEDBACK
    {formatted errors}

    ## CORRECTION TASK
    Fix these specific errors. Preserve working parts.
    Return ONLY the corrected code.
    """
    if not state.turns:
        return state.initial_prompt

    last_turn = state.turns[-1]
    error_text = format_error_summary(last_turn.errors)

    return (
        f"## INITIAL TASK\n"
        f"{state.initial_prompt}\n\n"
        f"## YOUR PREVIOUS ATTEMPT\n"
        f"```\n{last_turn.attempt_code}\n```\n\n"
        f"## TYPE CHECKER FEEDBACK\n"
        f"The following type errors were found in your code:\n"
        f"{error_text}\n\n"
        f"## CORRECTION TASK\n"
        f"Fix these specific errors. Preserve the parts that work correctly.\n"
        f"Return ONLY the corrected code."
    )


# ─── Main Loop ───────────────────────────────────────────────────────────────

def trialogue_fill(
    hole_target: Hole,
    initial_prompt: str,
    filler: Filler,
    verify_fn: Callable[[str], VerifyResult],
    max_corrections: int = 2,
    on_chunk: Optional[Callable] = None,
    on_turn: Optional[Callable[[int, str, list[str]], None]] = None,
) -> tuple[str, VerifyResult, TrialogueState]:
    """Execute a trialogue fill loop.

    Drop-in replacement for the retry loop in operations.fill().

    Flow:
    1. Fill with initial_prompt (stream if on_chunk provided)
    2. Verify the fill
    3. If errors and should_continue: format correction prompt, re-fill
    4. Repeat until clean, regression, or max_corrections reached
    5. Return the best attempt across all turns

    Args:
        hole_target: The hole being filled.
        initial_prompt: The assembled fill prompt.
        filler: LLM filler (dumb pipe — fill(str) -> str).
        verify_fn: Takes cleaned code string, returns VerifyResult.
        max_corrections: Max correction rounds (default 2).
        on_chunk: Streaming callback for first attempt only.
        on_turn: Progress callback(turn_idx, code, errors).

    Returns:
        (best_code, best_verify_result, state)
    """
    state = TrialogueState(
        initial_prompt=initial_prompt,
        max_corrections=max_corrections,
    )

    # Track the best verify result alongside the best code
    best_verify = VerifyResult(success=False)
    current_prompt = initial_prompt

    while True:
        # Generate fill — stream only on first attempt
        is_first = state.total_attempts == 0
        if is_first and on_chunk:
            raw_code = filler.fill(current_prompt, on_chunk=on_chunk)
        else:
            raw_code = filler.fill(current_prompt)

        # Verify the fill
        result = verify_fn(raw_code)

        # Record this attempt
        state.record_turn(raw_code, result.new_errors)

        # Update best verify result
        if result.success or len(result.new_errors) < len(best_verify.new_errors):
            best_verify = result

        # Notify caller
        if on_turn:
            on_turn(state.total_attempts - 1, raw_code, result.new_errors)

        # Clean fill — done
        if result.success:
            break

        # Check if we should try another correction
        if not state.should_continue:
            break

        # Build structured correction prompt for next attempt
        current_prompt = format_correction_prompt(state)

    return state.best_code, best_verify, state
