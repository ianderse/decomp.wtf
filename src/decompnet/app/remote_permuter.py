"""decomp-permuter on another computer, run by the app: batches out, results back, judged, over SSH.

Every TICK seconds, for each PS1 project, while a host is set in Settings (an SSH name this computer can reach without a
password, e.g. a Mac Studio over Tailscale):

1. its results come back (rsync) and are imported through the judge (`decomp permuter import`, its own process):
   matches are saved, improvements become functions' best attempts, and a judged output isn't judged again;
2. when the permuter isn't running there, or its batch is older than ROTATE, a fresh batch of near misses is
   exported (improvements are their new starting points), copied there (rsync --delete) and started (setup.sh, then
   decomp-permuter with --best-only --stack-diffs: stack offsets count, as they do for the judge).

Nothing here is required: no host, or a host that doesn't answer, and the app works as before. What it needs there:
decomp.wtf's toolchains installed (its Compilers & tools page, or `uv run decomp setup`) and decomp-permuter cloned
with its packages (docs/plans/permuter-on-m5.md).
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("decomp.wtf")
TICK = 20 * 60
ROTATE = 12 * 3600
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]


class RemotePermuter:
    def __init__(self, net, home: Path, settings):
        self.net, self.home, self.settings = net, Path(home), settings
        self.status: dict[str, dict] = {}  # slug -> what the agents panel shows
        self._lock = threading.Lock()

    # -- settings
    @property
    def host(self) -> str:
        return str(self.settings.data.get("permuter_host") or "").strip()

    @property
    def jobs(self) -> int:
        return max(1, min(int(self.settings.data.get("permuter_jobs") or 16), 256))

    @property
    def permuter_dir(self) -> str:
        return str(self.settings.data.get("permuter_dir") or "~/decomp-permuter").strip()

    # -- plumbing
    def _ssh(self, command: str, timeout: float = 120) -> subprocess.CompletedProcess:
        return subprocess.run([*SSH, self.host, command], capture_output=True, text=True, timeout=timeout)

    def _rsync(self, src: str, dst: str, delete: bool = False) -> subprocess.CompletedProcess:
        args = ["rsync", "-a", "--timeout=60", "-e", " ".join(SSH)] + (["--delete"] if delete else []) + [src, dst]
        return subprocess.run(args, capture_output=True, text=True, timeout=1800)

    def _cli(self, *args: str) -> subprocess.CompletedProcess:
        """`decomp permuter ...` in a process of its own (exporting and judging are busy work)."""
        return subprocess.run([sys.executable, "-m", "decompnet.app.cli", "permuter", *args], capture_output=True,
                              text=True, timeout=3600, env={**__import__("os").environ, "DECOMP_HOME": str(self.home)})

    def _remote_folder(self, slug: str) -> str:
        return f"permuter-{slug}"

    def _local(self, slug: str) -> Path:
        return self.home / ".decomp" / "permuter" / slug

    def running(self, slug: str) -> bool:
        res = self._ssh(f"pgrep -f {shlex.quote('permuter.py .*' + self._remote_folder(slug) + '/')} >/dev/null && echo yes")
        return res.stdout.strip() == "yes"

    # -- one round
    def tick(self, slug: str) -> dict:
        with self._lock:
            st = self.status.setdefault(slug, {"host": self.host})
            st["host"], st["checked_at"], st["error"] = self.host, time.time(), ""
            proj = self.net._project(slug)
            cfg = json.loads(proj["config"])
            batch = cfg.get("permuter_remote") or {}
            local, remote = self._local(slug), self._remote_folder(slug)
            try:
                is_running = self.running(slug)
            except (OSError, subprocess.SubprocessError) as e:
                st["error"] = f"can't reach {self.host}: {e}"
                return st
            st["running"] = is_running
            if local.exists() and batch:
                got = self._rsync(f"{self.host}:{remote}/", f"{local}/")
                if got.returncode == 0:
                    res = self._cli("import", slug, str(local))
                    stats = _stats(res.stdout)
                    if stats:
                        st["last_import"] = stats | {"at": time.time()}
                        if stats.get("matched") or stats.get("improved"):
                            with self.net.store.tx() as db:
                                self.net._event(db, proj["id"], None, "autopilot",
                                                f"permuter on {self.host}: {stats.get('matched', 0)} matched, "
                                                f"{stats.get('improved', 0)} improved")
                else:
                    st["error"] = f"copying the results back failed: {got.stderr.strip()[-200:]}"
            old = time.time() - (batch.get("started") or 0) > ROTATE
            if is_running and not old:
                return st
            if is_running:  # a fresh batch: what agents and the permuter improved since start from there
                self._ssh(f"pkill -f {shlex.quote('permuter.py .*' + remote + '/')}")
            elif self._ssh("pgrep -f 'permuter.py' >/dev/null && echo yes").stdout.strip() == "yes":
                st["error"] = (f"another decomp-permuter run is going on on {self.host} (started by hand?): "
                               "a batch starts once it's done")
                return st
            return self._start(slug, proj, st)

    def _start(self, slug: str, proj, st: dict) -> dict:
        local, remote = self._local(slug), self._remote_folder(slug)
        if local.exists():
            import shutil
            shutil.rmtree(local)
        res = self._cli("export", slug, str(local))
        n = sum(1 for p in local.glob("*/decomp.json")) if local.exists() else 0
        if n == 0:
            st["error"] = "no near misses to permute" if res.returncode == 0 else f"export failed: {res.stderr[-200:]}"
            return st
        sent = self._rsync(f"{local}/", f"{self.host}:{remote}/", delete=True)
        if sent.returncode != 0:
            st["error"] = f"copying the batch failed: {sent.stderr.strip()[-200:]}"
            return st
        start = ('export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"; '  # (ssh's PATH is bare)
                 f"sh {remote}/setup.sh >/dev/null && cd {self.permuter_dir} && "
                 f"(nohup .venv/bin/python permuter.py $HOME/{remote}/*/ -j {self.jobs} --best-only --stack-diffs "
                 f"> $HOME/{remote}/permuter.log 2>&1 &) && echo started")
        res = self._ssh(start)
        if "started" not in res.stdout:
            st["error"] = f"starting the permuter failed: {(res.stderr or res.stdout).strip()[-300:]}"
            return st
        with self.net.store.tx() as db:
            c = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()["config"])
            c["permuter_remote"] = {"started": time.time(), "functions": n, "host": self.host}
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(c), proj["id"]))
            self.net._event(db, proj["id"], None, "autopilot", f"permuter on {self.host}: started on {n} near misses")
        st["running"], st["batch"] = True, {"started": time.time(), "functions": n}
        return st

    def loop(self) -> None:
        """The app's background round: every PS1 ROM project, while a host is set."""
        time.sleep(60)
        while True:
            if self.host:
                for row in self.net.store.all("SELECT * FROM projects"):
                    try:
                        if json.loads(row["config"]).get("platform") == "psx" and not self.net.fork_slug(row["slug"]):
                            self.tick(row["slug"])
                    except Exception as e:  # the next round tries again
                        log.info("remote permuter %s: %s", row["slug"], e)
                        self.status.setdefault(row["slug"], {})["error"] = str(e)[:300]
            time.sleep(TICK)


def _stats(text: str) -> dict | None:
    """The import's summary line: permuter results: {...}."""
    import ast
    for line in reversed(text.splitlines()):
        if line.startswith("permuter results:"):
            try:
                return ast.literal_eval(line.split(":", 1)[1].strip())
            except (ValueError, SyntaxError):
                return None
    return None
