"""One video through the crew, phase by phase.

  0 map        5 full-video mappers build the shared evidence (scenes, voices, topics, structure, names)
  1 plan       the chief coordinator cuts the video into overlapping sections along natural seams
  2 discover   12 lenses x every section (section scouts) + 5 full-video reviewers, all independent;
               findings are sealed until every assignment of the phase has finished
  3 consensus  the consensus coordinator opens the findings, clusters them, counts support and dissent;
               the duplicate resolver keeps the strongest version; the ranking coordinator shortlists
  4 gates      16 gate reviewers judge every shortlisted clip independently
  5 verify     targeted verification passes on each finalist (10-100, stopping once settled)
  6 decide     the ranking coordinator scores 15 categories, sets confidence, approves / holds / rejects
  7 coverage   the coverage auditor proves every second was reviewed
Everything is recorded in the audit trail.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from ..events import Event
from ..media import Cancelled, fmt_ts
from . import consensus as cons
from . import mappers
from .evidence import Evidence
from .gates import GATES
from .lenses import FULL_REVIEWERS, LENSES
from .registry import CREW, Job
from .verify import FIXED, PROBES, budget, settled

MAPPERS = {"scenes": mappers.map_scenes, "sound": mappers.map_voices, "topics": mappers.map_topics,
           "structure": mappers.map_structure, "entities": mappers.map_names}
TECH_FLAWS = {"black", "dropout", "clipping", "loop", "no_words"}


@dataclass
class JobCtx:
    ev: Evidence
    rep: object = None
    watch: Callable | None = None
    already_made: Callable | None = None
    peers: list = field(default_factory=list)   # (id, text) of clips already chosen elsewhere (other videos, past runs)
    sections: list = field(default_factory=list)
    job: Job | None = None


# ---------------------------------------------------------------- executors (what each kind of agent does)
@CREW.executor("map")
def _map(a, ctx: JobCtx, agent: str):
    return MAPPERS[a.role](ctx.ev)


@CREW.executor("plan")
def _plan(a, ctx: JobCtx, agent: str):
    return mappers.plan_sections(ctx.ev)


@CREW.executor("scout")
def _scout(a, ctx: JobCtx, agent: str):
    from .lenses import scout_section

    return scout_section(ctx.ev, a.role, a.payload["section"])


@CREW.executor("full")
def _full(a, ctx: JobCtx, agent: str):
    return FULL_REVIEWERS[a.role](ctx.ev)


@CREW.executor("consensus")
def _consensus(a, ctx: JobCtx, agent: str):
    reg, job = CREW.registry, ctx.job
    opened = reg.open_sealed(job, agent, a.payload["scouts"] + a.payload["full"], "discover")
    subs = [(job.assignments[i].agent, opened[i]) for i in a.payload["scouts"] if i in opened]
    table, scores = cons.build_tables(subs)
    noms = []
    for i in a.payload["scouts"]:
        if i in opened:
            for n in opened[i]["nominations"]:
                noms.append({**n, "lens": opened[i]["lens"], "agent": job.assignments[i].agent})
    for i in a.payload["full"]:
        if i in opened:
            for n in opened[i].get("nominations", []):
                noms.append({**n, "lens": job.assignments[i].role, "agent": job.assignments[i].agent})
    return {"candidates": cons.consensus(ctx.ev, table, scores, noms, list(FULL_REVIEWERS)), "nominations": len(noms)}


@CREW.executor("dedupe")
def _dedupe(a, ctx: JobCtx, agent: str):
    kept, rejected = cons.dedupe(a.payload["candidates"], ctx.already_made)
    return {"kept": kept, "rejected": rejected}


@CREW.executor("shortlist")
def _shortlist(a, ctx: JobCtx, agent: str):
    p = a.payload
    ok = [c for c in p["kept"] if c["support"] >= p["min_support"] or c["minority"]]
    short = ok[: p["n"]]
    out = [{**c, "reason": f"only {c['support']} of {len(cons.LENS_NAMES)} lenses support it"}
           for c in p["kept"] if c not in ok]
    out += [{**c, "reason": "below the shortlist cut (stronger candidates took the review slots)"} for c in ok[p["n"]:]]
    return {"shortlist": short, "rejected": [{k: c[k] for k in ("id", "start", "end", "strength", "support", "reason")}
                                             for c in out]}


@CREW.executor("gate")
def _gate(a, ctx: JobCtx, agent: str):
    return GATES[a.role][1](ctx.ev, a.payload["cand"])


@CREW.executor("probe")
def _probe(a, ctx: JobCtx, agent: str):
    return PROBES[a.payload["probe"]](ctx.ev, a.payload["cand"], a.payload["k"], a.payload.get("peers"))


@CREW.executor("decide")
def _decide(a, ctx: JobCtx, agent: str):
    return [cons.decide(ctx.ev, c, c["gates"], c["vsum"], a.payload["cfg"]) for c in a.payload["cands"]]


@CREW.executor("coverage")
def _coverage(a, ctx: JobCtx, agent: str):
    p = a.payload
    return mappers.coverage(ctx.ev, ctx.sections, {k: set(v) for k, v in p["done"].items()},
                            list(cons.LENS_NAMES), set(p["full_done"]), list(FULL_REVIEWERS))


# ---------------------------------------------------------------- the driver
class VideoReview:
    def __init__(self, ctx: JobCtx, crew_cfg: dict, cap: int) -> None:
        self.ctx, self.cfg, self.cap = ctx, crew_cfg, cap
        self.ev = ctx.ev
        self.reg = CREW.registry
        self.phases: list[dict] = []
        self.me = "chief-1"

    def _emit(self, phase: str, msg: str, frac: float | None = None, **data) -> None:
        if self.ctx.rep:
            self.ctx.rep.emit(Event("crew", "analysis", msg, data={"phase": phase, "video": self.ev.meta.get("title", ""),
                                                                    **data}))
        if self.ctx.watch:
            self.ctx.watch("judge", frac, note=msg)

    def _wait(self, ids: list[str], phase: str, label: str, lo: float, hi: float) -> None:
        last = [0.0]

        def prog(done, total):
            now = time.time()
            if now - last[0] > 0.8:
                last[0] = now
                self._emit(phase, f"{label}: {done}/{total} assignments done", lo + (hi - lo) * done / max(1, total),
                           done=done, total=total)
        self.reg.wait(self.job, ids, on_progress=prog)

    def _phase(self, name: str, ids: list[str], t0: float) -> None:
        a = [self.job.assignments[i] for i in ids]
        self.phases.append({"phase": name, "seconds": round(time.time() - t0, 2), "assignments": len(a),
                            "done": sum(x.status == "done" for x in a), "failed": sum(x.status == "failed" for x in a),
                            "retried": sum(x.attempts > 1 for x in a), "agents": len({x.agent for x in a if x.agent}),
                            "helpers": sum(x.helper for x in a)})

    def _open(self, ids: list[str], phase: str) -> dict:
        self.reg.close_phase(self.job, phase)
        return self.reg.open_sealed(self.job, self.me, ids, phase)

    def run(self, job_id: str) -> dict:
        CREW.start()
        ev, reg = self.ev, self.reg
        self.job = job = reg.open_job(job_id, self.ctx)
        self.ctx.job = job
        started = time.time()
        try:
            return self._run(job, started)
        finally:
            reg.close_job(job_id)

    def _run(self, job: Job, started: float) -> dict:
        ev, reg = self.ev, self.reg
        span = (0.0, ev.duration)
        # ---- 0: map the whole video
        t0 = time.time()
        maps = {r: reg.add(job, r, "map", "video", f"Mapping {MAPPERS[r].__doc__.split('.')[0].lower()}", {}, span=span,
                           timeout=max(300.0, ev.duration / 5)) for r in MAPPERS}
        self._emit("map", "Full-video mappers: scenes, voices, topics, structure and names")
        self._wait([a.id for a in maps.values()], "map", "Mapping the whole video", 0.0, 0.12)
        self._phase("map", [a.id for a in maps.values()], t0)
        map_results = self._open([a.id for a in maps.values()], "map")
        map_summary = {r: map_results.get(a.id, {"failed": a.history[-1].get("error", "")[:200] if a.history else ""})
                       for r, a in maps.items()}
        # ---- 1: sections
        t0 = time.time()
        plan = reg.add(job, "chief", "plan", "video", "Cutting the video into overlapping sections", {}, span=span)
        self._wait([plan.id], "plan", "Planning sections", 0.12, 0.14)
        self._phase("plan", [plan.id], t0)
        sections = self._open([plan.id], "plan").get(plan.id)
        if not sections:
            raise RuntimeError("the chief coordinator could not plan sections")
        self.ctx.sections = sections
        reg.note(job, self.me, f"{len(sections)} sections planned", sections=[s["id"] for s in sections])
        self._emit("plan", f"{len(sections)} sections x {len(LENSES)} lenses = {len(sections) * len(LENSES)} section "
                           f"reviews, plus {len(FULL_REVIEWERS)} full-video reviews", 0.14)
        # ---- 2: independent discovery
        t0 = time.time()
        scouts = {}
        for s in sections:
            for role, (lens, *_r) in LENSES.items():
                a = reg.add(job, role, "scout", f"section:{s['id']}", f"{lens.replace('_', ' ').title()} lens on "
                            f"{s['id']} ({fmt_ts(s['core'][0])}-{fmt_ts(s['core'][1])})", {"section": s},
                            key=("scout", role, s["id"]), span=tuple(s["read"]), timeout=180)
                scouts[a.id] = (s["id"], lens)
        full = {reg.add(job, r, "full", "video", f"Reviewing the whole video: {r}", {}, span=span,
                        timeout=max(300.0, ev.duration / 10)).id: r for r in FULL_REVIEWERS}
        ids = list(scouts) + list(full)
        self._wait(ids, "discover", "Independent reviews", 0.14, 0.6)
        self._phase("discover", ids, t0)
        # ---- 3: consensus, duplicates, shortlist
        t0 = time.time()
        reg.close_phase(job, "discover")
        ca = reg.add(job, "consensus", "consensus", "video", "Opening sealed findings: consensus and dissent",
                     {"scouts": list(scouts), "full": list(full)})
        self._wait([ca.id], "consensus", "Consensus", 0.6, 0.63)
        found = self._open([ca.id], "consensus").get(ca.id) or {"candidates": [], "nominations": 0}
        da = reg.add(job, "dedupe", "dedupe", "video", "Merging near-duplicate moments", {"candidates": found["candidates"]})
        self._wait([da.id], "consensus", "Duplicates", 0.63, 0.64)
        dd = self._open([da.id], "consensus").get(da.id) or {"kept": [], "rejected": []}
        n_short = self.cfg.get("shortlist", "auto")
        n_short = int(n_short) if str(n_short).isdigit() else int(np.clip(3 * self.cap, 10, 60))
        sa = reg.add(job, "ranker", "shortlist", "video", "Shortlisting for the gate reviewers",
                     {"kept": dd["kept"], "min_support": int(self.cfg.get("min_support", 3)), "n": n_short})
        self._wait([sa.id], "consensus", "Shortlist", 0.64, 0.65)
        sl = self._open([sa.id], "consensus").get(sa.id) or {"shortlist": [], "rejected": []}
        short = sl["shortlist"]
        rejected = dd["rejected"] + sl["rejected"]
        self._phase("consensus", [ca.id, da.id, sa.id], t0)
        reg.note(job, "consensus-1", f"{found['nominations']} nominations -> {len(found['candidates'])} moments")
        reg.note(job, "dedupe-1", f"{len(dd['kept'])} distinct moments, {len(dd['rejected'])} already made")
        reg.note(job, "ranker-1", f"{len(short)} shortlisted for gate review")
        self._emit("consensus", f"{found['nominations']} nominations -> {len(found['candidates'])} moments -> "
                                f"{len(short)} shortlisted", 0.65)
        # ---- 4: gates
        t0 = time.time()
        gates = {}
        for c in short:
            reg.cap(job, f"cand:{c['id']}", 6)
            cand = {k: c[k] for k in ("id", "i", "j", "start", "end")}
            for role, (name, _) in GATES.items():
                a = reg.add(job, role, "gate", f"cand:{c['id']}", f"{name.replace('_', ' ').title()} check of {c['id']} "
                            f"({fmt_ts(c['start'])})", {"cand": cand}, key=("gate", role, c["id"]),
                            span=(c["start"], c["end"]), timeout=120)
                gates[a.id] = (c["id"], name)
        self._wait(list(gates), "gates", "Gate reviews", 0.65, 0.78)
        self._phase("gates", list(gates), t0)
        opened = self._open(list(gates), "gates")
        by_c = {c["id"]: c for c in short}
        for aid, (cid, name) in gates.items():
            r = opened.get(aid)
            by_c[cid].setdefault("gates", {})[name] = (
                {**r, "agent": job.assignments[aid].agent} if r else
                {"score": 50.0, "issues": [{"sev": "major", "code": "gate_failed",
                                            "detail": f"{name} review failed after retries - a person should check it"}],
                 "agent": None})
        for c in short:
            c["adjustments"] = cons.adjust(ev, c, c["gates"])
            c["t0"], c["t1"] = cons.final_times(ev, c)
        # ---- 5: verification
        t0 = time.time()
        probe_ids = self._verify(job, short)
        self._phase("verify", probe_ids, t0)
        # ---- 6: decide
        t0 = time.time()
        payload = [{k: c[k] for k in ("id", "i", "j", "t0", "t1", "support", "dissenters", "missing", "minority",
                                      "gates", "vsum")} for c in short]
        dec = reg.add(job, "ranker", "decide", "video", f"Scoring and deciding {len(short)} finalists",
                      {"cands": payload, "cfg": self.cfg})
        self._wait([dec.id], "decide", "Decisions", 0.95, 0.97)
        decisions = self._open([dec.id], "decide").get(dec.id) or []
        for c, d in zip(short, decisions):
            c["decision"] = d
            c["why"] = cons.why_it_works(c, d)
        self._same_moment_twice(short)
        final = sorted((c for c in short if c.get("decision")), key=lambda c: -c["decision"]["rank_score"])
        self._phase("decide", [dec.id], t0)
        # ---- 7: coverage
        t0 = time.time()
        done: dict[str, list] = {}
        for aid, (sid, lens) in scouts.items():
            if job.assignments[aid].status == "done":
                done.setdefault(sid, []).append(lens)
        full_done = [r for aid, r in full.items() if job.assignments[aid].status == "done"]
        cov = reg.add(job, "coverage", "coverage", "video", "Proving every second was reviewed",
                      {"done": done, "full_done": full_done})
        self._wait([cov.id], "coverage", "Coverage", 0.97, 0.99)
        report = self._open([cov.id], "coverage").get(cov.id) or {"complete": False, "summary": "coverage audit failed"}
        self._phase("coverage", [cov.id], t0)
        approved = [c for c in final if c["decision"]["status"] == "approved"]
        review = [c for c in final if c["decision"]["status"] == "review"]
        self._emit("done", f"{len(approved)} approved, {len(review)} held for your review, "
                           f"{len(final) - len(approved) - len(review) + len(rejected)} rejected - {report['summary']}", 1.0)
        return {"approved": approved, "review": review, "final": final, "rejected": rejected, "coverage": report,
                "maps": map_summary, "sections": self.ctx.sections, "phases": self.phases,
                "assignments": [a.record() for a in job.assignments.values()], "log": job.log,
                "seconds": round(time.time() - started, 2), "agents_used": len({a.agent for a in job.assignments.values()
                                                                                  if a.agent})}

    # ---------------------------------------------------------------- verification rounds
    def _verify(self, job: Job, short: list[dict]) -> list[str]:
        ev, reg = self.ev, self.reg
        strengths = [c["strength"] for c in short]
        max_passes = int(self.cfg.get("max_verification_passes", 100))
        retrans = str(self.cfg.get("retranscribe", "auto")).lower()
        all_ids: list[str] = []
        state = {}
        for rank, c in enumerate(short):
            value, risk, unc = cons.value_of(c, strengths), cons.risk_of(c["gates"]), cons.uncertainty_of(c)
            deep = value >= 0.9 or risk >= 0.6
            reg.cap(job, f"cand:{c['id']}", 6 if deep else 3)  # one clip never takes the whole team
            b = min(max_passes, budget(value, risk, unc))
            cand = {k: c[k] for k in ("id", "i", "j")} | {"start": c["t0"], "end": c["t1"]}
            exclude = {s["agent"] for s in c["supporters"] if s.get("agent")}  # no one verifies their own find
            fixed = dict(FIXED)
            if retrans == "off" or (retrans == "auto" and rank >= max(3, self.cap)):
                fixed.pop("v_transcript")
            if ev.video is None:
                fixed.pop("v_frames")
            ids = []
            for role, n in fixed.items():
                for k in range(n):
                    ids.append(reg.add(job, role, "probe", f"cand:{c['id']}", f"{PROBES[role].__name__[2:]} pass {k + 1} "
                                       f"on {c['id']}", {"probe": role, "cand": cand, "k": k,
                                                         "peers": self.ctx.peers if role == "v_duplicates" else None},
                                       key=("probe", role, c["id"], k), span=(c["t0"], c["t1"]), exclude=exclude,
                                       timeout=240 if role == "v_transcript" else 60).id)
            state[c["id"]] = {"c": c, "budget": b, "ids": ids, "stab": [], "cand": cand, "exclude": exclude, "k": 0,
                              "deep": deep, "value": value, "risk": risk, "uncertainty": unc}
            all_ids += ids
        self._emit("verify", f"Verifying {len(short)} finalists: {sum(s['budget'] for s in state.values())} targeted "
                             "passes budgeted", 0.78)
        self._wait(all_ids, "verify", "Verification passes", 0.78, 0.86)
        # stability passes, in rounds, until each clip's score settles or its budget is used
        rounds = 0
        while True:
            batch = []
            for cid, s in state.items():
                used = len(s["ids"])
                if used >= s["budget"] or settled(s["stab"]):
                    continue
                for _ in range(min(8, s["budget"] - used)):
                    k = s["k"]
                    s["k"] += 1
                    a = reg.add(job, "v_stability", "probe", f"cand:{cid}", f"stability pass {k + 1} on {cid}",
                                {"probe": "v_stability", "cand": s["cand"], "k": k},
                                key=("probe", "v_stability", cid, k), span=(s["cand"]["start"], s["cand"]["end"]),
                                exclude=s["exclude"], timeout=60)
                    s["ids"].append(a.id)
                    batch.append(a.id)
            if not batch:
                break
            rounds += 1
            self._wait(batch, "verify", f"Stability round {rounds}", 0.86, 0.95)
            all_ids += batch
            self.reg.close_phase(job, "verify")
            for cid, s in state.items():
                stab_ids = [i for i in s["ids"] if job.assignments[i].payload["probe"] == "v_stability"]
                res = self.reg.open_sealed(job, self.me, stab_ids, "verify")
                s["stab"] = [res[i]["value"] for i in stab_ids if i in res]
        opened = self._open(all_ids, "verify")
        for cid, s in state.items():
            passes = []
            for i in s["ids"]:
                a = job.assignments[i]
                r = opened.get(i)
                passes.append({"probe": a.payload["probe"][2:], "pass": a.payload["k"] + 1, "agent": a.agent,
                               "status": a.status, **({k: v for k, v in r.items() if k != "alt"} if r else {})})
            s["c"]["verification"] = {"budget": s["budget"], "deep_review": s["deep"], "value": round(s["value"], 2),
                                      "risk": s["risk"], "uncertainty": round(s["uncertainty"], 2), "passes": passes}
            s["c"]["vsum"] = self._summarize(s["c"], passes)
        return all_ids

    def _summarize(self, c: dict, passes: list[dict]) -> dict:
        done = [p for p in passes if p["status"] == "done" and "skip" not in p]
        by = {}
        for p in done:
            by.setdefault(p["probe"], []).append(p)
        mean = lambda xs: float(np.mean(xs)) if xs else None  # noqa: E731
        stab = [p["value"] for p in by.get("stability", [])]
        hw = 1.96 * float(np.std(stab, ddof=1)) / math.sqrt(len(stab)) if len(stab) >= 2 else None
        oks = [bool(p["ok"]) for p in done if "ok" in p]
        dup = by.get("duplicates", [{}])[0] if by.get("duplicates") else {}
        v = {"passes": len(done), "hook_robust": mean([p["value"] for p in by.get("hook", [])]),
             "boundary_ok": mean([float(p["ok"]) for p in by.get("boundary", [])]),
             "context_ok": mean([float(p["ok"]) for p in by.get("context", [])]),
             "stability_mean": mean(stab), "stability_hw": hw,
             "face_rate": mean([float(p.get("face", False)) for p in by.get("frames", [])]),
             "transcript_agreement": mean([p["value"] for p in by.get("transcript", [])]),
             "checks_ok": mean([float(x) for x in oks]),
             "uniqueness": dup.get("value"), "duplicate_of": dup.get("similar_to") if dup.get("ok") is False else "",
             "flags": sorted({f for p in by.get("claims", []) for f in p.get("flags", [])})}
        v = {k: x for k, x in v.items() if x is not None}
        if c["minority"]:
            v["minority_confirmed"] = bool(stab) and (v["stability_mean"] - (hw or 0)) >= 0.55 and \
                v.get("checks_ok", 0) >= 0.8
        return v

    def _same_moment_twice(self, short: list[dict]) -> None:
        """Two finalists telling the same thing in different words: the weaker one is rejected."""
        from .verify import shingles

        ranked = sorted((c for c in short if c.get("decision") and c["decision"]["status"] != "rejected"),
                        key=lambda c: -c["decision"]["rank_score"])
        kept = []
        for c in ranked:
            mine = shingles(self.ev.span_text(c["i"], c["j"]))
            twin = next((k for k in kept if mine and (len(mine & k[1]) / len(mine | k[1])) >= 0.5), None)
            if twin:
                c["decision"]["status"] = "rejected"
                c["decision"]["rejected_because"].append(f"says the same as {twin[0]['id']}")
                twin[0]["alternatives"].append({"id": c["id"], "start": c["t0"], "end": c["t1"],
                                                "reason": "same content, weaker"})
            else:
                kept.append((c, mine))


def write_audit(result: dict, meta: dict, path: Path) -> Path:
    """The complete audit trail for one video, as JSON."""
    def brief(c):
        return {k: c.get(k) for k in ("id", "i", "j", "start", "end", "t0", "t1", "strength", "quality", "support",
                                       "lens_support", "minority", "supporters", "dissenters", "abstained", "missing",
                                       "notes", "alternatives", "adjustments", "gates", "verification", "decision", "why")}
    doc = {"video": {k: meta.get(k) for k in ("id", "title", "channel", "duration", "webpage_url")},
           "seconds": result["seconds"], "agents_used": result["agents_used"], "phases": result["phases"],
           "maps": result["maps"], "sections": result["sections"], "coverage": result["coverage"],
           "finalists": [brief(c) for c in result["final"]], "rejected": result["rejected"],
           "coordinator_log": result["log"], "assignments": result["assignments"]}
    path.write_text(json.dumps(doc, indent=1, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o)),
                    encoding="utf-8")
    return path


__all__ = ["JobCtx", "VideoReview", "write_audit", "Cancelled"]
