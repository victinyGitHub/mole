"""mole — Filler implementations.

Fillers are dumb str→str pipes. They receive an assembled prompt and return
raw LLM output. ALL configuration is exposed via FillerConfig.

Available fillers:
  ClaudeCLIFiller  — calls Claude CLI subprocess (default, parallel-capable)
  ManualFiller     — interactive terminal input (fallback)
"""
from __future__ import annotations

import json
import os
import re
import select
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Generator, Optional

from .types import FillerConfig, Filler


# ─── Concurrency Control ────────────────────────────────────────────────────

MAX_CONCURRENT_PROCS = 6
_proc_semaphore = threading.Semaphore(MAX_CONCURRENT_PROCS)


def set_concurrency(n: int) -> None:
    """Set max concurrent Claude subprocess limit. Call before fills."""
    global MAX_CONCURRENT_PROCS, _proc_semaphore
    MAX_CONCURRENT_PROCS = max(1, n)
    _proc_semaphore = threading.Semaphore(MAX_CONCURRENT_PROCS)


# ─── Output Cleaning ────────────────────────────────────────────────────────

def _strip_fences(raw: str) -> str:
    """Strip markdown code fences from LLM output.

    Handles: triple backtick blocks, inline single backtick wrapping.
    """
    raw = raw.strip()
    # Triple backtick code blocks
    m = re.search(r'```(?:python|typescript|ts|js|tsx)?\s*\n(.+?)\n```', raw, re.DOTALL)
    if m:
        return m.group(1).strip()
    raw = re.sub(r'^```\w*\n?', '', raw)
    raw = re.sub(r'\n?```$', '', raw)
    # Inline single backtick wrapping
    if raw.startswith('`') and raw.endswith('`') and raw.count('`') == 2:
        raw = raw[1:-1]
    return raw.strip()


# ─── Claude Binary Discovery ────────────────────────────────────────────────

def _find_claude_bin() -> str:
    """Find the claude CLI binary. Checks CLAUDE_BIN env, then common paths."""
    env_bin = os.environ.get("CLAUDE_BIN")
    if env_bin:
        return env_bin
    for path in ["/usr/local/bin/claude", os.path.expanduser("~/.local/bin/claude")]:
        if os.path.isfile(path):
            return path
    # Fall back to PATH lookup
    return "claude"


# ─── Claude CLI Filler ───────────────────────────────────────────────────────

class ClaudeCLIFiller:
    """Calls Claude CLI via subprocess with a clean environment.

    Requires: Claude CLI installed (https://docs.anthropic.com/claude-code)

    Key design:
    - Clean env prevents CLAUDECODE nested session detection
    - stdin=DEVNULL prevents hanging
    - Semaphore caps concurrent processes to prevent OOM
    - Supports parallel batch fills via ThreadPoolExecutor
    """

    def __init__(self, config: Optional[FillerConfig] = None):
        self.config = config or FillerConfig()
        self.CLAUDE_BIN = _find_claude_bin()
        # Minimal env to avoid inheriting parent's CLAUDECODE var
        self.CLEAN_ENV = {
            "HOME": os.path.expanduser("~"),
            "USER": os.environ.get("USER", ""),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        }
        self._supports_no_session = self._check_flag_support()

    def _check_flag_support(self) -> bool:
        """Check if claude CLI supports --no-session-persistence."""
        try:
            proc = subprocess.run(
                [self.CLAUDE_BIN, "--help"],
                env=self.CLEAN_ENV,
                capture_output=True, text=True,
                stdin=subprocess.DEVNULL,
                timeout=10,
            )
            return "--no-session-persistence" in proc.stdout
        except Exception:
            return False

    def fill(self, prompt: str, on_chunk: Optional[callable] = None) -> str:
        """Send prompt to Claude CLI, return cleaned code output.

        If on_chunk is provided and config.streaming is True, streams the
        response and calls on_chunk(delta_text) for each new text chunk.
        Still returns the complete cleaned output.
        """
        if on_chunk and self.config.streaming:
            gen = self.stream_fill(prompt)
            try:
                while True:
                    chunk = next(gen)
                    on_chunk(chunk)
            except StopIteration as e:
                return e.value or ""
        raw = self._call_claude(prompt)
        return _strip_fences(raw)

    def stream_fill(self, prompt: str) -> Generator[str, None, str]:
        """Stream fill — yields text chunks as they arrive, returns final result.

        Uses Claude CLI's --output-format stream-json --verbose mode.
        Each JSONL line is parsed; text content blocks are yielded incrementally.
        The generator's return value is the complete cleaned output.

        Usage:
            gen = filler.stream_fill(prompt)
            try:
                while True:
                    chunk = next(gen)
                    display(chunk)  # show incrementally
            except StopIteration as e:
                final_code = e.value  # complete result
        """
        cmd = [
            self.CLAUDE_BIN, "-p", prompt,
            "--model", self.config.model,
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        if self._supports_no_session:
            cmd.append("--no-session-persistence")
        if self.config.effort != "default":
            cmd.extend(["--effort", self.config.effort])

        _proc_semaphore.acquire()
        full_text = ""
        final_result = ""
        try:
            proc = subprocess.Popen(
                cmd,
                env=self.CLEAN_ENV,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )
            start = time.monotonic()
            try:
                while True:
                    # Check timeout
                    if time.monotonic() - start > self.config.timeout:
                        proc.kill()
                        proc.communicate()
                        raise TimeoutError(f"Claude CLI timed out after {self.config.timeout}s")

                    # Non-blocking read with select
                    ready, _, _ = select.select([proc.stdout], [], [], 0.1)
                    if not ready:
                        if proc.poll() is not None:
                            # Process ended, drain remaining
                            for line in proc.stdout:
                                parsed = self._parse_stream_line(line.decode("utf-8", errors="replace"))
                                if parsed is not None:
                                    text, is_delta = parsed
                                    if is_delta:
                                        full_text += text
                                        yield text
                                    else:
                                        final_result = text
                            break
                        continue

                    line = proc.stdout.readline()
                    if not line:
                        break

                    decoded = line.decode("utf-8", errors="replace").strip()
                    if not decoded:
                        continue

                    parsed = self._parse_stream_line(decoded)
                    if parsed is not None:
                        text, is_delta = parsed
                        if is_delta:
                            full_text += text
                            yield text
                        else:
                            # Complete text block or result — use as final
                            final_result = text

                # Check exit code
                proc.wait(timeout=5)
                if proc.returncode != 0 and not full_text and not final_result:
                    stderr = proc.stderr.read().decode("utf-8", errors="replace")[:300]
                    raise RuntimeError(f"Claude CLI failed (exit {proc.returncode}): {stderr}")

            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise TimeoutError(f"Claude CLI timed out after {self.config.timeout}s")
        finally:
            _proc_semaphore.release()

        # Prefer the final result event (has clean text), fallback to accumulated deltas
        output = final_result if final_result else full_text
        return _strip_fences(output) if output else ""

    @staticmethod
    def _parse_stream_line(line: str) -> Optional[tuple[str, bool]]:
        """Parse a stream-json JSONL line, extract text content if present.

        Returns (text, is_delta) or None:
          - ("token text", True)  — incremental delta (append to accumulator)
          - ("full text", False)  — complete result (replace accumulator)
          - None                  — no text in this event (skip)
        """
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None

        etype = event.get("type")

        # Token-level streaming deltas (from --include-partial-messages)
        if etype == "stream_event":
            inner = event.get("event", {})
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        return (text, True)
            return None

        # Final result event — has the complete output
        if etype == "result" and "result" in event:
            return (event["result"], False)

        # Assistant message with text content (complete block)
        if etype == "assistant":
            msg = event.get("message", {})
            content = msg.get("content", [])
            for block in content:
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        return (text, False)

        return None

    def batch_fill(self, prompts: list[str]) -> list[str]:
        """Fill multiple prompts in parallel, capped by MAX_CONCURRENT_PROCS."""
        def _run(idx: int, prompt: str) -> tuple[int, str]:
            try:
                raw = self._call_claude(prompt)
                return (idx, _strip_fences(raw))
            except (TimeoutError, RuntimeError) as e:
                return (idx, f"# ERROR: {e}")

        workers = min(MAX_CONCURRENT_PROCS, len(prompts))
        results: dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run, i, p): i for i, p in enumerate(prompts)}
            for future in as_completed(futures):
                idx, result = future.result()
                results[idx] = result

        return [results.get(i, f"# ERROR: fill {i} missing") for i in range(len(prompts))]

    def _call_claude(self, prompt: str) -> str:
        """Call Claude CLI with clean env. Returns raw stdout."""
        cmd = [
            self.CLAUDE_BIN, "-p", prompt,
            "--model", self.config.model,
            "--output-format", "text",
        ]
        if self._supports_no_session:
            cmd.append("--no-session-persistence")
        if self.config.effort != "default":
            cmd.extend(["--effort", self.config.effort])

        _proc_semaphore.acquire()
        try:
            proc = subprocess.Popen(
                cmd,
                env=self.CLEAN_ENV,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )
            try:
                stdout, stderr = proc.communicate(timeout=self.config.timeout)
                if proc.returncode != 0:
                    err_text = stderr.decode("utf-8", errors="replace")[:300]
                    raise RuntimeError(f"Claude CLI failed (exit {proc.returncode}): {err_text}")
                result = stdout.decode("utf-8", errors="replace").strip()
                if not result:
                    raise RuntimeError("Claude CLI returned empty output")
                return result
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise TimeoutError(f"Claude CLI timed out after {self.config.timeout}s")
        finally:
            _proc_semaphore.release()


# ─── Manual Filler ───────────────────────────────────────────────────────────

class ManualFiller:
    """Interactive fallback — prompts user in terminal for code input."""

    def __init__(self, config: Optional[FillerConfig] = None):
        self.config = config or FillerConfig()

    def fill(self, prompt: str) -> str:
        """Print the prompt context and read code from stdin."""
        import sys

        if not sys.stdin.isatty():
            raise RuntimeError(
                "ManualFiller requires an interactive terminal. "
                "Use ClaudeCLIFiller for non-interactive contexts."
            )

        # Extract key info from prompt for display
        task_match = re.search(r'TASK:\s*(.+)', prompt)
        type_match = re.search(r'type:\s*(.+)', prompt, re.IGNORECASE)

        print("\n┌─ MANUAL FILL REQUIRED ─────────────────")
        if task_match:
            print(f"│ Task: {task_match.group(1).strip()}")
        if type_match:
            print(f"│ Type: {type_match.group(1).strip()}")
        print("│ Enter code (blank line to finish):")
        print("└────────────────────────────────────────\n")

        lines: list[str] = []
        while True:
            try:
                line = input("  ")
                if line == "":
                    break
                lines.append(line)
            except EOFError:
                break

        return "\n".join(lines)

    def batch_fill(self, prompts: list[str]) -> list[str]:
        """Fill prompts sequentially (manual = no parallelism)."""
        return [self.fill(p) for p in prompts]


# ─── Filler Registry ─────────────────────────────────────────────────────────

def get_filler(
    name: str,
    config: Optional[FillerConfig] = None,
) -> ClaudeCLIFiller | ManualFiller:
    """Create a filler by name.

    Available: claude (default), claude-opus, manual
    """
    if name == "claude":
        return ClaudeCLIFiller(config or FillerConfig(model="sonnet"))
    elif name == "claude-opus":
        return ClaudeCLIFiller(config or FillerConfig(model="opus"))
    elif name == "manual":
        return ManualFiller(config)
    else:
        available = "claude, claude-opus, manual"
        raise ValueError(f"Unknown filler: {name!r}. Available: {available}")


# Convenience names for the registry
FILLER_NAMES = ["claude", "claude-opus", "manual"]
