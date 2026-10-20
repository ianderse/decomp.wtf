"""The autopilot, the permuter, the look-alikes and the rebuilt-game check, each in a process of its own.

Both are busy Python loops (drafts, rewrites, diffs). Run as threads inside the app they held the interpreter most of
the time, and everything else (the GUI's requests, the agents' compiles, every database read) waited for it: a
4,000-row read took seconds instead of milliseconds. In their own process they use another core, at a lower priority,
and the app stays responsive. They share the app's database (SQLite in WAL mode serializes the writes).

Protocol: the app starts `python -m decompnet.app.worker <autopilot|permuter> <slug> <db> <workspace>`; the worker
prints its progress as one JSON object per line on stdout, and stops (cleanly, letting go of what it holds) when its
stdin closes, which is also what happens when the app quits or dies.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("decomp.wtf")
STOP_GRACE = 20  # seconds a worker gets to finish its current try before it's killed


class ProcStop:
    """Looks like the threading.Event the in-process loops use (`set`, `is_set`), but stops the worker process."""

    def __init__(self, proc: BackgroundProc):
        self.proc = proc

    def set(self) -> None:
        self.proc.stop()

    def is_set(self) -> bool:
        return self.proc.stopping

    def wait(self, timeout: float | None = None) -> bool:
        self.proc.done.wait(timeout)
        return self.proc.stopping


class BackgroundProc:
    """One worker process, seen from the app: `state` is a dict kept up to date from what it prints."""

    def __init__(self, kind: str, slug: str, db_path: str, workspace: Path, state: dict):
        self.kind, self.slug, self.state = kind, slug, state
        self.stopping = False
        self.done = threading.Event()
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0  # Ctrl-C in the app's console: not ours
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "decompnet.app.worker", kind, slug, str(db_path), str(workspace)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, text=True, encoding="utf-8",
            creationflags=flags, start_new_session=os.name != "nt")
        threading.Thread(target=self._read, daemon=True, name=f"{kind}-{slug}-reader").start()

    def _set(self, key: str, value) -> None:
        if isinstance(self.state, dict):
            self.state[key] = value
        else:
            setattr(self.state, key, value)

    def _read(self) -> None:
        try:
            for line in self.proc.stdout:
                try:
                    update = json.loads(line)
                except ValueError:
                    continue
                if isinstance(update, dict):
                    for k, v in update.items():
                        if k != "stop":
                            self._set(k, v)
        finally:
            code = self.proc.wait()
            now = self.state.get("state") if isinstance(self.state, dict) else getattr(self.state, "state", None)
            if now == "running":
                self._set("state", "stopped" if self.stopping else "done" if code == 0 else "error")
            if code and not self.stopping:
                log.info("%s for %s exited with code %s", self.kind, self.slug, code)
            self.done.set()

    def stop(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        try:
            self.proc.stdin.close()  # the worker's signal to finish up
        except OSError:
            pass

        def reap() -> None:
            try:
                self.proc.wait(STOP_GRACE)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        threading.Thread(target=reap, daemon=True).start()


def _report(state, stop: threading.Event, every: float = 1.0) -> None:
    """Print the loop's progress (a dict, or a dataclass) once a second until `stop`."""
    def snapshot() -> dict:
        d = state if isinstance(state, dict) else state.__dict__
        return {k: v for k, v in d.items() if k != "stop" and isinstance(v, (str, int, float, bool, type(None)))}
    while not stop.wait(every):
        print(json.dumps(snapshot()), flush=True)
    print(json.dumps(snapshot()), flush=True)


def main(argv: list[str]) -> int:
    kind, slug, db_path, workspace = argv
    if hasattr(os, "nice"):
        try:
            os.nice(10)  # the GUI and the agents come first
        except OSError:
            pass
    logger = logging.getLogger("decomp.wtf")
    try:  # the app's log, appended to (the app rotates it)
        handler = logging.FileHandler(Path(db_path).parent / "logs" / "decomp.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter(f"%(asctime)s %(levelname)s [{kind}] %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    except OSError:
        pass

    from .engine import RomNetwork
    net = RomNetwork(db_path, Path(workspace))  # in-process loops here: this *is* the worker

    if kind == "autopilot":
        state = net.start_autopilot(slug)
        stop = state["stop"]
        finished = lambda: state["state"] != "running"
    elif kind == "permuter":
        state = net.permuter.start(slug)
        if state is None:
            return 0
        stop = state.stop
        finished = lambda: state.state != "running"
    elif kind == "alike":  # look-alikes for every open function, again whenever agents matched more
        state = {"state": "running", "templates": 0, "refreshed_at": 0.0}
        stop = threading.Event()
        finished = lambda: state["state"] != "running"

        def loop() -> None:
            last = None
            try:
                while not stop.is_set():
                    proj = net.project_row(slug)
                    n = net.store.one("SELECT COUNT(*) AS n FROM functions WHERE project_id=? AND status='matched'",
                                      proj["id"])["n"]
                    if n != last:
                        state["templates"] = net.refresh_alikes(slug, stop)
                        state["compiler_hints"] = net.refresh_toolchain_hints(slug)  # fingerprints -> compilers
                        state["refreshed_at"], last = time.time(), n
                    stop.wait(600)
            finally:
                state["state"] = "stopped" if stop.is_set() else "done"
        threading.Thread(target=loop, daemon=True, name=f"alike-{slug}").start()
    elif kind == "relink":  # the rebuilt-game check, again after enough new matches or a header change
        state = {"state": "running", "phase": "waiting", "checked_at": 0.0}
        stop = threading.Event()
        finished = lambda: state["state"] != "running"

        def loop() -> None:
            try:
                while not stop.is_set():
                    if net.relink_due(slug):
                        state["phase"] = "rebuilding"
                        try:
                            res = net.relink(slug, log=lambda *_: None)
                            state["identical"], state["failures"] = res.get("identical"), len(res["failures"])
                        except Exception as e:  # the next round tries again
                            logging.getLogger("decomp.wtf").info("relink %s: %s", slug, e)
                            state["error"] = str(e)[:300]
                        state["phase"], state["checked_at"] = "waiting", time.time()
                    stop.wait(600)
            finally:
                state["state"] = "stopped" if stop.is_set() else "done"
        threading.Thread(target=loop, daemon=True, name=f"relink-{slug}").start()
    else:
        print(f"unknown worker {kind!r}", file=sys.stderr)
        return 2

    def watch_stdin() -> None:
        try:
            while sys.stdin.read(4096):
                pass
        except (OSError, ValueError):
            pass
        stop.set()
    threading.Thread(target=watch_stdin, daemon=True).start()

    reported = threading.Event()
    reporter = threading.Thread(target=_report, args=(state, reported), daemon=True)
    reporter.start()
    while not finished():
        time.sleep(0.5)
    for t in threading.enumerate():  # the loop's last bookkeeping (it says "done" before writing its summary)
        if t.name == f"{kind}-{slug}":
            t.join(30)
    reported.set()
    reporter.join(5)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
