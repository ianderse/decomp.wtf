"""Controlled experiments on how agents match: one factor at a time, arms assigned per function, results with
confidence intervals.

An experiment changes one thing (FACTORS) for part of the open functions: which part is decided by a hash of the
experiment's seed and the function's id, so a function stays in its arm across claims and the assignment can be
reproduced from the seed. A function enters an experiment when an agent first claims it (machine agents, like the
permuter, don't count), and its outcome is whether it matched before the experiment stopped; what agents spent on it
while they held it is its cost. The report gives each arm's match rate and cost per match with bootstrap 95%
intervals, and the difference from the first arm (the baseline): too noisy to act on while its interval includes 0
(or an arm has fewer than MIN_ARM functions). The sample size a 5-point difference would need at the rate seen so
far is given as a guide: a bigger effect shows sooner. (Looking every day and stopping at the first clear result
overstates it: decide the size up front, from that number.)

The memory A/B (memory_ab) is the first of these that existed: a memory experiment, while one runs, decides the arm
instead of the coin flip, and both are reported.
"""

from __future__ import annotations

import hashlib
import json
import random
import time

# factor -> its arms, the baseline first. What each changes:
FACTORS = {
    # what long-term memory recalls goes into the brief (network._memory_lines)
    "memory": ("memory", "control"),
    # the tiered helpers' model for the function: as the tier table says, always the strongest tier's, or always the
    # cheapest's (agents.Manager.tier)
    "tier": ("auto", "strong", "cheap"),
    # the permuters (the app's own and the remote one) work on the function's near misses, or leave it alone
    "permuter": ("on", "off"),
    # a near miss is tried with the project's other compiler setups (network._alternate_match), or isn't
    "alternates": ("on", "off"),
    # how many compiles without improvement before compile_and_diff says to move on: as usual, or twice that (does
    # the long tail of hard functions reward staying longer? network._move_on)
    "patience": ("normal", "long"),
}
DIFFERENCE = 0.05  # the smallest difference in match rate worth detecting (sample_size)
BOOTSTRAP = 2000
MIN_ARM = 30  # functions per arm before any difference counts


def arm_of(seed: str, function_id: int, arms: list[str] | tuple[str, ...]) -> str:
    h = int.from_bytes(hashlib.sha256(f"{seed}:{function_id}".encode()).digest()[:8], "big")
    return arms[h % len(arms)]


def active(store, project_id: int, factor: str | None = None) -> list[dict]:
    rows = store.all("SELECT * FROM experiments WHERE project_id=? AND stopped_at IS NULL"
                     + (" AND factor=?" if factor else "") + " ORDER BY id", project_id, *([factor] if factor else []))
    return [dict(r) | {"arms": json.loads(r["arms"])} for r in rows]


def arm(store, project_id: int, function_id: int, factor: str) -> str | None:
    """The function's arm in the running experiment on `factor`, or None (no experiment: everything as usual)."""
    for e in active(store, project_id, factor):
        return arm_of(e["seed"], function_id, e["arms"])
    return None


def start(store, project_id: int, factor: str, arms: list[str] | None = None, seed: str | None = None,
          note: str = "") -> int:
    if factor not in FACTORS:
        raise ValueError(f"no such factor {factor!r}: {', '.join(FACTORS)}")
    arms = list(arms or FACTORS[factor])
    if len(arms) < 2 or any(a not in FACTORS[factor] for a in arms):
        raise ValueError(f"arms for {factor}: two or more of {', '.join(FACTORS[factor])}")
    if active(store, project_id, factor):
        raise ValueError(f"an experiment on {factor} is already running: stop it first")
    seed = seed or f"{factor}-{random.randrange(1 << 32):08x}"
    with store.tx() as db:
        cur = db.execute("INSERT INTO experiments(project_id, factor, arms, seed, started_at, note) VALUES(?,?,?,?,?,?)",
                         (project_id, factor, json.dumps(arms), seed, time.time(), note[:500]))
        return cur.lastrowid


def stop(store, project_id: int, experiment_id: int) -> None:
    with store.tx() as db:
        db.execute("UPDATE experiments SET stopped_at=? WHERE id=? AND project_id=? AND stopped_at IS NULL",
                   (time.time(), experiment_id, project_id))


def enroll(store, project_id: int, function_id: int, skip: tuple[str, ...] = ()) -> None:
    """An agent claimed the function: it's in every running experiment from now on, in its arm; except those on a
    factor in `skip` (one this agent isn't subject to, like the tier model for a helper that isn't tiered)."""
    exps = [e for e in active(store, project_id) if e["factor"] not in skip]
    if not exps:
        return
    now = time.time()
    with store.tx() as db:
        for e in exps:
            db.execute("INSERT INTO experiment_units(experiment_id, function_id, arm, first_claim, claims) "
                       "VALUES(?,?,?,?,1) ON CONFLICT(experiment_id, function_id) DO UPDATE SET claims=claims+1",
                       (e["id"], function_id, arm_of(e["seed"], function_id, e["arms"]), now))


def charge(store, agent_id: int, usd: float, tokens: int) -> None:
    """What a session spent goes to the function its agent holds (or claimed last: a CLI agent reports its cost
    when its session ends), in every running experiment it's in."""
    if not (usd or tokens):
        return
    row = store.one("SELECT function_id FROM claims WHERE agent_id=?", agent_id)
    if row is None:  # a session's cost comes when it ends, after its claim: the function it claimed last
        last = store.one("SELECT e.detail, e.project_id FROM events e WHERE e.agent_id=? AND e.kind='claim' "
                         "AND e.created_at > ? ORDER BY e.id DESC LIMIT 1", agent_id, time.time() - 7200)
        row = last and store.one("SELECT id AS function_id FROM functions WHERE project_id=? AND name=?",
                                 last["project_id"], last["detail"])
    if row is None:
        return
    with store.tx() as db:
        db.execute("UPDATE experiment_units SET usd=usd+?, tokens=tokens+? WHERE function_id=? AND experiment_id IN "
                   "(SELECT id FROM experiments WHERE stopped_at IS NULL)", (usd, tokens, row["function_id"]))


def sample_size(p: float, d: float = DIFFERENCE) -> int:
    """Functions per arm to tell a rate of p from p + d (two-sided 5% level, 80% power)."""
    p = min(max(p, 0.01), 0.99 - d)
    q = p + d
    return int(((1.96 + 0.8416) ** 2 * (p * (1 - p) + q * (1 - q))) / (d * d)) + 1


def _ci(values: list[float], rng: random.Random, stat) -> tuple[float, float] | None:
    if len(values) < 2:
        return None
    draws = sorted(stat([values[rng.randrange(len(values))] for _ in values]) for _ in range(BOOTSTRAP))
    return draws[int(0.025 * BOOTSTRAP)], draws[int(0.975 * BOOTSTRAP) - 1]


def report(store, project_id: int) -> list[dict]:
    """Every experiment on the project, newest first: per arm n, matched, rate and cost per match with 95% intervals,
    and each arm's difference from the baseline, marked too noisy while its interval includes 0 or an arm is small."""
    out = []
    for e in store.all("SELECT * FROM experiments WHERE project_id=? ORDER BY id DESC", project_id):
        arms = json.loads(e["arms"])
        end = e["stopped_at"] or time.time()
        units = store.all(
            "SELECT u.arm, u.usd, u.tokens, (f.status='matched' AND f.matched_at >= u.first_claim AND f.matched_at <= ?) "
            "AS won FROM experiment_units u JOIN functions f ON f.id=u.function_id WHERE u.experiment_id=?", end, e["id"])
        rng = random.Random(e["seed"])
        by = {a: [u for u in units if u["arm"] == a] for a in arms}
        base = [float(u["won"]) for u in by[arms[0]]]
        rows = []
        for a in arms:
            won = [float(u["won"]) for u in by[a]]
            n, m = len(won), int(sum(won))
            usd = sum(u["usd"] for u in by[a])
            row = {"arm": a, "n": n, "matched": m, "rate": m / n if n else None,
                   "rate_ci": _ci(won, rng, lambda xs: sum(xs) / len(xs)), "usd": round(usd, 4),
                   "usd_per_match": round(usd / m, 4) if m else None,
                   "usd_per_match_ci": _ci([(u["usd"], float(u["won"])) for u in by[a]], rng,
                                           lambda xs: sum(x[0] for x in xs) / max(sum(x[1] for x in xs), 1)) if m else None}
            if a != arms[0] and n and base:
                diffs = []
                for _ in range(BOOTSTRAP):
                    s1 = [won[rng.randrange(n)] for _ in won]
                    s0 = [base[rng.randrange(len(base))] for _ in base]
                    diffs.append(sum(s1) / n - sum(s0) / len(s0))
                diffs.sort()
                lo, hi = diffs[int(0.025 * BOOTSTRAP)], diffs[int(0.975 * BOOTSTRAP) - 1]
                row["diff"] = (m / n) - sum(base) / len(base)
                row["diff_ci"] = (lo, hi)
            rows.append(row)
        rates = [r["rate"] for r in rows if r["rate"] is not None]
        need = sample_size(sum(rates) / len(rates)) if rates else sample_size(0.15)
        for r in rows[1:]:
            ci = r.get("diff_ci")
            r["too_noisy"] = ci is None or ci[0] <= 0 <= ci[1] or min(r["n"], rows[0]["n"]) < MIN_ARM
        out.append({"id": e["id"], "factor": e["factor"], "arms": arms, "seed": e["seed"], "note": e["note"] or "",
                    "started_at": e["started_at"], "stopped_at": e["stopped_at"], "needed_per_arm": need,
                    "arms_report": rows})
    return out
