"""Long-term memory across games: a Hindsight server (vectorize-io/hindsight) keeps what made functions match.

One memory bank per toolchain (named after it, e.g. gcc-2.7.2-psx), every memory tagged with its game, so what agents
work out about a compiler on one game helps on the next. Only what the judge verified goes in: a match (its C, and
the change from the best attempt before it that made it match) and accepted header edits, never an agent's claims
(a small model once "reported" a bug that didn't exist, and the agents after it believed it). What comes back goes
into claim briefs, so every agent gets it the same way (a CLI agent or the built-in one on a local model), without a
tool call of its own.

The server is optional: when it doesn't answer, briefs simply go without, and remembering is retried never (it's a
help, not a record: the app's database is the record).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("decomp.wtf")


class Memory:
    def __init__(self, url: str, share: float = 0.5, timeout: float = 4.0):
        self.url = url.rstrip("/")
        self.share = share  # the part of claims whose brief gets memory (the rest: the comparison, see memory_ab)
        self.timeout = timeout
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="memory")
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[float, list[str]]] = {}
        self._down_until = 0.0  # after a failure, don't make every brief wait for a server that isn't there
        self._banks: set[str] = set()

    def _post(self, path: str, body: dict, timeout: float) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")

    @staticmethod
    def _bank(bank: str) -> str:
        return urllib.parse.quote(bank, safe="")

    def _ensure_bank(self, bank: str) -> None:
        """A toolchain's bank, created on first use with a mission that keeps what's reusable (constructs and the
        instructions they give), not one function's names and addresses."""
        if bank in self._banks:
            return
        req = urllib.request.Request(f"{self.url}/v1/default/banks", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            known = {b.get("bank_id") for b in json.loads(r.read() or b"{}").get("banks") or []}
        if bank not in known:
            body = {"name": bank,
                    "mission": f"Matching decompilation of games built with {bank}: how C has to be written so this "
                               "compiler produces exactly the original machine code.",
                    "retain_mission": "Keep reusable lessons: which C construct, type or statement order gives which "
                                      "instructions or register allocation with this compiler, and the change that "
                                      "turned a near miss into an exact match. Game-specific conventions (struct "
                                      "layouts, helper signatures) are worth keeping too, tagged with their game. Drop "
                                      "one-off function names and addresses that don't describe anything reusable."}
            req = urllib.request.Request(f"{self.url}/v1/default/banks/{self._bank(bank)}", method="PUT",
                                         data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10):
                pass
        self._banks.add(bank)

    def retain(self, bank: str, content: str, context: str, tags: list[str], document_id: str,
               metadata: dict[str, str] | None = None) -> None:
        """Remember, in the background (never holds up the agent that just matched)."""
        item = {"content": content, "context": context, "document_id": document_id,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tags": tags,
                "metadata": {k: str(v) for k, v in (metadata or {}).items()}}

        def send() -> None:
            try:
                self._ensure_bank(bank)
            except Exception as e:
                log.info("memory: can't reach the bank %s: %s", bank, e)
                return
            path = f"/v1/default/banks/{self._bank(bank)}/memories"
            try:
                self._post(path, {"items": [item], "async": True}, 30)
            except urllib.error.HTTPError as e:
                if e.code != 422:
                    log.info("memory: retain failed: %s", e)
                    return
                try:  # a server without item-level tags: the older per-request ones
                    self._post(path, {"items": [{k: v for k, v in item.items() if k != "tags"}], "async": True,
                                      "document_tags": tags}, 30)
                except Exception as e2:
                    log.info("memory: retain failed: %s", e2)
            except Exception as e:
                log.info("memory: retain failed: %s", e)
        self._pool.submit(send)

    @property
    def up(self) -> bool:
        """False for a while after the server didn't answer."""
        return time.time() >= self._down_until

    def recall(self, bank: str, query: str, max_tokens: int = 700, limit: int = 5) -> list[str]:
        """What the bank knows that bears on `query`: up to `limit` short texts, best first ([] when unreachable)."""
        now = time.time()
        if now < self._down_until:
            return []
        key = (bank, query)
        with self._lock:
            hit = self._cache.get(key)
        if hit and now - hit[0] < 600:
            return hit[1]
        try:
            res = self._post(f"/v1/default/banks/{self._bank(bank)}/memories/recall",
                             {"query": query, "budget": "low", "max_tokens": max_tokens}, self.timeout)
        except urllib.error.HTTPError as e:
            if e.code == 404:  # no bank yet: nothing remembered with this compiler so far (the server is fine)
                return []
            log.info("memory: recall failed (%s); trying again in 5 minutes", e)
            self._down_until = now + 300
            return []
        except Exception as e:
            log.info("memory: recall failed (%s); trying again in 5 minutes", e)
            self._down_until = now + 300
            return []
        texts = []
        for r in res.get("results") or []:
            text = " ".join(str(r.get("text") or "").split())
            if text and text not in texts:
                texts.append(text[:400])
            if len(texts) >= limit:
                break
        with self._lock:
            self._cache[key] = (now, texts)
        return texts
