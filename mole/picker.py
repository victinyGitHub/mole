"""mole — Interactive approach picker for serve mode.

Client-side companion to server.py. Connects to the mole serve HTTP API,
displays hole status, previews diffs, and lets users pick approaches.

The serve loop generates diversify results in the background. This module
provides the interactive UX for reviewing and selecting approaches.

Usage from REPL:
  mole serve file.py       # in one terminal
  mole pick file.py        # in another (connects to serve API)

Or programmatically:
  client = ServeClient()
  status = client.status()
  approaches = client.get_diversify(hole_idx=1)
"""
from __future__ import annotations

import difflib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .types import Hole
from .operations import discover, apply as apply_fill
from .display import console, COLORS, HAS_RICH

if HAS_RICH:
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
    from rich import box


# ─── HTTP Client ──────────────────────────────────────────────────────────────

class ServeClient:
    """HTTP client for the mole serve API.

    Wraps urllib to talk to the serve mode's localhost HTTP API.
    No external dependencies — stdlib only.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 3077):
        self.base_url = f"http://{host}:{port}"

    def _get(self, path: str) -> dict:
        """GET request, return parsed JSON."""
        url = f"{self.base_url}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise ConnectionError(
                f"Cannot connect to mole serve at {self.base_url}. "
                f"Is 'mole serve' running? ({e})"
            )

    def _post(self, path: str) -> dict:
        """POST request, return parsed JSON."""
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method="POST", data=b"")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise ConnectionError(f"POST to {url} failed: {e}")

    def get_holes(self) -> list[dict]:
        """Fetch all holes + status from serve API."""
        data = self._get("/holes")
        return data.get("holes", [])

    def get_diversify(self, hole_idx: int) -> dict:
        """Fetch diversify results for a specific hole (1-indexed)."""
        return self._get(f"/holes/{hole_idx}/diversify")

    def pick(self, hole_idx: int, approach_idx: int) -> dict:
        """Pick approach k for hole n (both 1-indexed)."""
        return self._post(f"/holes/{hole_idx}/pick/{approach_idx}")

    def status(self) -> dict:
        """Get server status: file, hole count, pool stats."""
        return self._get("/status")

    def is_alive(self) -> bool:
        """Check if serve is running."""
        try:
            self.status()
            return True
        except ConnectionError:
            return False


# ─── Diff Preview ─────────────────────────────────────────────────────────────

def generate_diff(
    original_source: str,
    hole_line: int,
    approach_code: str,
    filename: str = "",
) -> str:
    """Generate a unified diff showing what an approach would change.

    Takes the original source, the line where the hole lives, and the
    approach's expanded code. Returns a unified diff string.

    The diff should show:
      - A few lines of context around the hole
      - The hole() call being replaced by the approach code
      - Proper unified diff format (--- / +++ / @@ headers)
    """
    lines = original_source.splitlines(keepends=True)

    if not (1 <= hole_line <= len(lines)):
        return f"# error: hole_line {hole_line} out of range (1-{len(lines)})"

    # Detect indentation from the hole line
    target = lines[hole_line - 1]
    indent = target[: len(target) - len(target.lstrip())]

    # Indent each line of approach_code to match
    code_lines = approach_code.splitlines()
    indented = [indent + cl if cl.strip() else cl for cl in code_lines]

    # Build modified source by replacing the hole line
    modified_lines = (
        lines[: hole_line - 1]
        + [l + "\n" for l in indented]
        + lines[hole_line:]
    )

    # Generate unified diff
    label = filename or "file"
    diff = difflib.unified_diff(
        lines,
        modified_lines,
        fromfile=f"a/{label}",
        tofile=f"b/{label}",
        n=3,  # context lines
    )
    return "".join(diff)


# ─── Terminal Display ─────────────────────────────────────────────────────────

def show_status_dashboard(holes: list[dict], server_status: dict) -> None:
    """Display a rich status dashboard showing all holes and their state.

    Each hole shows: index, line number, description (truncated), type,
    and generation status (ready/pending/error) with colored badges.

    Uses rich if available, falls back to plain text.
    """
    if not holes:
        print("  no holes found")
        return

    if HAS_RICH and console:
        # @mole:filled-by hand (trivial rich Table construction)
        table = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style=f"bold {COLORS['primary']}",
            border_style=COLORS['muted'],
        )
        table.add_column("#", justify="right", width=4)
        table.add_column("line", justify="right", width=6)
        table.add_column("description", min_width=30, max_width=50)
        table.add_column("type", style=COLORS['type'], max_width=20)
        table.add_column("status", justify="center", width=10)

        status_colors = {
            "ready": COLORS['secondary'],
            "pending": COLORS['warning'],
            "error": COLORS['error'],
        }
        for h in holes:
            desc = h.get("description", "")
            if len(desc) > 50:
                desc = desc[:47] + "..."
            st = h.get("status", "?")
            st_color = status_colors.get(st, COLORS['dim'])
            table.add_row(
                str(h.get("index", "?")),
                str(h.get("line", "?")),
                desc,
                h.get("type", "—"),
                f"[{st_color}]{st}[/]",
            )
        console.print(table)
    else:
        # Plain text fallback
        print(f"\n  {'#':>3}  {'line':>5}  {'status':>8}  description")
        print(f"  {'─'*3}  {'─'*5}  {'─'*8}  {'─'*40}")
        for h in holes:
            desc = h.get("description", "")[:45]
            print(f"  {h['index']:>3}  {h['line']:>5}  {h.get('status', '?'):>8}  {desc}")

    # Server pool status
    pool = server_status.get("pool", {})
    pending = pool.get("pending", 0)
    completed = pool.get("completed", 0)
    errored = pool.get("errored", 0)
    print(f"\n  pool: {completed} ready · {pending} pending · {errored} errored")


def show_approach(
    index: int,
    approach: dict,
    language: str = "python",
    show_code: bool = True,
) -> None:
    """Display one approach with name, description, and optional code preview.

    Uses rich panels with syntax highlighting if available.
    """
    name = approach.get("name", f"approach {index}")
    desc = approach.get("description", "")
    code = approach.get("code", "")
    sub_holes = approach.get("sub_holes", 0)

    if HAS_RICH and console:
        header = Text()
        header.append(f"  {index}. ", style=f"bold {COLORS['primary']}")
        header.append(name, style=f"bold {COLORS['secondary']}")
        if sub_holes:
            header.append(f"  ({sub_holes} sub-holes)", style=COLORS['dim'])
        console.print(header)
        if desc:
            console.print(f"     {desc}", style=COLORS['desc'])
        if show_code and code:
            syntax = Syntax(
                code, language,
                theme="monokai",
                line_numbers=True,
                padding=(0, 2),
            )
            console.print(Panel(syntax, border_style=COLORS['muted'], expand=False))
    else:
        print(f"\n  {index}. {name}")
        if desc:
            print(f"     {desc}")
        if show_code and code:
            for line in code.split("\n"):
                print(f"     {line}")
        if sub_holes:
            print(f"     ({sub_holes} sub-holes)")


def show_diff(diff_text: str) -> None:
    """Display a unified diff with syntax highlighting.

    + lines in green, - lines in red, @@ headers in cyan.
    """
    if HAS_RICH and console:
        lines = diff_text.split("\n")
        text = Text()
        for line in lines:
            if line.startswith("+++") or line.startswith("---"):
                text.append(line + "\n", style="bold")
            elif line.startswith("+"):
                text.append(line + "\n", style="green")
            elif line.startswith("-"):
                text.append(line + "\n", style="red")
            elif line.startswith("@@"):
                text.append(line + "\n", style="cyan")
            else:
                text.append(line + "\n")
        console.print(Panel(text, title="diff preview", border_style=COLORS['muted']))
    else:
        print(diff_text)


# ─── Interactive Pick Flow ────────────────────────────────────────────────────

def pick_interactive(
    client: ServeClient,
    path: Path,
    poll_interval: float = 2.0,
    auto_apply: bool = False,
) -> None:
    """Main interactive pick flow.

    Loop:
      1. Show status dashboard
      2. User selects a hole (or 'q' to quit, 'r' to refresh)
      3. Fetch diversify results for that hole
      4. Show approaches with code previews
      5. User picks an approach (or 'd' for diff preview)
      6. Apply the pick (write to file or just confirm)

    This is the top-level UX for the pick command.
    """
    # @mole:filled-by claude-diversify approach 2 (two nested loops) + manual cleanup
    try:
        while True:
            # ── Outer loop: show dashboard, select a hole ──────────────
            try:
                holes = client.get_holes()
                server_status = client.status()
            except ConnectionError as e:
                print(f"\n  connection lost: {e}", file=sys.stderr)
                return

            show_status_dashboard(holes, server_status)

            try:
                raw = input("\n  hole number (or 'r' refresh, 'q' quit): ").strip().lower()
            except EOFError:
                return

            if raw == "q":
                return
            if raw == "r":
                continue

            try:
                hole_idx = int(raw)
            except ValueError:
                print(f"  invalid input: {raw!r} — enter a number, 'r', or 'q'")
                continue

            if not (1 <= hole_idx <= len(holes)):
                print(f"  hole {hole_idx} out of range (1–{len(holes)})")
                continue

            # ── Fetch diversify results for selected hole ──────────────
            try:
                diversify_data = client.get_diversify(hole_idx)
            except ConnectionError as e:
                print(f"\n  connection lost: {e}", file=sys.stderr)
                return

            approaches: list[dict] = diversify_data.get("approaches", [])
            language: str = diversify_data.get("language", "python")
            hole_line: int = diversify_data.get("line", 0)

            if not approaches:
                status_str = diversify_data.get("status", "unknown")
                print(f"  hole {hole_idx}: no approaches yet (status: {status_str})")
                continue

            # Show all approaches
            print()
            for i, approach in enumerate(approaches, start=1):
                show_approach(i, approach, language=language, show_code=True)

            # ── Inner loop: select an approach ─────────────────────────
            while True:
                n_approaches = len(approaches)
                try:
                    raw2 = input(
                        f"\n  pick 1–{n_approaches}"
                        f" (or 'd<N>' diff, 'b' back, 'q' quit): "
                    ).strip().lower()
                except EOFError:
                    return

                if raw2 == "q":
                    return
                if raw2 == "b":
                    break  # back to outer loop

                # 'd' or 'd<N>' → diff preview
                if raw2.startswith("d") and len(raw2) > 1:
                    try:
                        diff_idx = int(raw2[1:].strip())
                    except ValueError:
                        print(f"  invalid: {raw2!r} — use 'd1', 'd2', etc.")
                        continue

                    if not (1 <= diff_idx <= n_approaches):
                        print(f"  approach {diff_idx} out of range")
                        continue

                    approach_code = approaches[diff_idx - 1].get("code", "")
                    try:
                        original_source = path.read_text()
                    except OSError as e:
                        print(f"  cannot read {path}: {e}")
                        continue

                    diff_text = generate_diff(
                        original_source, hole_line, approach_code,
                        filename=path.name,
                    )
                    show_diff(diff_text)
                    continue  # stay in inner loop

                # Numeric approach selection
                try:
                    approach_idx = int(raw2)
                except ValueError:
                    print(f"  invalid: {raw2!r} — number, 'd<N>', 'b', or 'q'")
                    continue

                if not (1 <= approach_idx <= n_approaches):
                    print(f"  approach {approach_idx} out of range (1–{n_approaches})")
                    continue

                # Apply the pick
                try:
                    result = client.pick(hole_idx, approach_idx)
                except ConnectionError as e:
                    print(f"\n  connection lost: {e}", file=sys.stderr)
                    return

                chosen_name = approaches[approach_idx - 1].get("name", f"approach {approach_idx}")

                if auto_apply:
                    try:
                        current_source = path.read_text()
                    except OSError as e:
                        print(f"  cannot read {path}: {e}", file=sys.stderr)
                        break

                    picked_code = approaches[approach_idx - 1].get("code", "")
                    # Re-discover to get proper Hole object for apply
                    dfile = discover(path)
                    matching = [h for h in dfile.holes if h.line_no == hole_line]
                    if matching:
                        new_source = apply_fill(
                            matching[0], picked_code, current_source, path,
                            mode="expand",
                        )
                        path.write_text(new_source)
                        print(f"  applied '{chosen_name}' to {path.name}")
                    else:
                        print(f"  warning: hole at L{hole_line} not found — pick recorded but not applied")
                else:
                    print(f"  picked '{chosen_name}' — {result.get('message', 'done')}")

                break  # back to outer loop

    except KeyboardInterrupt:
        print("\n  interrupted")
    except ConnectionError as e:
        print(f"\n  connection lost: {e}", file=sys.stderr)


# ─── CLI Entry Point ──────────────────────────────────────────────────────────

def pick_command(
    path: Path,
    host: str = "127.0.0.1",
    port: int = 3077,
    poll: bool = False,
) -> None:
    """Entry point for `mole pick file.py`.

    Connects to serve API, enters interactive pick mode.
    If --poll, waits for serve to become available first.
    """
    client = ServeClient(host=host, port=port)

    if poll:
        print(f"  waiting for mole serve on {host}:{port}...")
        while not client.is_alive():
            time.sleep(1.0)
        print("  connected!")

    if not client.is_alive():
        print(
            f"  error: cannot connect to mole serve at {host}:{port}\n"
            f"  start it with: mole serve {path}",
            file=sys.stderr,
        )
        sys.exit(1)

    pick_interactive(client, path)
