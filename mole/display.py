"""mole — Terminal display with rich formatting.

Inspired by Claude Code CLI's design: clean panels, syntax highlighting,
spinners during LLM calls, colored status badges.

Falls back to plain text if rich is not available.
"""
from __future__ import annotations

import sys
import textwrap
import threading
import time
from contextlib import contextmanager
from typing import Optional

from .types import Hole, HoleStatus, MoleFile

# ─── Rich Setup ──────────────────────────────────────────────────────────────

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
    from rich.live import Live
    from rich.spinner import Spinner
    from rich.columns import Columns
    from rich.markup import escape
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# Single console instance — stderr so we don't pollute stdout
console = Console(stderr=True) if HAS_RICH else None


# ─── Color Palette ───────────────────────────────────────────────────────────
# Derived from megelia.me — dark purple monochrome with lavender accents
# bg: rgb(42,40,55) · text: rgb(169,169,169) · accent: rgb(158,149,252)
# links: rgb(149,131,154) · headings: rgb(154,143,191) · border: rgb(83,73,104)

COLORS = {
    "primary": "#9e95fc",      # periwinkle — main accent (hover-link)
    "secondary": "#cdc2db",    # light lavender — success (h2)
    "warning": "#c2b090",      # muted gold — warnings (complements purple)
    "error": "#c47a8a",        # dusty rose — errors (purple-family warm)
    "dim": "#7c7a81",          # muted grey — secondary text (h4)
    "type": "#9a8fbf",         # medium purple — type annotations (h3)
    "desc": "#a9a9a9",         # grey — descriptions (body text)
    "hole": "#95839a",         # dusty mauve — hole markers (link color)
    "filled": "#cdc2db",       # light lavender — filled status
    "expanded": "#9e95fc",     # periwinkle — expanded status
    "muted": "#534968",        # purple border — borders, separators
}


# ─── Status Display ──────────────────────────────────────────────────────────

STATUS_STYLES = {
    HoleStatus.UNFILLED: ("○", f"[{COLORS['hole']}]unfilled[/]"),
    HoleStatus.EXPANDED: ("◎", f"[{COLORS['expanded']}]expanded[/]"),
    HoleStatus.FILLED: ("●", f"[{COLORS['filled']}]filled[/]"),
    HoleStatus.VERIFIED: ("✓", f"[{COLORS['filled']}]verified[/]"),
}


def _status_badge(hole: Hole) -> str:
    """Colored status badge for rich output."""
    icon, label = STATUS_STYLES.get(hole.status, ("?", "unknown"))
    return f"{icon} {label}"


# ─── Spinner Context Manager ────────────────────────────────────────────────

@contextmanager
def spinner(message: str):
    """Show a spinner during long operations (LLM calls etc).

    Usage:
        with spinner("Filling hole..."):
            result = filler.fill(prompt)
    """
    if not HAS_RICH:
        print(f"  {message}")
        yield
        return

    sp = Spinner("dots", text=Text(f" {message}", style=COLORS["dim"]))
    with Live(sp, console=console, refresh_per_second=12, transient=True):
        yield


@contextmanager
def timer_spinner(message: str):
    """Spinner with elapsed time display.

    Usage:
        with timer_spinner("Generating 3 approaches...") as t:
            result = diversify(...)
        print(f"Done in {t.elapsed:.1f}s")
    """
    class TimerResult:
        elapsed: float = 0.0

    result = TimerResult()

    if not HAS_RICH:
        print(f"  {message}")
        t0 = time.monotonic()
        yield result
        result.elapsed = time.monotonic() - t0
        return

    t0 = time.monotonic()

    def _update_text():
        while not done:
            elapsed = time.monotonic() - t0
            sp.text = Text(f" {message} [{elapsed:.0f}s]", style=COLORS["dim"])
            time.sleep(0.1)

    done = False
    sp = Spinner("dots", text=Text(f" {message}", style=COLORS["dim"]))
    updater = threading.Thread(target=_update_text, daemon=True)

    with Live(sp, console=console, refresh_per_second=12, transient=True):
        updater.start()
        yield result
        done = True
        result.elapsed = time.monotonic() - t0

    updater.join(timeout=0.5)


# ─── Streaming Display ────────────────────────────────────────────────────────

class StreamingCodePanel:
    """Live-updating code panel that shows LLM output forming in real-time.

    Usage:
        panel = StreamingCodePanel(lang="python", title="Filling L42...")
        panel.start()
        for chunk in filler.stream_fill(prompt):
            panel.update(chunk)
        panel.finish()
        final_code = panel.text
    """

    def __init__(self, lang: str = "python", title: str = "Streaming..."):
        self.lang = lang
        self.title = title
        self.text = ""
        self._live: Optional[Live] = None
        self._start_time = 0.0

    def start(self) -> None:
        """Begin the live display."""
        self._start_time = time.monotonic()
        if not HAS_RICH:
            print(f"  {self.title}")
            return
        self._live = Live(
            self._render(),
            console=console,
            refresh_per_second=8,
            transient=False,
        )
        self._live.start()

    def update(self, chunk: str) -> None:
        """Append a text chunk and refresh the display."""
        self.text += chunk
        if self._live:
            self._live.update(self._render())
        elif not HAS_RICH:
            # Plain text fallback: just print chunks
            sys.stderr.write(chunk)
            sys.stderr.flush()

    def finish(self) -> None:
        """Stop the live display, show final panel."""
        elapsed = time.monotonic() - self._start_time
        if self._live:
            self._live.update(self._render(done=True, elapsed=elapsed))
            self._live.stop()
            self._live = None
        elif not HAS_RICH:
            sys.stderr.write(f"\n  Done in {elapsed:.1f}s\n")

    def _render(self, done: bool = False, elapsed: float = 0.0):
        """Render current state as a Rich Panel with syntax highlighting."""
        code = self.text or " "
        syntax = Syntax(
            code, self.lang, theme="dracula",
            line_numbers=True, word_wrap=False,
        )

        if done:
            subtitle = f"[{COLORS['dim']}]{elapsed:.1f}s[/]"
        else:
            elapsed_now = time.monotonic() - self._start_time
            lines = self.text.count("\n") + (1 if self.text else 0)
            subtitle = f"[{COLORS['dim']}]{elapsed_now:.0f}s · {lines} lines[/]"

        return Panel(
            syntax,
            title=f"[{COLORS['primary']}]{self.title}[/]",
            subtitle=subtitle,
            border_style=COLORS["muted"],
            padding=(0, 1),
        )


def stream_or_spin(filler, prompt: str, message: str, lang: str = "python"):
    """Smart dispatch: stream with live panel if filler supports it, else spin.

    Returns the final code string (already fence-stripped).

    Usage:
        code = stream_or_spin(filler, prompt, "Filling L42...", lang="python")
    """
    # Only ClaudeCLIFiller supports streaming
    if hasattr(filler, 'stream_fill') and getattr(filler.config, 'streaming', False):
        panel = StreamingCodePanel(lang=lang, title=message)
        panel.start()
        try:
            gen = filler.stream_fill(prompt)
            final = ""
            try:
                while True:
                    chunk = next(gen)
                    panel.update(chunk)
            except StopIteration as e:
                final = e.value or ""
            panel.finish()
            return final if final else panel.text
        except Exception:
            panel.finish()
            raise
    else:
        # Fallback: spinner + blocking fill
        with spinner(message):
            return filler.fill(prompt)


# ─── Parallel Streaming Display (Diversify) ──────────────────────────────────

class DiversifyStreamingDisplay:
    """3 side-by-side streaming panels for parallel diversify expansions.

    Each panel independently receives text chunks from its thread.
    A single Rich Live context renders all 3 as a Columns layout.

    Buffered rendering: chunks accumulate in buffers, a background timer
    flushes to the Live display every FLUSH_INTERVAL_MS. This prevents
    glitchy re-renders from high-frequency token deltas across 3 threads.

    After streaming completes, shows the final side-by-side view and waits
    for user input: 'c' to continue to vertical detail view, or pick directly.

    Usage:
        display = DiversifyStreamingDisplay(n=3, lang="python")
        display.start()

        # From thread i:
        display.update(i, chunk_text)
        display.set_title(i, "Approach: recursion")

        display.finish()  # shows final side-by-side + waits for 'c'
    """

    FLUSH_INTERVAL = 0.15  # seconds between display refreshes

    def __init__(self, n: int = 3, lang: str = "python"):
        self.n = n
        self.lang = lang
        self.texts: list[str] = [""] * n
        self.titles: list[str] = [f"Approach {i+1}" for i in range(n)]
        self._live: Optional[Live] = None
        self._lock = threading.Lock()
        self._start_time = 0.0
        self._dirty = False
        self._flush_thread: Optional[threading.Thread] = None
        self._done = False

    def start(self) -> None:
        """Begin the live display."""
        self._start_time = time.monotonic()
        self._done = False
        if not HAS_RICH:
            print(f"  Generating {self.n} approaches...")
            return
        self._live = Live(
            self._render(),
            console=console,
            refresh_per_second=4,
            transient=True,
        )
        self._live.start()
        # Start background flush thread
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()

    def _flush_loop(self) -> None:
        """Background thread that flushes buffered updates at fixed intervals."""
        while not self._done:
            time.sleep(self.FLUSH_INTERVAL)
            if self._dirty and self._live:
                with self._lock:
                    self._dirty = False
                    self._live.update(self._render())

    def update(self, idx: int, chunk: str) -> None:
        """Append text to panel idx. Thread-safe. Buffered — marks dirty."""
        with self._lock:
            self.texts[idx] += chunk
            self._dirty = True

    def set_title(self, idx: int, title: str) -> None:
        """Set the title for panel idx. Thread-safe."""
        with self._lock:
            self.titles[idx] = title
            self._dirty = True

    def finish(self) -> None:
        """Stop streaming, show final side-by-side panels (non-transient)."""
        self._done = True
        elapsed = time.monotonic() - self._start_time
        if self._flush_thread:
            self._flush_thread.join(timeout=0.5)
            self._flush_thread = None
        if self._live:
            # Final flush
            with self._lock:
                self._live.update(self._render(done=True, elapsed=elapsed))
            self._live.stop()
            self._live = None
            # Re-print the final side-by-side as a permanent (non-transient) output
            console.print(self._render(done=True, elapsed=elapsed))
        elif not HAS_RICH:
            for i in range(self.n):
                lines = self.texts[i].count("\n") + 1
                print(f"  [{i+1}] {self.titles[i]}: {lines} lines")
            print(f"  Done in {elapsed:.1f}s")

    def _render(self, done: bool = False, elapsed: float = 0.0):
        """Render all panels as side-by-side columns."""
        panels = []
        elapsed_now = elapsed if done else (time.monotonic() - self._start_time)
        panel_width = (console.width // self.n - 1) if console else 40

        for i in range(self.n):
            code = self.texts[i] or " "
            syntax = Syntax(
                code, self.lang, theme="dracula",
                line_numbers=False, word_wrap=True,
            )
            lines = self.texts[i].count("\n") + (1 if self.texts[i] else 0)

            if done:
                subtitle = f"[{COLORS['dim']}]{lines}L · {elapsed_now:.1f}s[/]"
            else:
                subtitle = f"[{COLORS['dim']}]{lines}L · {elapsed_now:.0f}s[/]"

            panel = Panel(
                syntax,
                title=f"[{COLORS['primary']}][{i+1}] {self.titles[i]}[/]",
                subtitle=subtitle,
                border_style=COLORS["muted"] if not done else COLORS["primary"],
                padding=(0, 1),
                width=panel_width,
            )
            panels.append(panel)

        return Columns(panels, equal=True, expand=True)


# ─── Hole Display ────────────────────────────────────────────────────────────

def show_holes(dfile: MoleFile) -> None:
    """Display all holes with rich formatting."""
    if not HAS_RICH:
        _show_holes_plain(dfile)
        return

    if not dfile.holes:
        console.print(f"  [dim]No holes found in {dfile.path.name}[/dim]")
        return

    # Header
    console.print()
    title = Text()
    title.append("  ", style="bold")
    title.append(dfile.path.name, style=f"bold {COLORS['primary']}")
    title.append(f" — {len(dfile.holes)} hole(s)", style=COLORS["dim"])
    console.print(title)
    console.print()

    for i, h in enumerate(dfile.holes, 1):
        _render_hole_row(i, h)

    console.print()


def _render_hole_row(idx: int, h: Hole) -> None:
    """Render a single hole row with color."""
    # Line number + index
    line = Text()
    line.append(f"  [{idx}]", style=f"bold {COLORS['dim']}")
    line.append(f" L{h.line_no}", style="bold")
    line.append(f"  {_status_badge(h)}", style="")

    # Type annotation
    if h.expected_type:
        line.append(f"  {h.var_name}: ", style=COLORS["dim"]) if h.var_name else None
        line.append(f"{h.expected_type}", style=COLORS["type"])

    # Kind tag
    if h.is_return:
        line.append("  return", style=COLORS["dim"])
    elif h.is_bare:
        line.append("  bare", style=COLORS["dim"])

    console.print(line)

    # Description
    desc = Text()
    desc.append(f'      "{h.description}"', style=COLORS["desc"])
    console.print(desc)

    # Behavioral spec
    spec = h.behavior
    if spec.behavior:
        console.print(f"      [dim]behavior:[/] {spec.behavior}")
    if spec.requires:
        console.print(f"      [dim]requires:[/] {spec.requires}")
    if spec.ensures:
        console.print(f"      [dim]ensures:[/] {spec.ensures}")


def show_hole_detail(hole: Hole, source: str, backend) -> None:
    """Show detailed info about a single hole."""
    if not HAS_RICH:
        _show_hole_detail_plain(hole, source, backend)
        return

    console.print()

    # Header panel
    title = Text()
    title.append(f"L{hole.line_no}", style="bold")
    title.append(f"  {_status_badge(hole)}")
    console.print(f"  {title}")
    console.print()

    # Info grid
    if hole.description:
        console.print(f"  [{COLORS['desc']}]Description:[/] {hole.description}")
    if hole.expected_type:
        console.print(f"  [{COLORS['type']}]Type:[/] {hole.expected_type}")
    if hole.var_name:
        console.print(f"  [dim]Variable:[/] {hole.var_name}")

    kind = "return" if hole.is_return else "bare" if hole.is_bare else "assignment"
    console.print(f"  [dim]Kind:[/] {kind}")

    # Behavioral spec
    spec = hole.behavior
    if spec.behavior:
        console.print(f"  [dim]Behavior:[/] {spec.behavior}")
    if spec.requires:
        console.print(f"  [dim]Requires:[/] {spec.requires}")
    if spec.ensures:
        console.print(f"  [dim]Ensures:[/] {spec.ensures}")

    # Fill code
    if hole.fill_code:
        console.print()
        console.print(f"  [dim]Fill:[/]")
        _print_code(hole.fill_code, indent=4)

    if hole.filled_by:
        console.print(f"  [dim]Filled by:[/] {hole.filled_by}")

    # Enclosing block
    enclosing = backend.find_enclosing_block(source, hole.line_no)
    if enclosing:
        console.print()
        console.print(f"  [dim]Enclosing block:[/]")
        _print_code(enclosing, indent=4)

    console.print()


# ─── Code Display ────────────────────────────────────────────────────────────

def _detect_lang(code: str = "", path_hint: str = "") -> str:
    """Detect language for syntax highlighting."""
    if path_hint.endswith((".ts", ".tsx")):
        return "typescript"
    if path_hint.endswith((".js", ".jsx")):
        return "javascript"
    if path_hint.endswith(".rs"):
        return "rust"
    if path_hint.endswith(".go"):
        return "go"
    # Default to python
    return "python"


def _print_code(code: str, lang: str = "python", indent: int = 0) -> None:
    """Print syntax-highlighted code."""
    if not HAS_RICH:
        for line in code.splitlines():
            print(" " * indent + line)
        return

    syntax = Syntax(
        code.strip(),
        lang,
        theme="dracula",
        line_numbers=False,
        padding=(0, 0),
    )
    # Indent by wrapping in spaces
    for line in console.render_str(str(syntax)).splitlines() if indent == 0 else []:
        console.print(line)

    if indent > 0:
        prefix = " " * indent
        for line in code.strip().splitlines():
            # Use rich's syntax highlighting per-line
            console.print(f"{prefix}{escape(line)}", highlight=False)


def print_code_panel(code: str, title: str = "", lang: str = "python") -> None:
    """Print code in a bordered panel with syntax highlighting."""
    if not HAS_RICH:
        print(f"\n{'─' * 40}")
        if title:
            print(f"  {title}")
            print(f"{'─' * 40}")
        print(code)
        return

    syntax = Syntax(
        code.strip(),
        lang,
        theme="dracula",
        line_numbers=False,
        padding=(0, 1),
    )
    panel = Panel(
        syntax,
        title=f"[bold]{title}[/]" if title else None,
        border_style=COLORS["muted"],
        box=box.ROUNDED,
        padding=(0, 1),
    )
    console.print(panel)


# ─── Expansion Display ──────────────────────────────────────────────────────

def show_expansion(exp, idx: int = 0, lang: str = "python") -> None:
    """Display an expansion with styled panel and sub-holes."""
    if not HAS_RICH:
        _show_expansion_plain(exp, idx)
        return

    console.print()

    # Title
    if idx > 0:
        title_text = Text()
        title_text.append(f"  Expansion {idx}: ", style=f"bold {COLORS['dim']}")
        title_text.append(exp.approach_name, style=f"bold {COLORS['primary']}")
    else:
        title_text = Text()
        title_text.append("  Approach: ", style=f"bold {COLORS['dim']}")
        title_text.append(exp.approach_name, style=f"bold {COLORS['primary']}")

    console.print(title_text)

    if exp.approach_description:
        console.print(f"  [{COLORS['dim']}]{exp.approach_description}[/]")

    # Code panel
    syntax = Syntax(
        exp.expanded_code.strip(),
        lang,
        theme="dracula",
        line_numbers=True,
        padding=(0, 1),
    )
    panel = Panel(
        syntax,
        border_style=COLORS["muted"],
        box=box.ROUNDED,
        padding=(0, 1),
    )
    console.print(panel)

    # Sub-holes
    if exp.sub_holes:
        sub_text = Text()
        sub_text.append(f"  Sub-holes ", style=COLORS["dim"])
        sub_text.append(f"({len(exp.sub_holes)})", style=f"bold {COLORS['hole']}")
        sub_text.append(":", style=COLORS["dim"])
        console.print(sub_text)

        for sh in exp.sub_holes:
            sh_line = Text()
            sh_line.append(f"    L{sh.line_no}", style="bold")
            sh_line.append(" — ", style=COLORS["dim"])
            sh_line.append(sh.description, style=COLORS["desc"])
            if sh.expected_type:
                sh_line.append(f": {sh.expected_type}", style=COLORS["type"])
            console.print(sh_line)


# ─── Verify Display ─────────────────────────────────────────────────────────

def show_verify_result(result) -> None:
    """Display verification result with color."""
    if not HAS_RICH:
        _show_verify_plain(result)
        return

    if result.success:
        console.print(f"  [{COLORS['filled']}]✓ Type check passed[/] — no new errors")
    else:
        console.print(
            f"  [{COLORS['error']}]✗ Type check failed[/] — "
            f"{len(result.new_errors)} new error(s):"
        )
        for err in result.new_errors[:10]:
            console.print(f"    [{COLORS['dim']}]{escape(err)}[/]")
        if len(result.new_errors) > 10:
            console.print(f"    [{COLORS['dim']}]... and {len(result.new_errors) - 10} more[/]")


# ─── Prompt & Welcome ───────────────────────────────────────────────────────

def print_welcome(dfile: MoleFile) -> None:
    """Print the welcome banner and initial hole list."""
    if not HAS_RICH:
        show_holes(dfile)
        print("Commands: show <n>, expand <n>, diversify <n>, fill <n>, edit <line> <desc>,")
        print("          propagate, groups, context <n>, verify <n>, apply, undo, reload, quit\n")
        return

    # Banner
    console.print()
    banner = Text()
    banner.append("  🕳 ", style=f"bold {COLORS['primary']}")
    banner.append("mole", style=f"bold {COLORS['primary']}")
    console.print(banner)
    console.print(f"  [{COLORS['dim']}]dig holes · fill code · grow programs[/]")
    console.print()

    show_holes(dfile)

    # Command hints
    cmds = Text()
    cmds.append("  Commands: ", style=COLORS["dim"])
    cmd_names = ["show", "expand", "diversify", "fill", "edit", "propagate", "groups",
                 "context", "verify", "apply", "undo", "reload", "quit"]
    for i, cmd in enumerate(cmd_names):
        cmds.append(cmd, style=COLORS["primary"])
        if i < len(cmd_names) - 1:
            cmds.append(" · ", style=COLORS["muted"])
    console.print(cmds)
    console.print()


def get_prompt() -> str:
    """Return the styled prompt string for input()."""
    if not HAS_RICH:
        return "mole> "
    # ANSI escape codes directly since input() doesn't use rich
    # Periwinkle from megelia.me: rgb(158,149,252)
    accent = "\033[38;2;158;149;252m"
    bold = "\033[1m"
    reset = "\033[0m"
    dim = "\033[38;2;83;73;104m"  # purple border from megelia
    return f"{bold}{accent}🕳 mole{reset}{dim}>{reset} "


# ─── Action Feedback ─────────────────────────────────────────────────────────

def print_applied(message: str) -> None:
    """Print an 'applied' confirmation."""
    if HAS_RICH:
        console.print(f"  [{COLORS['filled']}]✓[/] {message}")
    else:
        print(f"  ✓ {message}")


def print_skipped(message: str) -> None:
    """Print a 'skipped' message."""
    if HAS_RICH:
        console.print(f"  [{COLORS['dim']}]— {message}[/]")
    else:
        print(f"  — {message}")


def print_error(message: str) -> None:
    """Print an error message."""
    if HAS_RICH:
        console.print(f"  [{COLORS['error']}]✗ {message}[/]")
    else:
        print(f"  ✗ {message}")


def print_info(message: str) -> None:
    """Print an info message."""
    if HAS_RICH:
        console.print(f"  [{COLORS['dim']}]{message}[/]")
    else:
        print(f"  {message}")


def print_fill_result(code: str, lang: str = "python") -> None:
    """Print a fill result with syntax highlighting."""
    if not HAS_RICH:
        print(f"\n  Fill result:\n{textwrap.indent(code, '    ')}")
        return

    console.print()
    console.print(f"  [{COLORS['dim']}]Fill result:[/]")
    syntax = Syntax(
        code.strip(),
        lang,
        theme="dracula",
        line_numbers=False,
        padding=(0, 1),
    )
    panel = Panel(
        syntax,
        border_style=COLORS["muted"],
        box=box.ROUNDED,
        padding=(0, 1),
    )
    console.print(panel)


# ─── Plain Text Fallbacks ───────────────────────────────────────────────────

def _show_holes_plain(dfile: MoleFile) -> None:
    """Plain text hole display (no rich)."""
    if not dfile.holes:
        print(f"  No holes found in {dfile.path.name}")
        return

    print(f"\nLoaded {dfile.path.name} — {len(dfile.holes)} hole(s) found\n")

    STATUS_ICONS = {
        HoleStatus.UNFILLED: "○",
        HoleStatus.EXPANDED: "◎",
        HoleStatus.FILLED: "●",
        HoleStatus.VERIFIED: "✓",
    }

    for i, h in enumerate(dfile.holes, 1):
        icon = STATUS_ICONS.get(h.status, "?")
        type_str = f"  {h.var_name}: {h.expected_type}" if h.expected_type else ""
        kind = ""
        if h.is_return:
            kind = " (return)"
        elif h.is_bare:
            kind = " (bare)"

        print(f"[{i}] L{h.line_no}  {icon} {h.status.value}{type_str}{kind}")
        print(f'    "{h.description}"')

        spec = h.behavior
        if spec.behavior:
            print(f"    behavior: {spec.behavior}")
        if spec.requires:
            print(f"    requires: {spec.requires}")
        if spec.ensures:
            print(f"    ensures: {spec.ensures}")
        print()


def _show_hole_detail_plain(hole: Hole, source: str, backend) -> None:
    """Plain text hole detail."""
    print(f"\n  Hole L{hole.line_no}: {hole.status.value}")
    print(f"  Description: {hole.description}")
    if hole.expected_type:
        print(f"  Type: {hole.expected_type}")
    if hole.var_name:
        print(f"  Variable: {hole.var_name}")
    kind = "return" if hole.is_return else "bare" if hole.is_bare else "assignment"
    print(f"  Kind: {kind}")

    spec = hole.behavior
    if spec.behavior:
        print(f"  Behavior: {spec.behavior}")
    if spec.requires:
        print(f"  Requires: {spec.requires}")
    if spec.ensures:
        print(f"  Ensures: {spec.ensures}")

    if hole.fill_code:
        print(f"  Fill:\n{textwrap.indent(hole.fill_code, '    ')}")
    if hole.filled_by:
        print(f"  Filled by: {hole.filled_by}")

    enclosing = backend.find_enclosing_block(source, hole.line_no)
    if enclosing:
        print(f"\n  Enclosing block:")
        print(textwrap.indent(enclosing, "    "))
    print()


def _show_expansion_plain(exp, idx: int = 0) -> None:
    """Plain text expansion display."""
    print(f"\n{'─' * 40}")
    if idx > 0:
        print(f"  Expansion {idx}: {exp.approach_name}")
    else:
        print(f"  Approach: {exp.approach_name}")
    if exp.approach_description:
        print(f"  {exp.approach_description}")
    print(f"{'─' * 40}")
    print(exp.expanded_code)
    if exp.sub_holes:
        print(f"\n  Sub-holes ({len(exp.sub_holes)}):")
        for sh in exp.sub_holes:
            type_str = f": {sh.expected_type}" if sh.expected_type else ""
            print(f"    L{sh.line_no} — {sh.description}{type_str}")


def _show_verify_plain(result) -> None:
    """Plain text verify display."""
    if result.success:
        print("  ✓ Type check passed — no new errors")
    else:
        print(f"  ✗ Type check failed — {len(result.new_errors)} new error(s):")
        for err in result.new_errors[:10]:
            print(f"    {err}")
        if len(result.new_errors) > 10:
            print(f"    ... and {len(result.new_errors) - 10} more")
