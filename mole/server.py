"""mole — Background serve mode with file watcher and HTTP API.

Watches a source file for changes, auto-discovers holes, and continuously
generates diversify results in the background. Users don't wait — results
are ready when they ask.

Architecture:
  mole serve file.py
    ├── FileWatcher     — detects saves, re-discovers holes
    ├── WorkerPool      — background diversify generation (ThreadPoolExecutor)
    ├── CacheManager    — stores results (from cache.py)
    └── HTTP API        — localhost:3077 for editor integration
         GET  /holes              — list all holes + status
         GET  /holes/<n>/diversify — get diversify results (cached or pending)
         POST /holes/<n>/pick/<k>  — select approach k for hole n

The serve loop:
  1. On file save → re-discover holes → diff against previous holes
  2. For each NEW or CHANGED hole → enqueue diversify job
  3. Worker pool processes jobs, writes to cache
  4. HTTP API and REPL both read from cache — instant results
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

from .types import Hole, Expansion, Filler, MoleFile
from .operations import discover, diversify
from .cache import CacheManager, cache_key, context_hash, source_hash, get_cache
from .context import assemble_context, DEFAULT_LAYERS
from .backends import get_backend, detect_language


# ─── Hole Diff ──────────────────────────────────────────────────────────────

@dataclass
class HoleDiff:
    """Result of diffing two hole discovery passes."""
    added: list[Hole]      # holes in new but not old
    removed: list[Hole]    # holes in old but not new
    changed: list[Hole]    # holes whose description or type changed
    unchanged: list[Hole]  # holes that are identical

    @property
    def needs_work(self) -> list[Hole]:
        """Holes that need new diversify generation."""
        return self.added + self.changed


def diff_holes(old: list[Hole], new: list[Hole]) -> HoleDiff:
    """Compare two hole lists and categorize changes.

    Identity is based on line proximity + description similarity.
    A hole that moved a few lines but kept the same description is 'unchanged'.
    A hole at the same position with a different description is 'changed'.
    """
    if not old:
        return HoleDiff(added=list(new), removed=[], changed=[], unchanged=[])
    if not new:
        return HoleDiff(added=[], removed=list(old), changed=[], unchanged=[])

    added: list[Hole] = []
    removed: list[Hole] = []
    changed: list[Hole] = []
    unchanged: list[Hole] = []

    # Index old holes by description for O(1) lookup
    old_by_desc: dict[str, Hole] = {h.description: h for h in old}
    matched_old: set[str] = set()

    for new_hole in new:
        if new_hole.description in old_by_desc:
            # Same description — check if type changed
            old_hole = old_by_desc[new_hole.description]
            matched_old.add(new_hole.description)
            if new_hole.expected_type != old_hole.expected_type:
                changed.append(new_hole)
            else:
                unchanged.append(new_hole)
        else:
            # No exact match — check for line proximity with different desc
            found_nearby = False
            for oh in old:
                if oh.description in matched_old:
                    continue
                if abs(new_hole.line_no - oh.line_no) <= 3:
                    # Same position, different description → changed
                    matched_old.add(oh.description)
                    changed.append(new_hole)
                    found_nearby = True
                    break
            if not found_nearby:
                added.append(new_hole)

    # Any old holes not matched are removed
    for oh in old:
        if oh.description not in matched_old:
            removed.append(oh)

    return HoleDiff(added=added, removed=removed, changed=changed, unchanged=unchanged)


# ─── Job Queue ──────────────────────────────────────────────────────────────

@dataclass
class DiversifyJob:
    """A pending diversify generation for one hole."""
    hole: Hole
    source: str
    path: Path
    enqueued_at: float = field(default_factory=time.monotonic)
    future: Optional[Future] = None

    @property
    def hole_id(self) -> str:
        """Stable identifier for this hole."""
        return f"{self.hole.line_no}:{self.hole.description[:40]}"


# ─── Worker Pool ────────────────────────────────────────────────────────────

class WorkerPool:
    """Background diversify generation pool.

    Manages a ThreadPoolExecutor that processes DiversifyJobs.
    Thread-safe: the job queue is protected by a lock.

    Design decisions:
      - Max 2 concurrent diversifies (each spawns 3 Claude processes)
      - Jobs are deduplicated by hole_id — re-enqueue replaces old job
      - Completed jobs write directly to CacheManager
    """

    def __init__(
        self,
        filler: Filler,
        cache: CacheManager,
        max_workers: int = 2,
    ):
        self._filler = filler
        self._cache = cache
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._lock = threading.Lock()
        self._jobs: dict[str, DiversifyJob] = {}  # hole_id → job
        self._completed: dict[str, list[Expansion]] = {}  # hole_id → results
        self._errors: dict[str, str] = {}  # hole_id → error message

    def enqueue(self, hole: Hole, source: str, path: Path) -> None:
        """Submit a diversify job for a hole. Deduplicates by hole_id."""
        job = DiversifyJob(hole=hole, source=source, path=path)
        with self._lock:
            old = self._jobs.get(job.hole_id)
            if old and old.future and not old.future.done():
                old.future.cancel()
            self._jobs[job.hole_id] = job
            job.future = self._pool.submit(self._run_job, job)

    def _run_job(self, job: DiversifyJob) -> None:
        """Execute a single diversify job. Called by thread pool."""
        try:
            results = diversify(
                hole_target=job.hole,
                source=job.source,
                path=job.path,
                filler=self._filler,
                cache=self._cache,
                use_cache=True,
            )
            with self._lock:
                self._completed[job.hole_id] = results
                self._errors.pop(job.hole_id, None)
        except Exception as e:
            with self._lock:
                self._errors[job.hole_id] = str(e)

    def status(self) -> dict:
        """Get pool status: pending, running, completed, errored counts."""
        with self._lock:
            pending = sum(1 for j in self._jobs.values()
                        if j.future and not j.future.done())
            return {
                "pending": pending,
                "completed": len(self._completed),
                "errored": len(self._errors),
                "total_jobs": len(self._jobs),
            }

    def get_result(self, hole_id: str) -> Optional[list[Expansion]]:
        """Get completed diversify results for a hole_id."""
        with self._lock:
            return self._completed.get(hole_id)

    def get_error(self, hole_id: str) -> Optional[str]:
        """Get error message for a failed job."""
        with self._lock:
            return self._errors.get(hole_id)

    def shutdown(self) -> None:
        """Stop all workers gracefully."""
        self._pool.shutdown(wait=False, cancel_futures=True)


# ─── File Watcher ───────────────────────────────────────────────────────────

class FileWatcher:
    """Watches a source file for changes using polling.

    Uses mtime + content hash to detect real changes (not just touches).
    On change: re-discovers holes, diffs against previous, enqueues new work.

    Polling interval: 1 second (good enough for save-on-edit detection).
    """

    def __init__(
        self,
        path: Path,
        worker_pool: WorkerPool,
        on_change: Optional[callable] = None,
    ):
        self._path = path.resolve()
        self._pool = worker_pool
        self._on_change = on_change
        self._last_mtime: float = 0
        self._last_hash: str = ""
        self._last_holes: list[Hole] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start watching in a background thread."""
        self._running = True
        # Do initial discovery
        self._check_and_process()
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the watcher."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _watch_loop(self) -> None:
        """Polling loop — checks file every second."""
        while self._running:
            time.sleep(1.0)
            try:
                self._check_and_process()
            except Exception:
                pass  # Don't crash the watcher on transient errors

    def _check_and_process(self) -> None:
        """Check for file changes and process if needed.

        Uses mtime as fast path, content hash as confirmation.
        First call always triggers (self._last_hash is empty).
        """
        try:
            stat = self._path.stat()
        except OSError:
            return

        # Fast path: mtime unchanged → skip
        if stat.st_mtime == self._last_mtime and self._last_hash:
            return

        # mtime changed — check content hash to confirm real change
        content = self._path.read_text()
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

        changed = content_hash != self._last_hash
        self._last_mtime = stat.st_mtime
        self._last_hash = content_hash

        if not changed and self._last_holes:
            return

        # Re-discover holes
        source = self._path.read_text()
        try:
            mole_file = discover(self._path)
        except Exception:
            return

        new_holes = mole_file.holes
        diff = diff_holes(self._last_holes, new_holes)
        self._last_holes = new_holes

        # Enqueue work for new/changed holes
        for h in diff.needs_work:
            self._pool.enqueue(h, source, self._path)

        if self._on_change:
            self._on_change(diff)


# ─── HTTP API ───────────────────────────────────────────────────────────────

class MoleAPIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the mole serve API.

    Routes:
      GET  /holes              → JSON list of all holes + cache status
      GET  /holes/<n>/diversify → diversify results for hole n
      POST /holes/<n>/pick/<k>  → select approach k for hole n
      GET  /status             → server status (watcher, pool, cache)
    """

    def _get_server(self) -> "MoleHTTPServer":
        """Type-safe accessor for our custom server."""
        return self.server  # type: ignore[return-value]

    def do_GET(self) -> None:
        """Handle GET requests."""
        import re as _re
        path = self.path
        srv = self._get_server()

        if path == "/holes":
            holes_data = []
            for i, h in enumerate(srv.holes):
                holes_data.append({
                    "index": i + 1,
                    "line": h.line_no,
                    "description": h.description,
                    "type": h.expected_type,
                    "status": h.status.value,
                })
            self._json_response({"holes": holes_data, "count": len(holes_data)})

        elif m := _re.match(r"/holes/(\d+)/diversify", path):
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(srv.holes):
                hole = srv.holes[idx]
                hole_id = f"{hole.line_no}:{hole.description[:40]}"
                result = srv.worker_pool.get_result(hole_id)
                error = srv.worker_pool.get_error(hole_id)
                if result:
                    self._json_response({
                        "hole": idx + 1,
                        "status": "ready",
                        "approaches": [
                            {"name": e.approach_name, "description": e.approach_description,
                             "code": e.expanded_code, "sub_holes": len(e.sub_holes)}
                            for e in result
                        ],
                    })
                elif error:
                    self._json_response({"hole": idx + 1, "status": "error", "error": error})
                else:
                    self._json_response({"hole": idx + 1, "status": "pending"})
            else:
                self._error(404, f"Hole {idx + 1} not found (have {len(srv.holes)})")

        elif path == "/status":
            pool_status = srv.worker_pool.status()
            self._json_response({
                "file": str(srv.source_path),
                "holes": len(srv.holes),
                "pool": pool_status,
            })

        else:
            self._error(404, f"Unknown route: {path}. Try /holes, /holes/<n>/diversify, /status")

    def do_POST(self) -> None:
        """Handle POST requests."""
        import re as _re
        path = self.path
        srv = self._get_server()

        if m := _re.match(r"/holes/(\d+)/pick/(\d+)", path):
            hole_idx = int(m.group(1)) - 1
            approach_idx = int(m.group(2)) - 1
            if 0 <= hole_idx < len(srv.holes):
                hole = srv.holes[hole_idx]
                hole_id = f"{hole.line_no}:{hole.description[:40]}"
                result = srv.worker_pool.get_result(hole_id)
                if result and 0 <= approach_idx < len(result):
                    picked = result[approach_idx]
                    self._json_response({
                        "picked": {"name": picked.approach_name, "code": picked.expanded_code},
                    })
                else:
                    self._error(404, "Approach not found or results not ready")
            else:
                self._error(404, f"Hole {hole_idx + 1} not found")
        else:
            self._error(404, f"Unknown route: {path}. Try POST /holes/<n>/pick/<k>")

    def log_message(self, format, *args):
        """Suppress default HTTP logging."""
        pass

    # ─── Response Helpers ─────────────────────────────────────────────

    def _json_response(self, data: dict, status: int = 200) -> None:
        """Send a JSON response."""
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        """Send an error response."""
        self._json_response({"error": message}, status)


class MoleHTTPServer(HTTPServer):
    """Extended HTTPServer that carries mole state."""

    def __init__(
        self,
        addr: tuple[str, int],
        watcher: FileWatcher,
        worker_pool: WorkerPool,
        cache: CacheManager,
        source_path: Path,
    ):
        super().__init__(addr, MoleAPIHandler)
        self.watcher = watcher
        self.worker_pool = worker_pool
        self.cache = cache
        self.source_path = source_path
        self.holes: list[Hole] = []  # updated by watcher callback


# ─── Serve Entry Point ──────────────────────────────────────────────────────

def serve(
    path: Path,
    filler: Filler,
    host: str = "127.0.0.1",
    port: int = 3077,
    on_ready: Optional[callable] = None,
) -> None:
    """Start the mole serve mode.

    This is the main entry point called by `mole serve file.py`.
    Blocks until Ctrl+C. Starts:
      1. CacheManager (from cache.py)
      2. WorkerPool (2 concurrent diversify workers)
      3. FileWatcher (polls file every 1s)
      4. HTTP API (localhost:3077)

    The initial file scan triggers diversify for all unfilled holes.
    """
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"Source file not found: {path}")

    cache = get_cache()
    pool = WorkerPool(filler=filler, cache=cache, max_workers=2)

    def on_file_change(diff: HoleDiff) -> None:
        """Called by watcher when holes change."""
        httpd.holes = list(diff.unchanged) + list(diff.added) + list(diff.changed)
        n = len(diff.needs_work)
        if n > 0:
            print(f"  ↻ {n} hole{'s' if n != 1 else ''} need generation")

    watcher = FileWatcher(path=path, worker_pool=pool, on_change=on_file_change)
    httpd = MoleHTTPServer(
        addr=(host, port),
        watcher=watcher,
        worker_pool=pool,
        cache=cache,
        source_path=path,
    )

    # Start components
    watcher.start()

    if on_ready:
        on_ready(host, port)

    print(f"  🕳 mole serve → {path.name}")
    print(f"  📡 http://{host}:{port}/holes")
    print(f"  watching for changes... (Ctrl+C to stop)\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopping...")
    finally:
        watcher.stop()
        pool.shutdown()
        httpd.server_close()
