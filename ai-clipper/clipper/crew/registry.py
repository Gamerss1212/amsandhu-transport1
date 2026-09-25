"""The assignment registry and the engine that runs the crew's agents.

Every piece of work is an Assignment with a unique key (job, kind, lens, scope): adding the same key twice
returns the existing assignment, so no two agents are ever given the same job. Each agent is its own
worker thread that pulls the next assignment for its role the moment it is free (work stealing inside a
role), and helps a related role when its own queue is empty, so capacity is never idle while work waits.

Independence is enforced here, not just promised: an agent only receives its own assignment's payload.
Submitted findings are sealed - only command roles (consensus, dedupe, ranking, coverage) can open them,
and only after the phase they belong to has closed. Conflict-of-interest rules keep an agent that
nominated a clip from verifying that same clip.

Reliability: the dispatcher re-queues work that failed or stalled (attempt tokens make a late answer from
a stalled attempt harmless), gives up after `max_attempts` and records why, restarts dead agent threads,
and caps how many agents may work on one clip at once.
"""
from __future__ import annotations

import itertools
import threading
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from ..agents import BOARD, ROLES
from ..media import CANCEL, Cancelled, fmt_ts
from .narrate import describe

COMMAND = {"chief", "dispatch", "consensus", "dedupe", "ranker", "coverage"}
# roles run by the crew's own worker threads (intake, production and publishing agents are driven by
# the pipeline and the posting service)
CREW_DIVISIONS = {"command", "full", "section", "gate", "verify"}
# who may help whom when their own queue is empty (never across a conflict of interest)
HELPS = {
    "section": ("v_stability",),
    "full": ("v_stability", "v_duplicates"),
    "gate": ("v_claims", "v_boundary", "v_context"),
    "verify": ("v_stability", "v_boundary", "v_hook", "v_context", "v_claims", "v_duplicates", "v_audio",
               "v_frames"),
}


@dataclass
class Assignment:
    id: str
    job: str
    role: str
    kind: str
    scope: str
    label: str
    payload: dict
    span: tuple[float, float] | None = None
    priority: float = 0.0
    exclude: frozenset = frozenset()
    timeout: float = 300.0
    max_attempts: int = 3
    status: str = "queued"          # queued | running | done | failed | cancelled
    agent: str | None = None
    attempts: int = 0
    token: int = 0
    started: float = 0.0
    history: list = field(default_factory=list)
    helper: bool = False             # taken by an agent from another role that was otherwise idle

    def record(self) -> dict:
        return {"id": self.id, "kind": self.kind, "role": self.role, "scope": self.scope, "label": self.label,
                "span": [round(self.span[0], 2), round(self.span[1], 2)] if self.span else None,
                "status": self.status, "agent": self.agent, "attempts": self.attempts,
                "helper": self.helper, "history": self.history}


class Job:
    def __init__(self, job_id: str, ctx: Any, seq: int) -> None:
        self.id, self.ctx, self.seq = job_id, ctx, seq
        self.assignments: dict[str, Assignment] = {}
        self.keys: dict[tuple, str] = {}
        self.sealed: dict[str, Any] = {}      # assignment id -> result (opened by command roles only)
        self.closed: set[str] = set()         # phases whose findings may be opened
        self.log: list[dict] = []             # coordinator decisions, in order
        self.started = time.time()


class Registry:
    def __init__(self) -> None:
        # one lock; agents sleep on their own role's signal (so an event wakes one right agent, not all 150),
        # coordinators waiting for a phase sleep on `cond`
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.role_cv: dict[str, threading.Condition] = defaultdict(lambda: threading.Condition(self.lock))
        self.jobs: dict[str, Job] = {}
        self.queues: dict[str, list[str]] = defaultdict(list)   # role -> assignment ids (with job prefix)
        self.index: dict[str, tuple[str, str]] = {}            # assignment id -> (job id, assignment id)
        self.running_per_scope: dict[tuple[str, str], int] = defaultdict(int)
        self.scope_caps: dict[tuple[str, str], int] = {}
        self._ids = itertools.count(1)
        self._jobs = itertools.count(1)
        self.executors: dict[str, Callable] = {}

    # ---------------------------------------------------------------- jobs and assignments
    def open_job(self, job_id: str, ctx: Any) -> Job:
        with self.cond:
            job = Job(job_id, ctx, next(self._jobs))
            self.jobs[job_id] = job
            return job

    def close_job(self, job_id: str) -> None:
        with self.cond:
            job = self.jobs.pop(job_id, None)
            if not job:
                return
            for a in job.assignments.values():
                if a.status == "queued":
                    a.status = "cancelled"
            for role, q in self.queues.items():
                self.queues[role] = [x for x in q if self.index.get(x, ("",))[0] != job_id]
            for aid in job.assignments:
                self.index.pop(aid, None)
            self.cond.notify_all()

    def add(self, job: Job, role: str, kind: str, scope: str, label: str, payload: dict, *,
            key: tuple | None = None, span=None, priority: float = 0.0, exclude=(), timeout: float = 300.0,
            max_attempts: int = 3) -> Assignment:
        """Queue an assignment; the same key twice returns the first one (no duplicate assignments)."""
        key = key or (kind, role, scope)
        with self.cond:
            if key in job.keys:
                return job.assignments[job.keys[key]]
            aid = f"A{next(self._ids):06d}"
            a = Assignment(aid, job.id, role, kind, scope, label, payload, span, priority, frozenset(exclude),
                           timeout, max_attempts)
            job.assignments[aid] = a
            job.keys[key] = aid
            self.index[aid] = (job.id, aid)
            self.queues[role].append(aid)
            self.role_cv[role].notify()
            return a

    def cap(self, job: Job, scope: str, n: int) -> None:
        """At most n agents at once on this scope (one clip never takes the whole team)."""
        with self.cond:
            self.scope_caps[(job.id, scope)] = n

    def note(self, job: Job, who: str, what: str, **data) -> None:
        with self.cond:
            job.log.append({"t": round(time.time() - job.started, 2), "by": who, "what": what, **data})

    # ---------------------------------------------------------------- taking and finishing work
    def _eligible(self, a: Assignment, agent_id: str) -> bool:
        if a.status != "queued" or agent_id in a.exclude:
            return False
        cap = self.scope_caps.get((a.job, a.scope))
        return cap is None or self.running_per_scope[(a.job, a.scope)] < cap

    def _pick(self, role: str, agent_id: str) -> Assignment | None:
        best, best_key = None, None
        for aid in self.queues.get(role, ()):
            job_id, _ = self.index.get(aid, (None, None))
            job = self.jobs.get(job_id)
            if not job:
                continue
            a = job.assignments[aid]
            if not self._eligible(a, agent_id):
                continue
            k = (job.seq, -a.priority, aid)  # oldest video first, then the most valuable work
            if best_key is None or k < best_key:
                best, best_key = a, k
        return best

    def take(self, agent_id: str, role: str, helps: tuple = (), wait: float = 2.0) -> Assignment | None:
        deadline = time.time() + wait
        with self.cond:
            while True:
                a = self._pick(role, agent_id)
                helper = False
                if a is None:
                    for other in helps:
                        a = self._pick(other, agent_id)
                        if a is not None:
                            helper = True
                            break
                if a is not None:
                    self.queues[a.role].remove(a.id)
                    a.status, a.agent, a.helper = "running", agent_id, helper
                    a.attempts += 1
                    a.token += 1
                    a.started = time.time()
                    self.running_per_scope[(a.job, a.scope)] += 1
                    a.history.append({"agent": agent_id, "start": round(a.started, 3), "status": "running"})
                    return a
                left = deadline - time.time()
                if left <= 0:
                    return None
                self.role_cv[role].wait(left)

    def _release(self, a: Assignment) -> None:
        k = (a.job, a.scope)
        self.running_per_scope[k] = max(0, self.running_per_scope[k] - 1)

    def complete(self, a: Assignment, token: int, result: Any) -> bool:
        with self.cond:
            job = self.jobs.get(a.job)
            if job is None or a.token != token or a.status != "running":
                return False  # a stalled attempt answering late: the retry already owns it
            self._release(a)
            a.status = "done"
            a.history[-1].update(status="done", end=round(time.time(), 3))
            job.sealed[a.id] = result
            self.cond.notify_all()
            if (a.job, a.scope) in self.scope_caps:  # a capped clip has room again
                self.role_cv[a.role].notify()
            return True

    def fail(self, a: Assignment, token: int, error: str, stalled: bool = False) -> None:
        with self.cond:
            job = self.jobs.get(a.job)
            if job is None or a.token != token or a.status != "running":
                return
            self._release(a)
            a.history[-1].update(status="stalled" if stalled else "failed", end=round(time.time(), 3),
                                 error=error[:300])
            if a.attempts < a.max_attempts and not CANCEL.is_set():
                a.status = "queued"
                # the retry goes to someone else when anyone else of that role is left to take it
                if set(BOARD.ids(a.role)) - a.exclude - {a.agent}:
                    a.exclude = a.exclude | {a.agent}
                self.queues[a.role].append(a.id)
                self.role_cv[a.role].notify_all()
            else:
                a.status = "failed"
            self.cond.notify_all()

    def watchdog(self) -> list[Assignment]:
        """Stalled work (running past its timeout) is taken back and re-queued."""
        now = time.time()
        stalled = []
        with self.cond:
            for job in self.jobs.values():
                for a in job.assignments.values():
                    if a.status == "running" and now - a.started > a.timeout:
                        stalled.append((a, a.token))
        for a, token in stalled:
            self.fail(a, token, f"stalled: no answer after {a.timeout:.0f}s", stalled=True)
        return [a for a, _ in stalled]

    # ---------------------------------------------------------------- waiting and reading results
    def wait(self, job: Job, ids: list[str], poll: float = 0.5, on_progress=None) -> None:
        """Block until every assignment in `ids` has finished (done or failed for good)."""
        while True:
            with self.cond:
                pending = [i for i in ids if job.assignments[i].status in ("queued", "running")]
                if not pending:
                    return
                if CANCEL.is_set():
                    for i in pending:
                        a = job.assignments[i]
                        if a.status == "queued":
                            a.status = "cancelled"
                            if a.id in self.queues[a.role]:
                                self.queues[a.role].remove(a.id)
                    raise Cancelled()
                self.cond.wait(poll)
            if on_progress:
                on_progress(len(ids) - len(pending), len(ids))

    def open_sealed(self, job: Job, requester: str, ids: list[str], phase: str) -> dict[str, Any]:
        """Findings can only be opened by command roles, and only once their phase is closed."""
        role = requester.rsplit("-", 1)[0]
        if role not in COMMAND:
            raise PermissionError(f"{requester} may not read other agents' findings")
        if phase not in job.closed:
            raise PermissionError(f"phase {phase} is still open: findings stay sealed until every reviewer submits")
        with self.cond:
            return {i: job.sealed[i] for i in ids if i in job.sealed}

    def close_phase(self, job: Job, phase: str) -> None:
        with self.cond:
            job.closed.add(phase)

    def stats(self) -> dict:
        with self.cond:
            out = {"queued": 0, "running": 0, "done": 0, "failed": 0, "jobs": len(self.jobs)}
            for job in self.jobs.values():
                for a in job.assignments.values():
                    if a.status in out:
                        out[a.status] += 1
            return out


def _task_line(a: Assignment, ev) -> str:
    """What the agent is doing right now, specific enough to picture it: the job, the stretch of video
    it is reading, which video, and whether it is a retry."""
    line = ("Helping: " if a.helper else "") + a.label
    if a.span and a.kind not in ("map", "plan", "full", "consensus", "dedupe", "shortlist", "decide", "coverage"):
        line += f" · {fmt_ts(a.span[0])}-{fmt_ts(a.span[1])}"
    title = (getattr(ev, "meta", None) or {}).get("title") if ev is not None else None
    if title:
        line += f" · in \"{title[:40]}\""
    if a.attempts > 1:
        line += f" · attempt {a.attempts}"
    return line


class Crew:
    """The crew's agents as live worker threads: started once, running for as long as the app is open."""

    def __init__(self) -> None:
        self.registry = Registry()
        self.threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._started = False
        self.restarts = 0

    def executor(self, kind: str):
        def register(fn):
            self.registry.executors[kind] = fn
            return fn
        return register

    def start(self) -> None:
        with self._lock:
            if self._started:
                self._revive()
                return
            self._started = True
            for role, division, *_ in ROLES:
                if division not in CREW_DIVISIONS:
                    continue
                for agent_id in BOARD.ids(role):
                    self._spawn(agent_id, role, division)

    def _spawn(self, agent_id: str, role: str, division: str) -> None:
        target = self._dispatcher if role == "dispatch" else self._agent
        t = threading.Thread(target=target, args=(agent_id, role, division), daemon=True, name=agent_id)
        self.threads[agent_id] = t
        t.start()

    def _revive(self) -> int:
        """Restart any agent whose thread died (it should never happen, but the team must stay whole)."""
        n = 0
        for agent_id, t in list(self.threads.items()):
            if not t.is_alive():
                a = BOARD.get(agent_id)
                self._spawn(agent_id, a["role"], a["division"])
                self.restarts += 1
                n += 1
        return n

    def _agent(self, agent_id: str, role: str, division: str) -> None:
        reg = self.registry
        helps = tuple(h for h in HELPS.get(division, ()) if h != role)
        while True:
            BOARD.alive(agent_id)
            try:
                a = reg.take(agent_id, role, helps)
            except Exception:  # never let a bookkeeping error kill an agent
                time.sleep(1)
                continue
            if a is None:
                continue
            token = a.token
            job = reg.jobs.get(a.job)
            fn = reg.executors.get(a.kind)
            ev = getattr(job.ctx, "ev", None) if job else None
            BOARD.start(agent_id, _task_line(a, ev))
            ok = False
            try:
                if job is None or fn is None:
                    raise RuntimeError(f"no executor for {a.kind}")
                t0 = time.time()
                result = fn(a, job.ctx, agent_id)
                ok = reg.complete(a, token, result)
                BOARD.report(agent_id, describe(a, result, ev) + f" · {time.time() - t0:.1f}s"
                             + ("" if ok else " (arrived late - a retry already took over)"), "done" if ok else "late")
            except Cancelled:
                reg.fail(a, token, "stopped")
                BOARD.report(agent_id, "Stopped: " + a.label, "fail")
            except Exception as exc:
                reg.fail(a, token, f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}")
                BOARD.report(agent_id, f"Hit a problem on '{a.label}' ({type(exc).__name__}) - will be retried", "fail")
            finally:
                BOARD.finish(agent_id, ok)

    def _dispatcher(self, agent_id: str, role: str, division: str) -> None:
        """Watchdog: re-queues stalled work, restarts dead agents, keeps the board's pulse."""
        while True:
            try:
                stalled = self.registry.watchdog()
                revived = self._revive()
                s = self.registry.stats()
                busy = s["running"]
                BOARD.alive(agent_id)
                BOARD.beat("dispatch", f"{busy} job(s) running, {s['queued']} queued across {s['jobs']} video(s)"
                           + (f" - re-queued {len(stalled)} stalled" if stalled else "")
                           + (f" - restarted {revived} agent(s)" if revived else ""))
            except Exception:
                pass
            time.sleep(1.0)


CREW = Crew()
