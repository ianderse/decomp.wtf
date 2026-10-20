"""The coordinator: who works on what, what counts as a match, and what everyone has learned.

Every public method here is one agent-facing action. They return plain text because the
consumer is an LLM reading a tool result; structured data lives in `status_json()` for
the website.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import random
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import experiments, policy, scoring, toolchains
from .compiler import CompileResult, compile_source
from .elfobj import FunctionObj, Reloc, extract_function
from .project import compiler_config, toolchain_id, with_toolchain
from .store import Store

CLAIM_TTL = 30 * 60
# A function an agent just released goes to the back of the queue for this long, for every agent: the next one
# starts on something fresh instead of passing the same function back and forth. Asked for by name, it comes at once.
RELEASE_COOLDOWN = 15 * 60
# the other compiler versions of a platform, tried on a near miss: a game is sometimes built with two of them
# (":gnu-as": without maspsx, for code that wasn't built with Sony's assembler, like some library code)
# ("@2.34": an older version of Sony's assembler; on SLUS-01252 62 stuck best attempts matched that way)
# ("/O1": built at -O1; 38 of them with GCC 2.8.1 there)
_PSX_OLDER = ("gcc-2.8.1-psx@2.34", "gcc-2.8.1-psx@2.21", "gcc-2.7.2-psx@2.21", "gcc-2.7.2-psx@2.34", "gcc-2.8.1-psx@2.56",
              "gcc-2.8.1-psx/O1", "gcc-2.7.2-psx/O1")
# earlier PSY-Q GCCs (built natively on macOS, see docs/plans/psx-gcc-macos.sh), tried right after the other
# current one: on SLUS-01252 208 of 1605 stuck best attempts matched with 2.6.3 (2.6.0: the same ones, ASPSX 2.21 on
# top: none more)
_PSX_26 = ("gcc-2.6.3-psx", "gcc-2.6.0-psx")
ALTERNATES = {"gcc-2.7.2-psx": ("gcc-2.8.1-psx", *_PSX_26, "gcc-2.7.2-psx:gnu-as", "gcc-2.8.1-psx:gnu-as", *_PSX_OLDER),
              "gcc-2.8.1-psx": ("gcc-2.7.2-psx", *_PSX_26, "gcc-2.8.1-psx:gnu-as", "gcc-2.7.2-psx:gnu-as", *_PSX_OLDER)}
MACHINE_AGENTS = ("autopilot", "permuter")  # decomp.wtf's own workers: their claims never mean "given up on"
# Twins first (see app/twins.py): a function whose code the game has more than once (boost: how many more times)
# and isn't too big or given up on, best value per instruction first; its twins (boost -1) last, as they match for
# free once it does.
_TWINS = ("(f.boost > 0 AND f.n_insns <= 400 AND f.abandons < 2) DESC, "
          "CASE WHEN f.boost > 0 THEN (f.boost + 1) * 64 / (f.n_insns + 16) ELSE 0 END DESC, f.boost < 0, ")
STRATEGIES = {
    "easiest": _TWINS + "f.abandons > 0, f.n_insns ASC",
    # what nobody gave up on, small first; among functions of about the same size, the files closest to done (a
    # file is linked into the game once all of it matches). Leftovers agents released again and again come last,
    # or a crowd of agents spends its time passing the hardest functions around.
    "finish": _TWINS + "f.abandons ASC, (f.n_insns + 31) / 32 ASC, (SELECT COUNT(*) FROM functions g WHERE "
                       "g.project_id=f.project_id AND g.unit=f.unit AND g.status='open') ASC, f.n_insns ASC",
    # near-misses, the ones given up on least first (else every agent resumes the same stuck one)
    "closest": "f.best_score IS NULL, f.abandons ASC, CAST(f.best_score AS REAL) / (f.n_insns * 100) ASC",
    "hardest": "f.n_insns DESC",
    # functions with a matched look-alike (functions.meta alike_sim, see RomNetwork.refresh_alikes), closest first:
    # for helpers that do best adapting a template (a small local model)
    "templates": "(json_extract(f.meta, '$.alike_sim') >= 0.6) DESC, json_extract(f.meta, '$.alike_sim') DESC, "
                 "f.abandons > 0, f.n_insns ASC",
    # a strong agent next to such helpers: the small templated functions are left to them
    "strong": _TWINS + "(COALESCE(json_extract(f.meta, '$.alike_sim'), 0) >= 0.6 AND f.n_insns <= 120) ASC, "
                       "f.abandons > 0, f.n_insns ASC",
    "random": "RANDOM()",
}



_IDENT = re.compile(r"\b[A-Za-z_]\w*\b")
_KEYWORDS = {"struct", "union", "enum", "typedef", "extern", "static", "const", "volatile", "unsigned", "signed",
             "void", "char", "short", "int", "long", "float", "double", "register", "inline", "define", "if", "endif"}


def _header_items(header: str) -> list[tuple[str, set[str], set[str]]]:
    """context.h as top-level declarations: (text, the names it declares, every identifier in it)."""
    items, depth, start, k = [], 0, 0, 0
    flat = re.sub(r"/\*.*?\*/|//[^\n]*", lambda m: " " * len(m.group()), header, flags=re.S)

    def add(text: str, flat_text: str) -> None:
        idents = set(_IDENT.findall(flat_text)) - _KEYWORDS
        names: set[str] = set()
        if flat_text.lstrip().startswith("#"):
            m = re.match(r"\s*#\s*define\s+(\w+)", flat_text)
            names = {m.group(1)} if m else set()
        else:
            for m in re.finditer(r"\b(?:struct|union|enum)\s+(\w+)", flat_text):
                names.add(m.group(1))
            outer = re.sub(r"\{.*\}", " ", flat_text, flags=re.S).split("=")[0]  # the declarators, outside any body
            pointer = re.search(r"\(\s*\*\s*(\w+)\s*\)", outer)  # (*name)(...): a function pointer
            if pointer:
                names.add(pointer.group(1))
            elif "(" in outer:  # a prototype: the name before its parameters
                m = re.search(r"(\w+)\s*\(", outer)
                if m:
                    names.add(m.group(1))
            else:
                for part in outer.split(","):
                    m = re.search(r"(\w+)\s*(?:\[[^\]]*\]\s*)*;?\s*$", part)
                    if m:
                        names.add(m.group(1))
            names -= _KEYWORDS
        text = text.strip()
        if "\n\n" in text:  # only the comment right above it belongs to it
            text = text[text.rindex("\n\n"):].strip()
        if text:
            items.append((text, names, idents))

    while k < len(flat):
        ch = flat[k]
        if depth == 0 and ch == "#" and flat[start:k].strip() == "":
            end = flat.find("\n", k)
            end = len(flat) if end < 0 else end
            add(header[k:end], flat[k:end])
            start = k = end + 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == ";" and depth == 0:
            add(header[start:k + 1], flat[start:k + 1])
            start = k + 1
        k += 1
    return items


def header_excerpt(header: str, code: str, budget: int = 60) -> list[str]:
    """The declarations of context.h that `code` (a function's assembly) refers to, and the types those use: what
    an agent needs from a 600-line header for one small function, without reading all of it."""
    items = _header_items(header)
    used = set(_IDENT.findall(code))
    picked = [i for i, (_, names, _) in enumerate(items) if names & used]
    seen = set(picked)
    for _ in range(2):  # the types those declarations use, and the types inside those
        want = set().union(*(items[i][2] for i in picked)) if picked else set()
        more = [i for i, (_, names, _) in enumerate(items) if i not in seen and names & want]
        seen.update(more)
        picked += more
    lines: list[str] = []
    for i in sorted(seen):
        text = items[i][0]
        if len(lines) + text.count("\n") + 1 > budget:
            lines.append("/* ... more: get_context() */")
            break
        lines += text.splitlines()
    return lines

class NetworkError(Exception):
    """An error meant to be shown to the agent as-is."""


@dataclass
class Agent:
    id: int
    name: str
    owner: str | None
    trusted: bool = False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 120 else f"{s // 60}m ago" if s < 7200 else f"{s // 3600}h ago"


class Network:
    def __init__(self, db_path: str | Path, claim_ttl: int = CLAIM_TTL):
        self.store = Store(db_path)
        self.agent_strategy: dict[int, str] = {}  # agent id -> its default claim order (helpers, see agents.Manager)
        self.tiered_agents: set[int] = set()  # agents in tiered helper sessions (the tier experiment counts only theirs)
        self.memory = None  # long-term memory across games (memory.Memory: a Hindsight server), when set up
        self.claim_ttl = claim_ttl
        self._pool = ThreadPoolExecutor(max_workers=8)

    # ------------------------------------------------------------------ agents

    def register_agent(self, name: str, owner: str | None = None, *, trusted: bool = True, origin: str = "cli",
                       terms_accepted: bool = False) -> str:
        """Mint an agent token. Operator-created (CLI) agents are trusted; self-registered (web) agents
        start at reputation level 'new' and earn the rest through verified matches."""
        name = name.strip()
        if not name or len(name) > 40 or not all(c.isalnum() or c in "-_." for c in name):
            raise NetworkError("agent name must be 1-40 chars of [A-Za-z0-9-_.]")
        owner = (owner or "").strip()[:60] or None
        token = "dn_" + secrets.token_urlsafe(24)
        now = time.time()
        with self.store.tx() as db:
            if db.execute("SELECT 1 FROM agents WHERE lower(name)=lower(?)", (name,)).fetchone():
                raise NetworkError(f"agent name {name!r} is taken")
            db.execute(
                "INSERT INTO agents(name,owner,token_hash,created_at,trusted,origin,terms_accepted_at) VALUES(?,?,?,?,?,?,?)",
                (name, owner, _hash_token(token), now, int(trusted), origin, now if terms_accepted else None),
            )
        return token

    def authenticate(self, token: str | None) -> Agent:
        if not token:
            raise NetworkError("missing API token: add `--header \"Authorization: Bearer dn_...\"` to your MCP config")
        row = self.store.one("SELECT id,name,owner,trusted FROM agents WHERE token_hash=?", _hash_token(token))
        if row is None:
            raise NetworkError("invalid API token")
        with self.store.tx() as db:
            db.execute("UPDATE agents SET last_seen=? WHERE id=?", (time.time(), row["id"]))
        return Agent(row["id"], row["name"], row["owner"], bool(row["trusted"]))

    def _require_budget(self, agent: Agent) -> None:
        ok, why = policy.compile_budget(self.store, agent.id)
        if not ok:
            raise NetworkError(why)

    def _require_level(self, agent: Agent, what: str) -> None:
        rep = policy.reputation(self.store, agent.id)
        if not rep["level"].can_edit_context:
            raise NetworkError(f"{what} needs reputation level 'member' (one verified match). You are '{rep['level'].name}': "
                               f"match a function first; add_note is open to everyone.")

    # ---------------------------------------------------------------- helpers

    def _project(self, slug: str | None):
        if slug:
            row = self.store.one("SELECT * FROM projects WHERE slug=?", slug)
            if row is None:
                raise NetworkError(f"no project {slug!r}; call list_projects")
            return row
        rows = self.store.all("SELECT * FROM projects")
        if len(rows) == 1:
            return rows[0]
        raise NetworkError("several projects exist; pass `project` (see list_projects)")

    def _function(self, name: str, project: str | None):
        if project is None and "/" in name and "@" not in name.split("/", 1)[0]:  # project/name (fork names hold units)
            project, name = name.split("/", 1)
        if project is None:
            rows = self.store.all("SELECT * FROM functions WHERE name=?", name)
            if len(rows) > 1:
                raise NetworkError(f"{name!r} exists in several projects; pass `project`")
            row = rows[0] if rows else None
        else:
            row = self.store.one(
                "SELECT f.* FROM functions f JOIN projects p ON p.id=f.project_id WHERE p.slug=? AND f.name=?",
                project, name,
            )
        if row is None:
            raise NetworkError(f"no function {name!r}" + (f" in {project!r}" if project else ""))
        return row

    def _context(self, project_id: int, unit: str | None = ""):
        """Latest header for a translation unit, falling back to the project-wide one. Real codebases have
        one header set per source file; small projects just have the project-wide header ('' unit)."""
        unit = unit or ""
        return self.store.one(
            "SELECT * FROM context_versions WHERE project_id=? AND unit IN (?, '') "
            "ORDER BY unit=? DESC, version DESC LIMIT 1", project_id, unit, unit)

    def _unit_contexts(self, project_id: int) -> set[str]:
        return {r["unit"] for r in self.store.all(
            "SELECT DISTINCT unit FROM context_versions WHERE project_id=? AND unit != ''", project_id)}

    def _effective_unit(self, fn, units: set[str]) -> str:
        return fn["unit"] if fn["unit"] in units else ""

    @staticmethod
    def _hash_only(fn) -> bool:
        return fn["target"] is None

    def _target_obj(self, fn, proj) -> FunctionObj:
        return FunctionObj(
            name=fn["name"], arch=proj["arch"], big_endian=bool(proj["big_endian"]), code=fn["target"],
            relocs=[Reloc.from_json(r) for r in json.loads(fn["relocs"])],
        )

    def _expire_claims(self, db) -> None:
        now = time.time()
        own = {r["id"] for r in db.execute("SELECT id FROM agents WHERE name IN (?, ?)", MACHINE_AGENTS)}
        for row in db.execute("SELECT c.function_id, c.agent_id, f.project_id, f.name FROM claims c "
                              "JOIN functions f ON f.id=c.function_id WHERE c.expires_at < ?", (now,)).fetchall():
            if row["agent_id"] in own:  # decomp.wtf's own tries (cut short by a restart): nobody gave up on it
                continue
            db.execute("UPDATE functions SET abandons=abandons+1 WHERE id=?", (row["function_id"],))
            self._event(db, row["project_id"], row["agent_id"], "expired", row["name"])
        db.execute("DELETE FROM claims WHERE expires_at < ?", (now,))

    def _event(self, db, project_id, agent_id, kind, detail) -> None:
        db.execute("INSERT INTO events(project_id,agent_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                   (project_id, agent_id, kind, detail, time.time()))

    def _agent_name(self, agent_id) -> str:
        row = self.store.one("SELECT name FROM agents WHERE id=?", agent_id) if agent_id else None
        return row["name"] if row else "?"

    def _compile(self, proj, header: str, source: str, fn=None, toolchain: str | None = None):
        """With `fn`: that function's own compiler when it has one (a game built with two compiler versions)."""
        cfg = json.loads(proj["config"])
        tc = toolchain or (self.fn_toolchain(fn) if fn is not None else None)
        if tc and "toolchain" in cfg.get("compiler", {}):
            cfg = {**cfg, "compiler": with_toolchain(cfg["compiler"], tc)}
        res = compile_source(compiler_config(Path(proj["root"]), cfg), header, source)
        if not res.ok and "got fatal signal" in res.log and (tid := cfg.get("compiler", {}).get("toolchain")):
            res = self._explain_crash(proj, tid, header, source, res)
        return res

    def _explain_crash(self, proj, tid: str, header: str, source: str, res):
        """The compiler crashed instead of saying what's wrong (the arm64 macOS builds of the old PS1 GCCs do, on
        some messages): the same GCC version, built for Intel, reads the source and says it."""
        try:
            other = toolchains.get(toolchain_id(tid)).explain_with
            if not other or toolchains.current_platform() not in toolchains.get(toolchain_id(tid)).no_warnings_on:
                return res
            toolchains.ensure(other, log=lambda *_: None)
            said = compile_source(compiler_config(Path(proj["root"]), {"compiler": {"toolchain": other, "flags": [
                "-O2", "-fsyntax-only"]}}), header, source)
        except Exception:
            return res
        why = "\n".join(ln for ln in said.log.splitlines() if "func.o" not in ln).strip()
        if not why:
            return CompileResult(False, None, res.log.strip() + "\nThe compiler crashed on code it accepts (GCC 2.7.2 "
                                 "finds nothing wrong with it): write that statement another way.")
        return CompileResult(False, None, f"{why}\n(the PS1 compiler's macOS build crashed printing this; the same "
                                          "GCC version built for Intel says it)")

    def _alternates_on(self, proj, fn) -> bool:
        """Whether a near miss gets tried with the other compiler setups (not, in an alternates experiment's "off"
        arm). Keeping existing matches (re-judging) always tries them."""
        return experiments.arm(self.store, proj["id"], fn["id"], "alternates") != "off"

    def _alternate_match(self, fn, proj, header: str, source: str) -> str | None:
        """A near miss that is byte-identical with another compiler version of the platform: that compiler, now
        recorded as the function's own (so its regression tests and the export build use it)."""
        comp = json.loads(proj["config"]).get("compiler", {})
        main = comp.get("toolchain")
        current = self.fn_toolchain(fn) or main
        options = [main, *comp.get("alternates", ALTERNATES.get(main, ()))]
        for tc in dict.fromkeys(t for t in options if t and t != current):
            if toolchains.installed(toolchain_id(tc)) is None:
                continue
            res = self._compile(proj, header, source, toolchain=tc)
            if not res.ok:
                continue
            cand, verdict, _ = self._evaluate(fn, proj, res.obj)
            if cand is None:
                continue
            if verdict is None:
                verdict = cand.fingerprint() == self._target_obj(fn, proj).fingerprint()
            if verdict:
                self.set_fn_toolchain(fn, None if tc == main else tc)
                return tc
        return None

    def set_fn_toolchain(self, fn, toolchain: str | None) -> None:
        with self.store.tx() as db:
            row = db.execute("SELECT meta FROM functions WHERE id=?", (fn["id"],)).fetchone()
            try:
                meta = json.loads(row["meta"] or "{}") if row else {}
            except ValueError:
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            if toolchain:
                meta["toolchain"] = toolchain
            else:
                meta.pop("toolchain", None)
            db.execute("UPDATE functions SET meta=? WHERE id=?", (json.dumps(meta) if meta else None, fn["id"]))

    # ---------------------------------------------------------------- long-term memory (memory.py)

    def _memory_bank(self, proj, fn) -> str | None:
        """The bank a function's lessons go in: its compiler's (the project's, or the one it matched with)."""
        tc = self.fn_toolchain(fn) or json.loads(proj["config"]).get("compiler", {}).get("toolchain")
        return toolchain_id(tc) if tc else None

    def _memory_lines(self, proj, fn, agent) -> list[str]:
        """What memory has for a claimed function, for half the claims (chosen at random and recorded: memory_ab,
        so whether it helps is measured, not assumed); the other half go without."""
        bank = self._memory_bank(proj, fn)
        if self.memory is None or bank is None or agent.name in MACHINE_AGENTS or not self.memory.up:
            return []  # (while the server doesn't answer, claims stay out of the comparison)
        # a running memory experiment decides (per function, from its seed); otherwise the coin flip, as before
        arm = experiments.arm(self.store, proj["id"], fn["id"], "memory") or (
            "memory" if random.random() < self.memory.share else "control")
        texts: list[str] = []
        if arm == "memory":
            asm = scoring.compact_asm(self._asm_text(fn, proj))
            calls = sorted(set(re.findall(r"\bjal\s+(\w+)", asm)))
            query = (f"How to write C that {bank} compiles to exactly this {fn['n_insns']}-instruction function"
                     + (f" (it calls {', '.join(calls[:8])})" if calls else "") + ":\n" + "\n".join(asm.splitlines()[:40]))
            texts = self.memory.recall(bank, query)  # (a timeout still counts: recalled=0, or the arms would skew)
        with self.store.tx() as db:
            db.execute("INSERT INTO memory_ab(function_id,agent_id,project_id,arm,claimed_at,recalled) VALUES(?,?,?,?,?,?)",
                       (fn["id"], agent.id, proj["id"], arm, time.time(), len(texts) if arm == "memory" else None))
        if not texts:
            return []
        return ["", f"### From long-term memory ({bank}: what earlier matches taught, in any game; hints: check them "
                    "against the diff)", *[f"- {t}" for t in texts]]

    def _remember_match(self, proj, fn, agent, source: str) -> None:
        """A verified match into memory: its own C, and the change from the best attempt before it that made it match
        (the reusable part: which construct gives which instructions)."""
        bank = self._memory_bank(proj, fn)
        if self.memory is None or bank is None or agent.name in MACHINE_AGENTS:
            return
        try:
            from .rom.export import own_source
            words = set(re.findall(r"\b[A-Za-z_]\w*\b", source + (fn["best_source"] or "")))
            defined = {r["name"] for r in self.store.all(
                f"SELECT name FROM functions WHERE project_id=? AND name IN ({','.join('?' * len(words))})",
                proj["id"], *sorted(words))} if words else set()
            mine = own_source(source, fn["name"], defined).strip()
            before = own_source(fn["best_source"], fn["name"], defined).strip() if fn["best_source"] else ""
        except Exception:
            mine, before = source.strip(), (fn["best_source"] or "").strip()
        content = (f"{proj['name']} ({proj['slug']}), {fn['name']}: {fn['n_insns']} instructions, compiled with {bank} "
                   f"{' '.join(json.loads(proj['config']).get('compiler', {}).get('flags') or [])}. Matching C:\n{mine}\n")
        if before and before != mine:
            diff = [ln for ln in difflib.unified_diff(before.splitlines(), mine.splitlines(), lineterm="", n=1)
                    if not ln.startswith(("---", "+++"))][:40]
            content += (f"\nThe best attempt before it scored {fn['best_score']} (0 = exact); this change made it "
                        "match:\n" + "\n".join(diff) + "\n")
        notes = self.store.all("SELECT text FROM notes WHERE function_id=? ORDER BY id DESC LIMIT 2", fn["id"])
        if notes:
            content += "\nNotes agents left on it: " + " | ".join(" ".join(n["text"].split())[:300] for n in notes)
        self.memory.retain(bank, content, f"Verified by the judge: compiles byte for byte to the original function "
                           f"in {proj['name']}.", [f"game:{proj['slug']}"], f"{proj['slug']}:{fn['name']}",
                           {"game": proj["slug"], "function": fn["name"], "agent": agent.name, "kind": "match"})

    def _remember_header(self, proj, agent, old_header: str, new_header: str, message: str, version: int) -> None:
        bank = json.loads(proj["config"]).get("compiler", {}).get("toolchain")
        if self.memory is None or not bank or agent.name in MACHINE_AGENTS:
            return
        added = [ln[1:] for ln in difflib.unified_diff(old_header.splitlines(), new_header.splitlines(), lineterm="", n=0)
                 if ln.startswith("+") and not ln.startswith("+++")][:30]
        self.memory.retain(toolchain_id(bank), f"{proj['name']} ({proj['slug']}) shared header v{version}: "
                           f"{message.strip()}\n" + "\n".join(added), "An accepted change to the game's shared "
                           "header: every matched function still matched with it.", [f"game:{proj['slug']}"],
                           f"{proj['slug']}:header:v{version}", {"game": proj["slug"], "kind": "header"})

    def memory_report(self, project_id: int) -> list[dict]:
        """The memory A/B so far: per arm and agent, claims and how many the agent matched while holding them."""
        rows = self.store.all(
            "SELECT m.arm, a.name AS agent, COUNT(*) AS claims, SUM(COALESCE(m.recalled, 0) > 0) AS with_memories, "
            "SUM(f.status='matched' AND f.matched_agent_id=m.agent_id "
            "AND f.matched_at >= m.claimed_at AND f.matched_at < m.claimed_at + 7200) AS matched FROM memory_ab m "
            "JOIN agents a ON a.id=m.agent_id JOIN functions f ON f.id=m.function_id WHERE m.project_id=? "
            "GROUP BY m.arm, a.name ORDER BY a.name, m.arm", project_id)
        return [dict(r) for r in rows]

    @staticmethod
    def fn_toolchain(fn) -> str | None:
        """The compiler a function matched with when it isn't the project's (functions.meta's "toolchain")."""
        try:
            meta = json.loads(fn["meta"] or "{}")
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        return meta.get("toolchain") if isinstance(meta, dict) else None

    def _evaluate(self, fn, proj, obj: bytes) -> tuple[FunctionObj | None, bool | None, list[str]]:
        """(candidate, verdict, problems) for a compiled object. verdict None means: decide by comparing
        fingerprints. Projects whose targets come from a ROM link the candidate instead (see app.engine)."""
        return extract_function(obj, fn["name"]), None, []

    def _asm_text(self, fn, proj) -> str:
        return fn["asm"]

    def _extra_brief(self, fn, proj) -> list[str]:
        return []

    # ---------------------------------------------------------------- projects

    def list_projects(self) -> str:
        rows = self.store.all(
            """SELECT p.slug, p.name, p.arch, p.description, p.config, COUNT(f.id) AS total,
                      SUM(f.status='matched') AS matched, SUM(f.size) AS bytes,
                      SUM(CASE WHEN f.status='matched' THEN f.size ELSE 0 END) AS matched_bytes
               FROM projects p LEFT JOIN functions f ON f.project_id=p.id GROUP BY p.id ORDER BY p.slug"""
        )
        if not rows:
            return "No projects yet."
        out = []
        for r in rows:
            pct = 100 * (r["matched_bytes"] or 0) / (r["bytes"] or 1)
            legal = json.loads(r["config"]).get("legal", {})
            out.append(f"- {r['slug']}: {r['name']} [{r['arch']}] — {r['matched'] or 0}/{r['total']} functions "
                       f"matched ({pct:.1f}% of code); legal basis: {legal.get('basis')}, contributions licensed "
                       f"{legal.get('license')}\n  {r['description']}".rstrip())
        return "\n".join(out)

    def get_context(self, project: str | None, function: str | None = None) -> str:
        if function:
            fn = self._function(function, project)
            proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
            ctx = self._context(proj["id"], fn["unit"])
        else:
            proj = self._project(project)
            if self._unit_contexts(proj["id"]):
                raise NetworkError(f"{proj['slug']} has one header per source file: pass `function` to get the "
                                   f"header that function compiles against")
            ctx = self._context(proj["id"])
        return (f"// context.h for {proj['slug']} — version {ctx['version']} "
                f"(prepended to every compile as #include \"context.h\")\n{ctx['header']}")

    # ------------------------------------------------------------------ claims

    def claim(self, agent: Agent, project: str | None = None, function: str | None = None,
              strategy: str = "easiest") -> str:
        fn = self.claim_row(agent, project, function, strategy)
        if fn is None:
            proj = self._project(project)
            return f"Nothing left to claim in {proj['slug']} — every open function is matched or claimed. 🎉"
        return self._brief(fn, agent, header="CLAIMED")

    def claim_row(self, agent: Agent, project: str | None = None, function: str | None = None,
                  strategy: str = "easiest"):
        """Claim (or renew) a function and return its row, or None when nothing is left."""
        if strategy not in STRATEGIES:
            raise NetworkError(f"strategy must be one of {', '.join(STRATEGIES)}")
        proj = self._project(project) if (project or function is None) else None
        now = time.time()
        with self.store.tx() as db:
            self._expire_claims(db)
            held = db.execute("SELECT f.* FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id=?",
                              (agent.id,)).fetchone()
            if held is not None:
                if function and held["id"] != self._function(function, proj["slug"] if proj else None)["id"]:
                    raise NetworkError(f"you already hold {held['name']}; match it or release_function it first")
                fn = held
                db.execute("UPDATE claims SET expires_at=? WHERE function_id=?", (now + self.claim_ttl, fn["id"]))
            elif function:
                fn = self._function(function, proj["slug"] if proj else None)
                if fn["status"] == "matched":
                    raise NetworkError(f"{fn['name']} is already matched; use get_function to read its source")
                if fn["status"] == "asm":
                    raise NetworkError(f"{fn['name']} is hand-written assembly: no C compiles to it, so it's left out")
                other = db.execute("SELECT agent_id, expires_at FROM claims WHERE function_id=?", (fn["id"],)).fetchone()
                if other is not None:
                    raise NetworkError(f"{fn['name']} is claimed by {self._agent_name(other['agent_id'])} "
                                       f"for another {int(other['expires_at'] - now) // 60} min")
                db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fn["id"], agent.id, now, now + self.claim_ttl))
                self._event(db, fn["project_id"], agent.id, "claim", fn["name"])
            else:
                fn = db.execute(
                    f"""SELECT f.* FROM functions f WHERE f.project_id=? AND f.status='open'
                        AND f.id NOT IN (SELECT function_id FROM claims)
                        AND f.name NOT IN (SELECT detail FROM events WHERE project_id=? AND agent_id=? AND kind='release')
                        ORDER BY f.name IN (SELECT detail FROM events WHERE project_id=? AND kind='release'
                                            AND created_at > ?), {STRATEGIES[strategy]} LIMIT 1""",
                    (proj["id"], proj["id"], agent.id, proj["id"], now - RELEASE_COOLDOWN),
                ).fetchone()
                if fn is None:
                    return None
                db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fn["id"], agent.id, now, now + self.claim_ttl))
                self._event(db, fn["project_id"], agent.id, "claim", fn["name"])
        return fn

    def release(self, agent: Agent, function: str, notes: str, project: str | None = None) -> str:
        fn = self._function(function, project)
        with self.store.tx() as db:
            row = db.execute("SELECT agent_id FROM claims WHERE function_id=?", (fn["id"],)).fetchone()
            if row is None or row["agent_id"] != agent.id:
                raise NetworkError(f"you don't hold a claim on {fn['name']}")
            db.execute("DELETE FROM claims WHERE function_id=?", (fn["id"],))
            db.execute("UPDATE functions SET abandons=abandons+1 WHERE id=?", (fn["id"],))
            if notes.strip():
                db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(?,?,?,?,?)",
                           (fn["project_id"], fn["id"], agent.id, notes.strip(), time.time()))
            self._event(db, fn["project_id"], agent.id, "release", fn["name"])
        return (f"Released {fn['name']}. Your best attempt and notes are saved so the next agent starts "
                f"where you left off. Call claim_function for a new one.")

    def get_function(self, function: str, project: str | None = None, agent: Agent | None = None) -> str:
        return self._brief(self._function(function, project), agent, header="VIEW")

    def _brief(self, fn, agent: Agent | None, header: str, project_notes: bool = True) -> str:
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        ctx = self._context(proj["id"], fn["unit"])
        claim = self.store.one("SELECT * FROM claims WHERE function_id=?", fn["id"])
        lines = [f"## {header}: {proj['slug']}/{fn['name']}",
                 f"size {fn['size']} bytes, {fn['n_insns']} instructions, arch {proj['arch']}, "
                 f"context.h v{ctx['version']} ({len(ctx['header'].splitlines())} lines)"]
        if claim:
            who = "you" if agent and claim["agent_id"] == agent.id else self._agent_name(claim["agent_id"])
            lines.append(f"claimed by {who}, lease expires in {int(claim['expires_at'] - time.time()) // 60} min "
                         f"(each compile_and_diff renews it)")
        if fn["status"] == "matched":
            lines += [f"MATCHED by {self._agent_name(fn['matched_agent_id'])} {_ago(fn['matched_at'])}:",
                      "```c", fn["matched_source"].strip(), "```"]
        if self._hash_only(fn):
            lines += ["", "### Target assembly", "This is a hash-only project: the coordinator keeps a one-way fingerprint of "
                      "each function, never its code. Disassemble your own lawful copy with "
                      f"`decompnet local asm <your-object.o> {fn['name']}` and diff locally with `decompnet local diff`."]
        else:
            asm = self._asm_text(fn, proj)
            lines += ["", "### Target assembly", "```asm", scoring.compact_asm(asm), "```"]
            if fn["status"] != "matched" and (decls := header_excerpt(ctx["header"], asm)):
                lines += ["", f"### What context.h declares for it (v{ctx['version']}; the rest: get_context())",
                          "```c", *decls, "```"]
        lines += self._extra_brief(fn, proj)
        if fn["status"] != "matched":
            if fn["best_source"]:
                lines += ["", f"### Best attempt so far: score {fn['best_score']} by "
                              f"{self._agent_name(fn['best_agent_id'])} ({fn['attempts']} attempts total) — "
                              f"start from this", "```c", fn["best_source"].strip(), "```"]
            elif fn["seed"]:
                lines += ["", "### Machine-generated starting point (m2c: expect wrong types and names; "
                              "the context header helps)", "```c",
                          fn["seed"].strip(), "```"]
        # each note with its author's record: a note by an agent that matched nothing is a guess (a small local model
        # once reported the brief's assembly as "wrong", and the agents after it believed it)
        wins = "(SELECT COUNT(*) FROM functions m WHERE m.matched_agent_id=n.agent_id AND m.status='matched') AS wins"
        who = lambda n: f"{n['name']}, {n['wins']} match{'' if n['wins'] == 1 else 'es'}"
        notes = self.store.all(
            f"SELECT n.text, n.created_at, a.name, {wins} FROM notes n LEFT JOIN agents a ON a.id=n.agent_id "
            "WHERE n.function_id=? ORDER BY n.id DESC LIMIT 8", fn["id"])
        if notes:
            lines += ["", "### Notes from previous agents on this function (hints, not facts: the assembly above is "
                          "the exact target)"]
            lines += [f"- ({who(n)}, {_ago(n['created_at'])}) {n['text']}" for n in notes]
        pnotes = self.store.all(
            f"SELECT n.text, a.name, {wins} FROM notes n LEFT JOIN agents a ON a.id=n.agent_id "
            "WHERE n.project_id=? AND n.function_id IS NULL ORDER BY n.id DESC LIMIT 5", proj["id"])
        if header == "CLAIMED" and agent is not None and fn["status"] != "matched":
            if agent.name not in MACHINE_AGENTS:  # (a tier experiment counts only tiered sessions: the model is theirs)
                experiments.enroll(self.store, proj["id"], fn["id"],
                                   skip=() if agent.id in self.tiered_agents else ("tier",))
            lines += self._memory_lines(proj, fn, agent)
        if pnotes and project_notes:
            lines += ["", "### Project-wide notes (latest)"] + [f"- ({who(n)}) {n['text']}" for n in pnotes]
        if fn["unit"] and fn["unit"] in self._unit_contexts(proj["id"]):
            lines.insert(2, f"source unit {fn['unit']}: call get_context with function=\"{fn['name']}\" for its header")
        if header == "CLAIMED":
            lines += ["", "Next: read context.h with get_context, write C for this function, and iterate with "
                          "compile_and_diff until score is 0. Submit only the function body (plus any helper "
                          "declarations); context.h is included automatically."]
        return "\n".join(lines)

    # --------------------------------------------------------- compile & diff

    def compile_and_diff(self, agent: Agent, function: str, source: str, project: str | None = None) -> str:
        return self.attempt(agent, function, source, project)["text"]

    def attempt(self, agent: Agent, function: str, source: str, project: str | None = None) -> dict:
        """Compile, score and record one attempt. Returns {"text", "score", "matched"} (score None: didn't compile)."""
        self._require_budget(agent)
        fn = self._function(function, project)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        now = time.time()

        # Claim bookkeeping first: you can't score (or get credit) on someone else's function.
        with self.store.tx() as db:
            self._expire_claims(db)
            claim = db.execute("SELECT agent_id FROM claims WHERE function_id=?", (fn["id"],)).fetchone()
            scratch = fn["status"] == "matched"
            if not scratch:
                if claim is None:
                    held = db.execute("SELECT 1 FROM claims WHERE agent_id=?", (agent.id,)).fetchone()
                    if held:
                        raise NetworkError("you hold another claim; release it before working on a different function")
                    db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fn["id"], agent.id, now, now + self.claim_ttl))
                    self._event(db, fn["project_id"], agent.id, "claim", fn["name"])
                elif claim["agent_id"] != agent.id:
                    raise NetworkError(f"{fn['name']} is claimed by {self._agent_name(claim['agent_id'])}")
                else:
                    db.execute("UPDATE claims SET expires_at=? WHERE function_id=?", (now + self.claim_ttl, fn["id"]))

        ctx = self._context(proj["id"], fn["unit"])
        result = self._compile(proj, ctx["header"], source, fn)

        if not result.ok:
            self._record(fn, agent, None, source, ctx["version"])
            return {"text": f"❌ COMPILE ERROR (context v{ctx['version']})\n{result.log.strip()}", "score": None, "matched": False}

        cand, verdict, problems = self._evaluate(fn, proj, result.obj)
        if cand is None:
            self._record(fn, agent, None, source, ctx["version"])
            return {"text": f"❌ Compiled, but the object has no function named `{fn['name']}`. "
                            f"Define it with exactly that name (non-static).", "score": None, "matched": False}
        if self._hash_only(fn):
            matched_now = cand.fingerprint() == fn["fingerprint"] and not scratch
            self._record(fn, agent, 0 if matched_now else None, source, ctx["version"], matched=matched_now, compiled=True)
            if matched_now:
                return {"text": f"✅ MATCH! {fn['name']} matches the target fingerprint. Credited to {agent.name}; "
                                f"your claim is released.", "score": 0, "matched": True}
            return {"text": "❌ No match. This is a hash-only project, so the coordinator can only say match / no match. "
                            f"For a scored diff, run `decompnet local diff <your-object.o> {fn['name']} <your.c>` "
                            "against your own lawful copy.", "score": None, "matched": False}
        d = scoring.diff(self._target_obj(fn, proj), cand)
        if verdict is not None:
            d = scoring.DiffResult(0 if verdict else max(1, d.score), d.max_score, d.rows, verdict)
        if not d.bytes_match and not scratch and d.percent >= 90 and self._alternates_on(proj, fn) and \
                self._alternate_match(fn, proj, ctx["header"], source):
            d = scoring.DiffResult(0, d.max_score, d.rows, True)
        matched_now = d.bytes_match and not scratch
        self._record(fn, agent, d.score, source, ctx["version"], matched=matched_now)
        if matched_now:
            self._remember_match(proj, fn, agent, source)

        if scratch:
            head = f"(scratch — {fn['name']} was already matched) score {d.score}"
        elif d.bytes_match:
            return {"text": f"✅ MATCH! {fn['name']} is byte-identical to the target ({fn['n_insns']} instructions). "
                            f"Credited to {agent.name}; your claim is released. Call claim_function for the next one.",
                    "score": 0, "matched": True}
        else:
            best = self.store.one("SELECT best_score FROM functions WHERE id=?", fn["id"])["best_score"]
            head = (f"score {d.score} ({scoring.percent_text(d.percent)} match) — best so far {best}. "
                    f"Target {len([r for r in d.rows if r[1]])} insns, yours {len([r for r in d.rows if r[2]])}.")
            if d.rows and all(m == " " for m, _, _ in d.rows):
                head += ("\nNOT A MATCH YET: every instruction reads the same, but the bytes differ"
                         + (" because of the linking notes at the end: fix those." if problems else
                            ": an encoding the listing doesn't show (branch form, an immediate, a relocation)."))
        warn = f"\ncompiler output:\n{result.log.strip()}" if result.log.strip() else ""
        if problems:
            warn += "\nwhen linking your code at the function's address:\n" + "\n".join(f"- {p}" for p in problems)
        return {"text": f"{head}\n{d.render()}{warn}{self.stall_note(fn['id'], agent.id)}", "score": d.score, "matched": False}

    def stall_note(self, function_id: int, agent_id: int, after: int | None = None) -> str:
        """A nudge once an agent's last `after` compiles of a function didn't beat its best: functions that match
        mostly do in the first few tries, and the agent's time is better spent on the next one. Big functions get
        longer (a quarter of Mario Party 6's agent matches over 64 instructions took more than 4 compiles, and
        those functions hold most of the code left)."""
        if after is None:
            row = self.store.one("SELECT n_insns FROM functions WHERE id=?", function_id)
            n = (row["n_insns"] if row else 0) or 0
            after = 4 if n < 64 else 6 if n < 128 else 8 if n < 256 else 10
        rows = self.store.all("SELECT score FROM attempts WHERE function_id=? AND agent_id=? ORDER BY id DESC LIMIT 16",
                              function_id, agent_id)
        best, since = None, 0
        for r in reversed(rows):
            if r["score"] is not None and (best is None or r["score"] < best):
                best, since = r["score"], 0
            else:
                since += 1
        if since < after:
            return ""
        return (f"\n\n⏭ No better in your last {since} compiles. release_function with notes (what you tried, what's "
                "left) and claim the next one: functions that match mostly do in the first few tries, and this one "
                "keeps your best attempt for whoever comes next.")

    def _record(self, fn, agent: Agent, score: int | None, source: str, ctx_version: int, matched: bool = False,
                compiled: bool | None = None, via: str = "agent"):
        now = time.time()
        compiled = score is not None if compiled is None else compiled
        with self.store.tx() as db:
            db.execute("INSERT INTO attempts(function_id,agent_id,score,compiled,context_version,source,created_at,via) "
                       "VALUES(?,?,?,?,?,?,?,?)", (fn["id"], agent.id, score, compiled, ctx_version, source, now, via))
            db.execute("UPDATE functions SET attempts=attempts+1 WHERE id=?", (fn["id"],))
            if score is not None:
                db.execute("UPDATE functions SET best_score=?, best_source=?, best_agent_id=? "
                           "WHERE id=? AND status='open' AND (best_score IS NULL OR best_score > ?)",
                           (score, source, agent.id, fn["id"], score))
            if matched:
                db.execute("UPDATE functions SET status='matched', matched_source=?, matched_agent_id=?, "
                           "matched_at=?, best_score=0, best_source=? WHERE id=?",
                           (source, agent.id, now, source, fn["id"]))
                db.execute("DELETE FROM claims WHERE function_id=?", (fn["id"],))
                self._event(db, fn["project_id"], agent.id, "match", fn["name"])
        if matched:
            self.on_match(self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"]), fn["id"], agent)

    def on_match(self, proj, function_id: int, agent: Agent | None = None) -> None:
        """A function just matched (subclasses try its code on its twins)."""

    # -------------------------------------------------------- shared context

    def edit_context(self, agent: Agent, project: str | None, old: str, new: str, message: str,
                     function: str | None = None) -> str:
        """Exact-string edit of a header. Accepted only if every already-matched function compiled against
        that header still matches with the new one — that's the project's test suite."""
        self._require_level(agent, "Editing the shared header")
        if function:
            fn = self._function(function, project)
            proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
            unit = self._effective_unit(fn, self._unit_contexts(proj["id"]))
        else:
            proj = self._project(project)
            if self._unit_contexts(proj["id"]):
                raise NetworkError(f"{proj['slug']} has one header per source file: pass `function` to edit the "
                                   f"header that function compiles against")
            unit = ""
        if not message.strip():
            raise NetworkError("give a short commit message explaining the change")
        for _ in range(3):
            ctx = self._context(proj["id"], unit)
            header = ctx["header"]
            if old:
                from .textedit import replace_once

                new_header = replace_once(header, old, new, "the header")
            else:
                new_header = header.rstrip("\n") + "\n\n" + new.strip("\n") + "\n"
            members = self._matched_using(proj, unit)
            broken = self.regression_test(proj, new_header, only=members)
            if broken:
                listing = "\n".join(f"- {name}: {why}" for name, why in broken[:10])
                return (f"❌ REJECTED: the edit breaks {len(broken)} already-matched function(s), so the header "
                        f"stays at v{ctx['version']}.\n{listing}")
            with self.store.tx() as db:
                if self._latest_version(db, proj["id"], unit) != ctx["version"]:
                    continue  # someone else committed meanwhile: re-apply on top and re-test
                version = self._next_version(db, proj["id"])
                db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                           "VALUES(?,?,?,?,?,?,?)", (proj["id"], version, new_header, agent.id, message.strip(), time.time(), unit))
                self._event(db, proj["id"], agent.id, "context", f"v{version}{f' ({unit})' if unit else ''}: {message.strip()}")
            self._remember_header(proj, agent, header, new_header, message, version)
            return f"✅ header is now v{version} (all {len(members)} matched functions that use it still match)."
        raise NetworkError("the header is being edited heavily right now; re-read it and retry")

    def rename_symbol(self, agent: Agent, project: str | None, old: str, new: str, message: str = "") -> str:
        """Project-wide refactor: rename an identifier (field, type, global, function param...) in every
        header *and* in every matched source / best attempt, then regression-test the result.
        The rename is textual (whole words), so it is meant for unique names like `unk_1C` or `func_80012345`."""
        self._require_level(agent, "Renaming across the project")
        proj = self._project(project)
        ident = re.compile(r"^[A-Za-z_]\w*$")
        if not (ident.match(old) and ident.match(new)):
            raise NetworkError("old and new must be C identifiers")
        word = re.compile(rf"\b{re.escape(old)}\b")
        for _ in range(3):
            units = self._unit_contexts(proj["id"])
            current = {u: self._context(proj["id"], u) for u in ({""} | units)}
            changed = {u: word.sub(new, c["header"]) for u, c in current.items() if word.search(c["header"])}
            if not changed:
                raise NetworkError(f"`{old}` does not occur in any header")
            matched = self.store.all("SELECT * FROM functions WHERE project_id=? AND status='matched'", proj["id"])
            rewritten = {r["id"]: word.sub(new, r["matched_source"]) for r in matched}
            header_for = lambda fn: changed.get(self._effective_unit(fn, units), current[self._effective_unit(fn, units)]["header"])
            broken = self.regression_test(proj, None, sources=rewritten, only=matched, header_for=header_for)
            if broken:
                listing = "\n".join(f"- {name}: {why}" for name, why in broken[:10])
                return f"❌ REJECTED: renaming breaks {len(broken)} matched function(s).\n{listing}"
            msg = message.strip() or f"rename {old} -> {new}"
            with self.store.tx() as db:
                n_matched = db.execute("SELECT COUNT(*) AS n FROM functions WHERE project_id=? AND status='matched'",
                                       (proj["id"],)).fetchone()["n"]
                if n_matched != len(matched) or any(self._latest_version(db, proj["id"], u) != current[u]["version"]
                                                    for u in changed):
                    continue
                for u, header in changed.items():
                    version = self._next_version(db, proj["id"])
                    db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                               "VALUES(?,?,?,?,?,?,?)", (proj["id"], version, header, agent.id, msg, time.time(), u))
                for fid, src in rewritten.items():
                    db.execute("UPDATE functions SET matched_source=?, best_source=? WHERE id=?", (src, src, fid))
                for r in db.execute("SELECT id, best_source FROM functions WHERE project_id=? AND status='open' "
                                    "AND best_source IS NOT NULL", (proj["id"],)).fetchall():
                    db.execute("UPDATE functions SET best_source=? WHERE id=?", (word.sub(new, r["best_source"]), r["id"]))
                self._event(db, proj["id"], agent.id, "context", f"{msg} ({len(changed)} header(s))")
            return (f"✅ renamed `{old}` -> `{new}` in {len(changed)} header(s) and in {len(rewritten)} matched "
                    f"sources; all still match.")
        raise NetworkError("headers are being edited heavily right now; retry")

    def _latest_version(self, db, project_id: int, unit: str) -> int | None:
        row = db.execute("SELECT MAX(version) AS v FROM context_versions WHERE project_id=? AND unit=?",
                         (project_id, unit)).fetchone()
        return row["v"]

    def _next_version(self, db, project_id: int) -> int:
        return (db.execute("SELECT MAX(version) AS v FROM context_versions WHERE project_id=?",
                           (project_id,)).fetchone()["v"] or 0) + 1

    def _matched_using(self, proj, unit: str) -> list:
        units = self._unit_contexts(proj["id"])
        return [f for f in self.store.all("SELECT * FROM functions WHERE project_id=? AND status='matched'", proj["id"])
                if self._effective_unit(f, units) == unit]

    def regression_test(self, proj, header: str | None, sources: dict[int, str] | None = None,
                        only: list | None = None, header_for=None) -> list[tuple[str, str]]:
        matched = only if only is not None else self.store.all(
            "SELECT * FROM functions WHERE project_id=? AND status='matched'", proj["id"])

        def check(fn) -> tuple[str, str] | None:
            res = self._compile(proj, header_for(fn) if header_for else header,
                                (sources or {}).get(fn["id"], fn["matched_source"]), fn)
            if not res.ok:
                return fn["name"], "no longer compiles: " + res.log.strip().splitlines()[0][:200] if res.log.strip() else "compile failed"
            cand, verdict, _ = self._evaluate(fn, proj, res.obj)
            if verdict is None and cand is not None:
                want = fn["fingerprint"] if self._hash_only(fn) else self._target_obj(fn, proj).fingerprint()
                verdict = cand.fingerprint() == want
            if cand is None or not verdict:
                return fn["name"], "compiles but no longer matches"
            return None

        if not matched:  # nothing to protect, but the header must at least parse
            if header is None:
                return []
            res = self._compile(proj, header, "")
            return [] if res.ok else [("<header>", res.log.strip()[:300])]
        return [b for b in self._pool.map(check, matched) if b]

    # ------------------------------------------------------------------ notes

    def add_note(self, agent: Agent, project: str | None, text: str, function: str | None = None) -> str:
        proj = self._project(project)
        fn = self._function(function, proj["slug"]) if function else None
        if not text.strip():
            raise NetworkError("empty note")
        with self.store.tx() as db:
            db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(?,?,?,?,?)",
                       (proj["id"], fn["id"] if fn else None, agent.id, text.strip()[:4000], time.time()))
        return "Noted. Other agents will see it" + (f" when they open {fn['name']}." if fn else " in their briefings.")

    def search_notes(self, project: str | None, query: str) -> str:
        proj = self._project(project)
        rows = self.store.all(
            "SELECT n.text, n.created_at, a.name AS agent, f.name AS fn FROM notes n "
            "LEFT JOIN agents a ON a.id=n.agent_id LEFT JOIN functions f ON f.id=n.function_id "
            "WHERE n.project_id=? AND n.text LIKE ? ORDER BY n.id DESC LIMIT 20", proj["id"], f"%{query}%")
        if not rows:
            return f"No notes matching {query!r}."
        return "\n".join(f"- [{r['fn'] or 'project'}] ({r['agent']}, {_ago(r['created_at'])}) {r['text']}" for r in rows)

    # ----------------------------------------------------------------- status

    def status_json(self) -> dict:
        with self.store.tx() as db:
            self._expire_claims(db)
        projects = []
        for p in self.store.all("SELECT * FROM projects ORDER BY slug"):
            agg = self.store.one(
                "SELECT COUNT(*) AS total, SUM(status='matched') AS matched, SUM(size) AS bytes, "
                "SUM(CASE WHEN status='matched' THEN size ELSE 0 END) AS matched_bytes, SUM(attempts) AS attempts "
                "FROM functions WHERE project_id=?", p["id"])
            claims = self.store.all(
                "SELECT f.name, a.name AS agent, c.claimed_at, f.best_score, f.n_insns FROM claims c "
                "JOIN functions f ON f.id=c.function_id JOIN agents a ON a.id=c.agent_id "
                "WHERE f.project_id=? ORDER BY c.claimed_at", p["id"])
            ctx = self._context(p["id"])
            claimed = {c["name"] for c in claims}
            funcs = [{"name": f["name"], "insns": f["n_insns"], "best": f["best_score"],
                      "status": "matched" if f["status"] == "matched" else "claimed" if f["name"] in claimed else "open"}
                     for f in self.store.all("SELECT name, n_insns, status, best_score FROM functions "
                                             "WHERE project_id=? ORDER BY name", p["id"])]
            legal = json.loads(p["config"]).get("legal", {})
            projects.append({
                "legal": {"basis": legal.get("basis"), "license": legal.get("license")},
                "funcs": funcs,
                "slug": p["slug"], "name": p["name"], "arch": p["arch"], "description": p["description"],
                "functions": agg["total"], "matched": agg["matched"] or 0, "bytes": agg["bytes"] or 0,
                "matched_bytes": agg["matched_bytes"] or 0, "attempts": agg["attempts"] or 0,
                "context_version": ctx["version"] if ctx else 0,
                "claims": [{"function": c["name"], "agent": c["agent"], "since": c["claimed_at"],
                            "best_score": c["best_score"], "insns": c["n_insns"]} for c in claims],
            })
        leaders = self.store.all(
            "SELECT a.name, a.owner, COUNT(f.id) AS matches, COALESCE(SUM(f.n_insns),0) AS insns, a.last_seen "
            "FROM agents a LEFT JOIN functions f ON f.matched_agent_id=a.id "
            "GROUP BY a.id ORDER BY insns DESC, matches DESC LIMIT 25")
        events = self.store.all(
            "SELECT e.kind, e.detail, e.created_at, a.name AS agent, p.slug FROM events e "
            "LEFT JOIN agents a ON a.id=e.agent_id LEFT JOIN projects p ON p.id=e.project_id "
            "ORDER BY e.id DESC LIMIT 30")
        return {
            "projects": projects,
            "leaderboard": [dict(r) for r in leaders],
            "events": [dict(r) for r in events],
        }

    def status(self, agent: Agent | None = None) -> str:
        s = self.status_json()
        out = []
        for p in s["projects"]:
            pct = 100 * p["matched_bytes"] / (p["bytes"] or 1)
            out.append(f"{p['slug']}: {p['matched']}/{p['functions']} functions, {pct:.1f}% of code matched, "
                       f"context.h v{p['context_version']}, {len(p['claims'])} agents working")
            out += [f"  - {c['agent']} on {c['function']} ({c['insns']} insns, best {c['best_score']})"
                    for c in p["claims"]]
        out.append("Leaderboard (instructions matched):")
        out += [f"  {i + 1}. {r['name']}{' (you)' if agent and r['name'] == agent.name else ''}: "
                f"{r['matches']} functions / {r['insns']} insns" for i, r in enumerate(s["leaderboard"][:10])]
        return "\n".join(out)

    # ------------------------------------------------------------- moderation
    # Operator-only actions (CLI), for takedown requests and GDPR erasure requests.

    def remove_project(self, slug: str) -> int:
        proj = self._project(slug)
        with self.store.tx() as db:
            ids = [r["id"] for r in db.execute("SELECT id FROM functions WHERE project_id=?", (proj["id"],))]
            for table in ("claims", "attempts"):
                db.executemany(f"DELETE FROM {table} WHERE function_id=?", [(i,) for i in ids])
            for table in ("notes", "events", "context_versions", "functions", "symbols", "twin_misses"):
                db.execute(f"DELETE FROM {table} WHERE project_id=?", (proj["id"],))
            db.execute("DELETE FROM projects WHERE id=?", (proj["id"],))
        return len(ids)

    def reset_function(self, slug: str, name: str) -> None:
        """Remove every submission for one function (matched source, attempts, notes) and reopen it."""
        fn = self._function(name, slug)
        with self.store.tx() as db:
            for table in ("claims", "attempts", "notes"):
                db.execute(f"DELETE FROM {table} WHERE function_id=?", (fn["id"],))
            db.execute("UPDATE functions SET status='open', best_score=NULL, best_source=NULL, best_agent_id=NULL, "
                       "matched_source=NULL, matched_agent_id=NULL, matched_at=NULL, attempts=0, abandons=0 "
                       "WHERE id=?", (fn["id"],))

    def revoke_agent(self, name: str, erase: bool = False) -> None:
        """Disable an agent's token. With erase=True also strip its identity (GDPR erasure):
        the agent is renamed to an anonymous id and the operator name is removed."""
        row = self.store.one("SELECT id FROM agents WHERE name=?", name)
        if row is None:
            raise NetworkError(f"no agent named {name!r}")
        with self.store.tx() as db:
            db.execute("DELETE FROM claims WHERE agent_id=?", (row["id"],))
            db.execute("UPDATE agents SET token_hash=? WHERE id=?", ("revoked:" + secrets.token_hex(16), row["id"]))
            if erase:
                db.execute("UPDATE agents SET name=?, owner=NULL WHERE id=?", (f"erased-{row['id']}", row["id"]))

    def list_notes(self, slug: str) -> list[dict]:
        proj = self._project(slug)
        return [dict(r) for r in self.store.all(
            "SELECT n.id, f.name AS function, a.name AS agent, n.text FROM notes n LEFT JOIN functions f "
            "ON f.id=n.function_id LEFT JOIN agents a ON a.id=n.agent_id WHERE n.project_id=? ORDER BY n.id",
            proj["id"])]

    def learnings(self, slug: str, budget: int = 2500) -> str:
        """The project-wide notes agents left (newest first, within `budget` characters), for every new session:
        what one agent works out about the compiler or the project, the next one starts with."""
        proj = self._project(slug)
        out, used = [], 0
        for r in self.store.all("SELECT text FROM notes WHERE project_id=? AND function_id IS NULL ORDER BY id DESC LIMIT 40",
                                proj["id"]):
            text = " ".join(r["text"].split())[:600]
            if used + len(text) > budget:
                break
            out.append(f"- {text}")
            used += len(text)
        return "\n".join(out)

    def delete_note(self, note_id: int) -> None:
        with self.store.tx() as db:
            if db.execute("DELETE FROM notes WHERE id=?", (note_id,)).rowcount == 0:
                raise NetworkError(f"no note with id {note_id}")

    # ----------------------------------------------------------------- export

    def export(self, project: str, out_dir: Path) -> int:
        """Write matched sources + the current context as a tree you can PR upstream."""
        proj = self._project(project)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "context.h").write_text(self._context(proj["id"])["header"], encoding="utf-8")
        for unit in sorted(self._unit_contexts(proj["id"])):
            (out_dir / "contexts").mkdir(exist_ok=True)
            (out_dir / "contexts" / f"{unit}.h").write_text(self._context(proj["id"], unit)["header"], encoding="utf-8")
        rows = self.store.all(
            "SELECT f.name, f.matched_source, a.name AS agent, a.owner FROM functions f "
            "LEFT JOIN agents a ON a.id=f.matched_agent_id WHERE f.project_id=? AND f.status='matched' "
            "ORDER BY f.name", proj["id"])
        (out_dir / "src").mkdir(exist_ok=True)
        credits = []
        for r in rows:
            (out_dir / "src" / f"{r['name']}.c").write_text(
                f'#include "context.h"\n\n// matched by {r["agent"]}' + (f" (operator: {r['owner']})" if r["owner"] else "")
                + f"\n{r['matched_source'].strip()}\n", encoding="utf-8")
            credits.append(f"{r['name']}\t{r['agent']}\t{r['owner'] or ''}")
        (out_dir / "CREDITS.tsv").write_text("function\tagent\toperator\n" + "\n".join(credits) + "\n",
                                             encoding="utf-8")
        return len(rows)
