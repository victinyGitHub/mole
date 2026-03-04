"""mole — CLI interface.

Interactive REPL and batch modes for hole-driven development.

Usage:
    mole myfile.py                          # Interactive REPL
    mole --check myfile.py                  # Show holes
    mole --fill-all myfile.py               # Fill all unfilled holes
    mole --expand-all myfile.py             # Expand all unfilled holes
    mole --model opus myfile.py              # Use opus model
    mole --layers types,behavior f.py       # Selective context layers
"""
from __future__ import annotations

import argparse
import readline
import sys
import textwrap
from pathlib import Path
from typing import Optional

from .types import Hole, HoleStatus, MoleFile, FillerConfig
from .operations import (
    discover, expand, diversify, fill, verify, apply, resync,
    edit_hole, propagate, antiunify,
)
from .fillers import get_filler, FILLER_NAMES
from .context import (
    TypeContextLayer, SymbolContextLayer,
    BehaviorContextLayer, CodeContextLayer,
    DEFAULT_LAYERS, assemble_context,
)
from .backends import get_backend, detect_language
from .display import (
    show_holes, show_hole_detail, show_expansion, show_verify_result,
    print_welcome, get_prompt, print_code_panel, print_fill_result,
    print_applied, print_skipped, print_error, print_info,
    spinner, timer_spinner, stream_or_spin, console, HAS_RICH,
)


# ─── Layer Resolution ────────────────────────────────────────────────────────

LAYER_MAP = {
    "types": TypeContextLayer,
    "symbols": SymbolContextLayer,
    "behavior": BehaviorContextLayer,
    "code": CodeContextLayer,
}


def _resolve_layers(layer_str: Optional[str]) -> list:
    """Resolve comma-separated layer names to layer instances."""
    if not layer_str:
        return list(DEFAULT_LAYERS)

    layers = []
    for name in layer_str.split(","):
        name = name.strip().lower()
        if name in LAYER_MAP:
            layers.append(LAYER_MAP[name]())
        else:
            print_error(f"Unknown layer '{name}', skipping")

    return layers if layers else list(DEFAULT_LAYERS)


# ─── REPL ────────────────────────────────────────────────────────────────────

# Tab-completable commands
_REPL_COMMANDS = [
    "show", "expand", "diversify", "fill", "context",
    "verify", "apply", "edit", "propagate", "groups",
    "config", "undo", "reload", "quit", "exit",
]

# Valid config keys and their types/validators
_CONFIG_FIELDS = {
    "model": str,
    "effort": str,
    "temperature": float,
    "max_tokens": int,
    "timeout": int,
    "streaming": bool,
}
_VALID_MODELS = ["haiku", "sonnet", "opus"]
_VALID_EFFORTS = ["low", "medium", "high"]
_BOOL_MAP = {"true": True, "false": False, "on": True, "off": False, "1": True, "0": False}


def _mole_completer(text: str, state: int) -> Optional[str]:
    """Tab completer for REPL commands."""
    matches = [c for c in _REPL_COMMANDS if c.startswith(text.lower())]
    if state < len(matches):
        return matches[state]
    return None


def _setup_readline() -> None:
    """Configure readline for history + tab completion."""
    readline.set_completer(_mole_completer)
    readline.parse_and_bind("tab: complete")
    # Save/load history across sessions
    history_file = Path.home() / ".mole_history"
    try:
        readline.read_history_file(str(history_file))
    except FileNotFoundError:
        pass
    import atexit
    atexit.register(readline.write_history_file, str(history_file))


def _repl(dfile: MoleFile, filler_name: str, layers: list, config: FillerConfig) -> None:
    """Interactive REPL for hole-driven development."""
    _setup_readline()
    filler_obj = get_filler(filler_name, config)
    backend = get_backend(dfile.language)
    undo_stack: list[str] = [dfile.source]  # Source history for undo
    lang = dfile.language  # For syntax highlighting

    print_welcome(dfile)

    prompt_str = get_prompt()

    while True:
        try:
            raw = input(prompt_str).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n")
            print_info("Bye!")
            break

        if not raw:
            continue

        parts = raw.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("q", "quit", "exit"):
            break

        elif cmd == "show":
            if arg:
                hole = _get_hole(dfile, arg)
                if hole:
                    show_hole_detail(hole, dfile.source, backend)
            else:
                show_holes(dfile)

        elif cmd == "expand":
            hole = _get_hole(dfile, arg)
            if hole:
                if config.streaming and hasattr(filler_obj, 'stream_fill'):
                    from .display import StreamingCodePanel
                    panel = StreamingCodePanel(lang=lang, title=f"Expanding L{hole.line_no}...")
                    panel.start()
                    try:
                        exp = expand(hole, dfile.source, dfile.path, filler_obj, layers, backend,
                                     on_chunk=lambda c: panel.update(c))
                        panel.finish()
                    except Exception:
                        panel.finish()
                        raise
                else:
                    with spinner(f"Expanding L{hole.line_no}..."):
                        exp = expand(hole, dfile.source, dfile.path, filler_obj, layers, backend)
                show_expansion(exp, lang=lang)
                # Ask whether to apply the expansion
                try:
                    confirm = input("\n  Apply this expansion? (y/n): ").strip().lower()
                    if confirm in ('y', 'yes'):
                        undo_stack.append(dfile.source)
                        dfile.source = apply(hole, exp.expanded_code, dfile.source, dfile.path, backend, mode="expand")
                        dfile.path.write_text(dfile.source)
                        dfile = discover(dfile.path, backend)
                        print_applied(f"Applied → {len(dfile.unfilled)} sub-hole(s) now available")
                        show_holes(dfile)
                    else:
                        print_skipped("Expansion not applied")
                except (EOFError, KeyboardInterrupt):
                    print_skipped("Skipped")

        elif cmd == "diversify":
            hole = _get_hole(dfile, arg)
            if hole:
                if config.streaming and hasattr(filler_obj, 'stream_fill'):
                    from .display import DiversifyStreamingDisplay
                    display = DiversifyStreamingDisplay(n=3, lang=lang)
                    display.start()
                    try:
                        exps = diversify(
                            hole, dfile.source, dfile.path, filler_obj, layers, backend,
                            on_chunk=lambda idx, c: display.update(idx, c),
                            on_title=lambda idx, name: display.set_title(idx, name),
                        )
                        display.finish()
                    except Exception:
                        display.finish()
                        raise
                    # Side-by-side is showing — user can pick directly or expand vertically
                    try:
                        pick = input("\n  Pick (1-3), 'c' for detail view, 'n' to skip: ").strip()
                        if pick.lower() == 'n':
                            print_skipped("No approach applied")
                        elif pick.lower() == 'c':
                            # Show full vertical detail for each approach
                            for i, exp in enumerate(exps, 1):
                                show_expansion(exp, i, lang=lang)
                            # Then pick
                            pick2 = input("\n  Pick approach (1-3), or 'n' to skip: ").strip()
                            if pick2.lower() == 'n':
                                print_skipped("No approach applied")
                            else:
                                idx = int(pick2) - 1
                                if 0 <= idx < len(exps):
                                    chosen = exps[idx]
                                    undo_stack.append(dfile.source)
                                    dfile.source = apply(hole, chosen.expanded_code, dfile.source, dfile.path, backend, mode="expand")
                                    dfile.path.write_text(dfile.source)
                                    dfile = discover(dfile.path, backend)
                                    print_applied(f"Selected: {chosen.approach_name} → {len(dfile.unfilled)} sub-hole(s)")
                                    show_holes(dfile)
                                else:
                                    print_error(f"Invalid choice: {pick2}")
                        else:
                            idx = int(pick) - 1
                            if 0 <= idx < len(exps):
                                chosen = exps[idx]
                                undo_stack.append(dfile.source)
                                dfile.source = apply(hole, chosen.expanded_code, dfile.source, dfile.path, backend, mode="expand")
                                dfile.path.write_text(dfile.source)
                                dfile = discover(dfile.path, backend)
                                print_applied(f"Selected: {chosen.approach_name} → {len(dfile.unfilled)} sub-hole(s)")
                                show_holes(dfile)
                            else:
                                print_error(f"Invalid choice: {pick}")
                    except (ValueError, EOFError, KeyboardInterrupt):
                        print_skipped("No selection made")
                else:
                    with timer_spinner(f"Generating 3 approaches for L{hole.line_no}") as t:
                        exps = diversify(hole, dfile.source, dfile.path, filler_obj, layers, backend)
                    print_info(f"Done in {t.elapsed:.1f}s")
                    for i, exp in enumerate(exps, 1):
                        show_expansion(exp, i, lang=lang)
                    # Pick from vertical view
                    try:
                        pick = input("\n  Pick approach (1-3), or 'n' to skip: ").strip()
                        if pick.lower() == 'n':
                            print_skipped("No approach applied")
                        else:
                            idx = int(pick) - 1
                            if 0 <= idx < len(exps):
                                chosen = exps[idx]
                                undo_stack.append(dfile.source)
                                dfile.source = apply(hole, chosen.expanded_code, dfile.source, dfile.path, backend, mode="expand")
                                dfile.path.write_text(dfile.source)
                                dfile = discover(dfile.path, backend)
                                print_applied(f"Selected: {chosen.approach_name} → {len(dfile.unfilled)} sub-hole(s)")
                                show_holes(dfile)
                            else:
                                print_error(f"Invalid choice: {pick}")
                    except (ValueError, EOFError):
                        print_skipped("No selection made")

        elif cmd == "fill":
            hole = _get_hole(dfile, arg)
            if hole:
                if config.streaming and hasattr(filler_obj, 'stream_fill'):
                    from .display import StreamingCodePanel
                    panel = StreamingCodePanel(lang=lang, title=f"Filling L{hole.line_no}...")
                    panel.start()
                    try:
                        code, result = fill(hole, dfile.source, dfile.path, filler_obj, layers, backend,
                                            on_chunk=lambda c: panel.update(c))
                        panel.finish()
                    except Exception:
                        panel.finish()
                        raise
                else:
                    with spinner(f"Filling L{hole.line_no}..."):
                        code, result = fill(hole, dfile.source, dfile.path, filler_obj, layers, backend)
                print_fill_result(code, lang=lang)
                show_verify_result(result)
                # Auto-apply if verification passed
                if result.success and hole.fill_code:
                    try:
                        confirm = input("\n  Apply this fill? (y/n): ").strip().lower()
                        if confirm in ('y', 'yes'):
                            undo_stack.append(dfile.source)
                            dfile.source = apply(hole, hole.fill_code, dfile.source, dfile.path, backend)
                            dfile.path.write_text(dfile.source)
                            dfile = discover(dfile.path, backend)
                            print_applied(f"Applied → {len(dfile.unfilled)} hole(s) remaining")
                            show_holes(dfile)
                        else:
                            print_skipped("Fill not applied")
                    except (EOFError, KeyboardInterrupt):
                        print_skipped("Skipped")

        elif cmd == "context":
            hole = _get_hole(dfile, arg)
            if hole:
                ctx = assemble_context(hole, dfile.source, dfile.path, layers, backend)
                print_code_panel(ctx, title="Context", lang="text")

        elif cmd == "verify":
            hole = _get_hole(dfile, arg)
            if hole and hole.fill_code:
                with spinner("Verifying..."):
                    result = verify(hole, hole.fill_code, dfile.source, dfile.path, backend)
                show_verify_result(result)
            elif hole:
                print_info("Hole not yet filled — nothing to verify")

        elif cmd == "apply":
            # Apply all filled holes
            applied = 0
            for h in dfile.holes:
                if h.fill_code and h.status == HoleStatus.FILLED:
                    undo_stack.append(dfile.source)
                    dfile.source = apply(h, h.fill_code, dfile.source, dfile.path, backend)
                    applied += 1
            if applied > 0:
                # Write back to file
                dfile.path.write_text(dfile.source)
                print_applied(f"Applied {applied} fill(s) → {dfile.path.name}")
                # Re-discover holes
                dfile = discover(dfile.path, backend)
                show_holes(dfile)
            else:
                print_info("No filled holes to apply")

        elif cmd == "undo":
            if len(undo_stack) > 1:
                dfile.source = undo_stack.pop()
                dfile.path.write_text(dfile.source)
                dfile = discover(dfile.path, backend)
                print_applied("Undone. Reloaded.")
                show_holes(dfile)
            else:
                print_info("Nothing to undo")

        elif cmd == "edit":
            # edit <line> <description> — mark existing code for AI replacement
            edit_parts = arg.split(None, 1)
            if len(edit_parts) < 2:
                print_info("Usage: edit <line-number> <description>")
            else:
                try:
                    edit_line = int(edit_parts[0])
                    edit_desc = edit_parts[1]
                    undo_stack.append(dfile.source)
                    dfile.source = edit_hole(dfile.source, edit_line, edit_desc, dfile.path, backend)
                    dfile.path.write_text(dfile.source)
                    dfile = discover(dfile.path, backend)
                    print_applied(f"Marked L{edit_line} as hole → ready for fill")
                    show_holes(dfile)
                except ValueError as e:
                    print_error(str(e))

        elif cmd == "propagate":
            # Run type checker, auto-generate holes at error sites
            with spinner("Running type checker..."):
                new_holes = propagate(dfile.path, backend)
            if new_holes:
                dfile = discover(dfile.path, backend)
                print_applied(f"Created {len(new_holes)} hole(s) from type errors")
                show_holes(dfile)
            else:
                print_info("No type errors found — nothing to propagate")

        elif cmd == "groups":
            # Show anti-unified hole groups (structurally similar holes)
            groups = antiunify(dfile.path, backend)
            if groups:
                print_info(f"Found {len(groups)} type group(s):")
                for i, g in enumerate(groups, 1):
                    if HAS_RICH and console:
                        from .display import COLORS
                        from rich.text import Text
                        label = Text()
                        label.append(f"  [{i}] ", style=COLORS["muted"])
                        label.append(g.pattern, style=COLORS["type"])
                        label.append(f" × {g.size}", style=COLORS["secondary"])
                        if g.type_vars:
                            vars_str = ", ".join(f"{k}={v}" for k, vlist in g.type_vars.items() for v in vlist)
                            label.append(f"  ({vars_str})", style=COLORS["dim"])
                        console.print(label)
                    else:
                        print(f"  [{i}] {g.label}")
                    for h in g.holes:
                        name_part = f"  {h.var_name}" if h.var_name else ""
                        print(f"       L{h.line_no}{name_part}: {h.description[:50]}")
            else:
                print_info("No type groups found (need 2+ holes with compatible types)")

        elif cmd == "config":
            if not arg:
                # Show current config
                if HAS_RICH and console:
                    from .display import COLORS
                    from rich.table import Table
                    table = Table(show_header=False, box=None, padding=(0, 2))
                    table.add_column(style=COLORS["dim"])
                    table.add_column(style=COLORS["primary"])
                    for field in _CONFIG_FIELDS:
                        val = getattr(config, field)
                        table.add_row(field, str(val))
                    console.print("  Current config:")
                    console.print(table)
                else:
                    print("  Current config:")
                    for field in _CONFIG_FIELDS:
                        print(f"    {field}: {getattr(config, field)}")
            else:
                cfg_parts = arg.split(None, 1)
                if len(cfg_parts) < 2:
                    print_info("Usage: config <key> <value>  |  config (show all)")
                else:
                    key, val_str = cfg_parts[0].lower(), cfg_parts[1]
                    if key not in _CONFIG_FIELDS:
                        print_error(f"Unknown config key: {key}. Valid: {', '.join(_CONFIG_FIELDS)}")
                    elif key == "model" and val_str not in _VALID_MODELS:
                        print_error(f"Unknown model: {val_str}. Valid: {', '.join(_VALID_MODELS)}")
                    elif key == "effort" and val_str not in _VALID_EFFORTS:
                        print_error(f"Unknown effort: {val_str}. Valid: {', '.join(_VALID_EFFORTS)}")
                    elif key == "streaming" and val_str.lower() not in _BOOL_MAP:
                        print_error(f"Invalid boolean: {val_str}. Use: true/false, on/off, 1/0")
                    else:
                        try:
                            if _CONFIG_FIELDS[key] is bool:
                                cast_val = _BOOL_MAP[val_str.lower()]
                            else:
                                cast_val = _CONFIG_FIELDS[key](val_str)
                            setattr(config, key, cast_val)
                            # Recreate filler with updated config
                            filler_obj = get_filler(filler_name, config)
                            print_applied(f"{key} → {cast_val}")
                        except ValueError:
                            print_error(f"Invalid value for {key}: {val_str}")

        elif cmd == "reload":
            dfile = discover(dfile.path, backend)
            show_holes(dfile)

        else:
            print_error(f"Unknown command: {cmd}")
            if HAS_RICH and console:
                from .display import COLORS
                from rich.text import Text
                cmds = Text()
                cmds.append("  Commands: ", style=COLORS["dim"])
                for i, c in enumerate(_REPL_COMMANDS[:-1]):
                    cmds.append(c, style=COLORS["primary"])
                    cmds.append(" · ", style=COLORS["muted"])
                cmds.append(_REPL_COMMANDS[-1], style=COLORS["primary"])
                console.print(cmds)
            else:
                print("  Commands: show, expand, diversify, fill, context, verify, apply, undo, reload, quit")


def _get_hole(dfile: MoleFile, arg: str) -> Optional[Hole]:
    """Resolve hole from index or line number."""
    if not arg:
        print_info("Usage: <command> <hole-number>")
        return None
    try:
        idx = int(arg)
    except ValueError:
        print_error(f"Invalid hole number: {arg}")
        return None

    # Try as 1-indexed hole number first
    if 1 <= idx <= len(dfile.holes):
        return dfile.holes[idx - 1]

    # Try as line number
    for h in dfile.holes:
        if h.line_no == idx:
            return h

    print_error(f"Hole {idx} not found (have {len(dfile.holes)} holes)")
    return None


# ─── Batch Modes ─────────────────────────────────────────────────────────────

def _batch_check(path: Path) -> None:
    """Show all holes in a file (no LLM calls)."""
    dfile = discover(path)
    show_holes(dfile)


def _batch_fill_all(path: Path, filler_name: str, layers: list, config: FillerConfig) -> None:
    """Fill all unfilled holes in a file.

    Processes holes BOTTOM-UP to avoid line-shift corruption when
    multi-statement fills insert extra lines.

    Import hoisting is DEFERRED to the end. The apply() function normally
    hoists #import: lines to the top of the file, but this shifts ALL
    line numbers — breaking subsequent fills that still reference original
    positions. Instead we collect imports and hoist once after all fills.
    """
    from .operations import _extract_fill_imports, _find_import_insert_point

    dfile = discover(path)
    filler_obj = get_filler(filler_name, config)
    backend = get_backend(dfile.language)

    unfilled = dfile.unfilled
    if not unfilled:
        print_info(f"No unfilled holes in {path.name}")
        return

    # Sort bottom-up: highest line number first
    unfilled_sorted = sorted(unfilled, key=lambda h: -h.line_no)

    print_info(f"Filling {len(unfilled_sorted)} hole(s) in {path.name}...")

    applied_count = 0
    failed_count = 0
    all_imports: list[str] = []  # Collect all imports for deferred hoisting

    for i, h in enumerate(unfilled_sorted, 1):
        with spinner(f"[{i}/{len(unfilled_sorted)}] L{h.line_no}: {h.description}"):
            # Pass accumulated imports so verify includes them (prevents false
            # "not defined" errors from other fills' deferred imports)
            code, result = fill(
                h, dfile.source, path, filler_obj, layers, backend,
                extra_imports=all_imports if all_imports else None,
            )

        show_verify_result(result)

        if h.fill_code and result.success:
            # Extract imports BEFORE apply (so apply doesn't hoist them)
            clean_code, new_imports = _extract_fill_imports(h.fill_code)
            all_imports.extend(new_imports)
            h.fill_code = clean_code  # Store clean code (no #import: lines)

            dfile.source = apply(h, h.fill_code, dfile.source, path, backend)
            applied_count += 1
            print_applied(f"Fill applied for L{h.line_no}")
        elif h.fill_code:
            failed_count += 1
            print_error(f"Skipped L{h.line_no} (type check failed)")

    # Deferred import hoisting — hoist all collected imports at once
    if all_imports:
        lines = dfile.source.splitlines()
        insert_idx = _find_import_insert_point(lines, dfile.language)
        # Deduplicate imports and skip already-present ones
        existing_imports = set(l.strip() for l in lines if l.strip().startswith(("import ", "from ")))
        unique_imports = []
        for imp in all_imports:
            if imp.strip() not in existing_imports and imp not in unique_imports:
                unique_imports.append(imp)
        for imp in reversed(unique_imports):
            lines.insert(insert_idx, imp)
        dfile.source = "\n".join(lines)

    # Write back
    path.write_text(dfile.source)
    print_applied(f"Done. {applied_count} applied, {failed_count} skipped → {path.name}")


def _batch_expand_all(path: Path, filler_name: str, layers: list, config: FillerConfig) -> None:
    """Expand all unfilled holes in a file.

    Processes holes BOTTOM-UP to avoid line-shift corruption.
    """
    dfile = discover(path)
    filler_obj = get_filler(filler_name, config)
    backend = get_backend(dfile.language)

    unfilled = dfile.unfilled
    if not unfilled:
        print_info(f"No unfilled holes in {path.name}")
        return

    # Sort bottom-up: highest line number first
    unfilled_sorted = sorted(unfilled, key=lambda h: -h.line_no)

    print_info(f"Expanding {len(unfilled_sorted)} hole(s) in {path.name}...")

    for i, h in enumerate(unfilled_sorted, 1):
        with spinner(f"[{i}/{len(unfilled_sorted)}] L{h.line_no}: {h.description}"):
            exp = expand(h, dfile.source, path, filler_obj, layers, backend)
        show_expansion(exp)

        # Apply expansion to source
        dfile.source = apply(h, exp.expanded_code, dfile.source, path, backend, mode="expand")

    # Write back
    path.write_text(dfile.source)
    print_applied(f"Done. Written to {path.name}")


# ─── Batch: Propagate ─────────────────────────────────────────────────────────

def _batch_propagate(path: Path) -> None:
    """Run type checker and auto-generate holes from errors."""
    with spinner("Running type checker..."):
        new_holes = propagate(path)
    if new_holes:
        print_applied(f"Created {len(new_holes)} hole(s) from type errors")
        dfile = discover(path)
        show_holes(dfile)
    else:
        print_info("No type errors found — nothing to propagate")


# ─── Batch: Groups ────────────────────────────────────────────────────────────

def _batch_groups(path: Path) -> None:
    """Show anti-unified hole groups."""
    groups = antiunify(path)
    if groups:
        print_info(f"Found {len(groups)} type group(s):")
        for i, g in enumerate(groups, 1):
            if HAS_RICH and console:
                from .display import COLORS
                from rich.text import Text
                label = Text()
                label.append(f"  [{i}] ", style=COLORS["muted"])
                label.append(g.pattern, style=COLORS["type"])
                label.append(f" × {g.size}", style=COLORS["secondary"])
                console.print(label)
            else:
                print(f"  [{i}] {g.label}")
            for h in g.holes:
                name_part = f"  {h.var_name}" if h.var_name else ""
                print(f"       L{h.line_no}{name_part}: {h.description[:50]}")
    else:
        print_info("No type groups found (need 2+ holes with compatible types)")


# ─── CLI Entry Point ─────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> None:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="mole",
        description="Human-centred assisted programming with typed holes",
    )
    parser.add_argument("file", type=Path, help="Source file to work on")
    parser.add_argument("--check", action="store_true", help="Show holes (no LLM calls)")
    parser.add_argument("--fill-all", action="store_true", help="Fill all unfilled holes")
    parser.add_argument("--expand-all", action="store_true", help="Expand all unfilled holes")
    parser.add_argument("--propagate", action="store_true", help="Generate holes from type errors")
    parser.add_argument("--groups", action="store_true", help="Show anti-unified hole groups")
    parser.add_argument(
        "--filler", default="claude", choices=FILLER_NAMES,
        help="Filler to use (default: claude)",
    )
    parser.add_argument("--layers", default=None, help="Comma-separated context layers")
    parser.add_argument("--language", default=None, help="Override language detection")
    parser.add_argument("--model", default=None, help="Override model for filler")
    parser.add_argument("--effort", default="medium", help="Effort level for Claude CLI")

    args = parser.parse_args(argv)

    if not args.file.exists():
        print_error(f"File not found: {args.file}")
        sys.exit(1)

    # Build filler config
    config = FillerConfig(effort=args.effort)
    if args.model:
        config.model = args.model

    # Resolve layers
    layers = _resolve_layers(args.layers)

    # Dispatch to mode
    if args.check:
        _batch_check(args.file)
    elif args.fill_all:
        _batch_fill_all(args.file, args.filler, layers, config)
    elif args.expand_all:
        _batch_expand_all(args.file, args.filler, layers, config)
    elif args.propagate:
        _batch_propagate(args.file)
    elif args.groups:
        _batch_groups(args.file)
    else:
        # Interactive REPL
        dfile = discover(args.file)
        _repl(dfile, args.filler, layers, config)


if __name__ == "__main__":
    main()
