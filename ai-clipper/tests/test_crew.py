"""The 150-agent crew: registry guarantees, sections and coverage, independent review, gates, packaging."""
import json
import subprocess
import threading
import time

import numpy as np
import pytest

from clipper.analysis.transcribe import group_segments
from clipper.crew import consensus as cons
from clipper.crew import gates, mappers
from clipper.crew.evidence import Evidence
from clipper.crew.package import faithful, publish_check
from clipper.crew.registry import Crew, Registry
from clipper.media import ffmpeg_exe

from .test_free_mode import DULL, STRONG, STRONG_B, TEXT
from .test_moments import make_words


def evidence(cfg, text, **meta):
    cfg["analysis"].update(min_clip_seconds=15, max_clip_seconds=60)
    w = make_words(text)
    t = {"words": w, "segments": group_segments(w), "source": "test"}
    return Evidence({"duration": w[-1]["e"] + 1, **meta}, t, {"pct": {}, "raw": {}}, None, cfg, None, None)


# ---------------------------------------------------------------- registry
def test_registry_never_duplicates_and_keeps_findings_sealed():
    reg = Registry()
    job = reg.open_job("j", None)
    a = reg.add(job, "s_hook", "scout", "section:S01", "hook S01", {})
    assert reg.add(job, "s_hook", "scout", "section:S01", "again", {}) is a  # same key: same assignment
    got = reg.take("s_hook-1", "s_hook", wait=0.1)
    assert got is a and reg.take("s_hook-2", "s_hook", wait=0.1) is None  # nobody else gets it
    reg.complete(a, a.token, {"secret": 1})
    with pytest.raises(PermissionError):  # a scout cannot read another agent's findings
        reg.open_sealed(job, "s_humor-1", [a.id], "discover")
    with pytest.raises(PermissionError):  # nor can a coordinator before the phase is closed
        reg.open_sealed(job, "consensus-1", [a.id], "discover")
    reg.close_phase(job, "discover")
    assert reg.open_sealed(job, "consensus-1", [a.id], "discover") == {a.id: {"secret": 1}}


def test_failed_and_stalled_work_is_retried_by_someone_else():
    reg = Registry()
    job = reg.open_job("j", None)
    a = reg.add(job, "g_privacy", "gate", "cand:C1", "privacy", {}, timeout=0.05)
    first = reg.take("g_privacy-1", "g_privacy", wait=0.1)
    reg.fail(first, first.token, "boom")
    assert reg.take("g_privacy-1", "g_privacy", wait=0.1) is None  # the retry goes to the other reviewer
    second = reg.take("g_privacy-2", "g_privacy", wait=0.1)
    assert second is a and a.attempts == 2
    time.sleep(0.1)
    assert reg.watchdog() == [a]  # stalled: taken back
    late = a.token - 1
    assert reg.complete(a, late, "stale answer") is False  # an old attempt answering late is ignored
    third = reg.take("g_privacy-1", "g_privacy", wait=0.1) or reg.take("g_privacy-2", "g_privacy", wait=0.1)
    reg.fail(third, third.token, "boom again")
    assert a.status == "failed" and len(a.history) == 3  # gives up after 3 attempts, and says why


def test_one_clip_never_takes_the_whole_team_and_finders_do_not_verify_their_own_find():
    reg = Registry()
    job = reg.open_job("j", None)
    reg.cap(job, "cand:C1", 2)
    ids = [reg.add(job, "v_stability", "probe", "cand:C1", f"p{k}", {}, key=("p", k), exclude={"s_hook-1"}).id
           for k in range(5)]
    reg.add(job, "v_stability", "probe", "cand:C2", "other clip", {}, key=("q", 0))
    taken = [reg.take(f"v_stability-{k}", "v_stability", wait=0.05) for k in (1, 2)]
    assert all(t.scope == "cand:C1" for t in taken)
    nxt = reg.take("v_hook-1", "v_hook", helps=("v_stability",), wait=0.05)  # capped: the helper takes C2
    assert nxt.scope == "cand:C2" and nxt.helper
    reg.complete(taken[0], taken[0].token, {})  # a C1 slot frees up...
    assert reg.take("s_hook-1", "s_hook", helps=("v_stability",), wait=0.05) is None  # ...not for the scout who found C1
    assert reg.take("v_stability-1", "v_stability", wait=0.05).scope == "cand:C1"
    assert len(ids) == 5


def test_crew_agents_are_live_threads_and_are_restarted():
    crew = Crew()
    crew.start()
    assert len(crew.threads) == 6 + 10 + 60 + 32 + 10  # command + full + section + gate + verify
    victim = crew.threads["s_hook-1"]
    crew.threads["s_hook-1"] = threading.Thread(target=lambda: None)
    crew.threads["s_hook-1"].start()
    crew.threads["s_hook-1"].join()
    assert crew._revive() == 1 and crew.threads["s_hook-1"].is_alive() and victim.is_alive()


# ---------------------------------------------------------------- sections and coverage
def test_sections_tile_the_video_and_every_clip_fits_one(cfg):
    ev = evidence(cfg, DULL * 6 + STRONG + DULL * 6)
    mappers.map_topics(ev)
    sections = mappers.plan_sections(ev)
    assert len(sections) >= 3
    assert sections[0]["core"][0] == 0 and sections[-1]["core"][1] == pytest.approx(ev.duration)
    for a, b in zip(sections, sections[1:]):
        assert a["core"][1] == b["core"][0]  # no gap, no overlap in ownership
        assert a["read"][1] - a["core"][1] >= ev.max_s  # reading overlap longer than any clip
    full = {s["id"]: set(cons.LENS_NAMES) for s in sections}
    cov = mappers.coverage(ev, sections, full, cons.LENS_NAMES, {"narrative"}, ["narrative"])
    assert cov["complete"] and not cov["gaps"] and cov["words_reviewed"] == f"{len(ev.words)}/{len(ev.words)}"
    full[sections[1]["id"]].discard("humor")  # a lens that never reported is a named gap, not hidden
    cov = mappers.coverage(ev, sections, full, cons.LENS_NAMES, {"narrative"}, ["narrative"])
    assert not cov["complete"] and cov["unreviewed"][0]["missing"] == ["humor"]


def test_every_window_is_owned_by_exactly_one_section(cfg):
    ev = evidence(cfg, TEXT)
    sections = mappers.plan_sections(ev)
    owned = [tuple(x) for s in sections for x in zip(*ev.grid(s["i0"], s["i1"]))]
    assert len(owned) == len(set(owned)) == len(list(zip(*ev.grid(0, ev.n))))


# ---------------------------------------------------------------- the whole crew on a video
def _video(tmp_path, text):
    w = make_words(text)
    d = w[-1]["e"] + 2
    v = tmp_path / "source.mp4"
    subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=320x180:r=15:d={d}",
                    "-f", "lavfi", "-i", f"sine=f=200:d={d}", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", str(v)], check=True)
    return v, {"words": w, "segments": group_segments(w), "source": "test"}, d


def test_crew_reviews_every_second_and_approves_only_the_strong_moment(cfg, tmp_path):
    from clipper.crew import review_video

    cfg["analysis"].update(min_clip_seconds=15, max_clip_seconds=60)
    cfg["crew"]["retranscribe"] = "off"
    v, t, d = _video(tmp_path, DULL + STRONG_B + DULL + STRONG + DULL)
    clips, finalists, res = review_video({"id": "vid", "title": "Pod", "channel": "Pod", "duration": d}, t,
                                         {"pct": {}, "raw": {}}, None, cfg, v, None)
    titles = [c.title for c in clips]
    assert any(x.startswith("Nobody talks about") for x in titles)
    assert any(x.startswith("Nobody believed") or "twenty thousand dollars" in x for x in titles)
    assert not [c for c in finalists if c.status == "approved" and "meeting room" in c.summary]  # filler never passes
    for c in clips:
        rec = c.crew
        assert c.status == "approved" and rec["support"] >= 3 and 0 < rec["confidence"] <= 1
        assert 30 <= rec["verification"]["passes"] <= 100
        assert set(rec["scores"]) >= {"hook", "retention", "emotional_impact", "clarity", "originality", "usefulness",
                                      "entertainment", "shareability", "replay_value", "production_quality",
                                      "contextual_completeness", "audience_fit", "platform_suitability", "safety",
                                      "overall_confidence"}
        # a scout that nominated the clip never verified it
        supporters = {s["agent"] for s in rec["supporters"]}
        audit = json.loads((v.parent / "crew_audit.json").read_text())
        mine = next(f for f in audit["finalists"] if f["id"] == rec["id"])
        assert not supporters & {p["agent"] for p in mine["verification"]["passes"]}
    assert res["coverage"]["complete"]
    kinds = {a["kind"] for a in res["assignments"]}
    assert kinds >= {"map", "plan", "scout", "full", "consensus", "dedupe", "shortlist", "gate", "probe", "decide",
                     "coverage"}
    assert res["agents_used"] >= 60
    scouts = [a for a in res["assignments"] if a["kind"] == "scout"]
    assert len(scouts) == 12 * len(res["sections"])  # every section got every lens exactly once


# ---------------------------------------------------------------- gates
def _cand(ev, first, last=None):
    i = next(k for k, s in enumerate(ev.text) if s.startswith(first))
    j = next(k for k, s in enumerate(ev.text) if last and s.startswith(last)) if last else min(ev.n - 1, i + 4)
    return {"id": "C1", "i": i, "j": j, "start": ev.S[i], "end": ev.E[j]}


def test_privacy_misinformation_and_sarcasm_gates(cfg):
    ev = evidence(cfg, DULL + "Call me any time. My number is 555-123-4567 so write it down. That is the plan for today. "
                  "Okay great. " + DULL)
    c = _cand(ev, "Call me")
    assert any(x["sev"] == "block" for x in gates.g_privacy(ev, c)["issues"])
    ev = evidence(cfg, DULL + "They don't want you to know this. Vaccines cause autism and the elites hide it. "
                  "Wake up people. It is all true. " + DULL)
    c = _cand(ev, "They don't want")
    assert gates.g_misinfo(ev, c)["issues"][0]["sev"] == "block"
    ev = evidence(cfg, DULL + "Honestly I think my brother is the worst person alive. He ruined every holiday we had. "
                  "Nobody in my family can stand him at all. I mean it. Just kidding, I love him. " + DULL)
    c = _cand(ev, "Honestly I think", "I mean it")
    rep = gates.g_reputation(ev, c)
    assert rep["proposal"].get("j") == c["j"] + 1  # the "just kidding" is pulled into the clip
    assert gates.g_context(ev, c)["proposal"].get("j") == c["j"] + 1


def test_boundary_and_context_gates_propose_fixes(cfg):
    ev = evidence(cfg, DULL + "What was the hardest year of your life? Twenty nineteen was the hardest year of my life. "
                  "I lost my job and my house in the same month. I slept in my car for six weeks. "
                  "That taught me everything about money. " + DULL)
    c = _cand(ev, "Twenty nineteen", "That taught me")
    ctx = gates.g_context(ev, c)
    assert ctx["proposal"]["i"] == c["i"] - 1  # the question is added so the answer makes sense
    notes = cons.adjust(ev, dict(c), {"context": ctx})
    assert notes and "start moved" in notes[0]


# ---------------------------------------------------------------- consensus
def test_consensus_counts_support_dissent_and_minority_reports(cfg):
    ev = evidence(cfg, TEXT)
    I, J = ev.grid(0, ev.n)
    rng = np.random.default_rng(1)
    table = {n: {(int(i), int(j)): (0.2, 0, f"{n}-1") for i, j in zip(I, J)} for n in cons.LENS_NAMES}
    scores = {n: list(rng.uniform(0, 0.5, len(I))) for n in cons.LENS_NAMES}
    w1, w2 = (int(I[3]), int(J[3])), (int(I[-4]), int(J[-4]))
    for n in ("hook", "retention", "story", "quotes"):
        table[n][w1] = (0.8, 1, f"{n}-2")
    table["authenticity"][w1] = (0.1, -1, "authenticity-3")
    table["humor"][w2] = (0.95, 1, "humor-4")  # one lens, exceptional: a minority report
    noms = [{"i": w1[0], "j": w1[1], "start": ev.S[w1[0]], "end": ev.E[w1[1]], "score": 0.8, "why": "strong",
             "lens": "hook", "agent": "s_hook-2"},
            {"i": w2[0], "j": w2[1], "start": ev.S[w2[0]], "end": ev.E[w2[1]], "score": 0.95, "why": "funny",
             "lens": "humor", "agent": "s_humor-4"}]
    out = {(c["i"], c["j"]): c for c in cons.consensus(ev, table, scores, noms, ["narrative"])}
    a, b = out[w1], out[w2]
    assert a["support"] == 4 and [d["by"] for d in a["dissenters"]] == ["authenticity"] and not a["minority"]
    assert b["support"] == 1 and b["minority"]


def test_dedupe_keeps_the_strongest_version():
    a = {"id": "C1", "start": 10, "end": 50, "strength": 0.9, "support": 5, "dissenters": [], "alternatives": []}
    b = {"id": "C2", "start": 15, "end": 55, "strength": 0.7, "support": 3, "dissenters": [], "alternatives": []}
    c = {"id": "C3", "start": 100, "end": 140, "strength": 0.6, "support": 3, "dissenters": [], "alternatives": []}
    kept, rejected = cons.dedupe([a, b, c], already_made=lambda s, e: s >= 100)
    assert [k["id"] for k in kept] == ["C1"] and a["alternatives"][0]["id"] == "C2"
    assert rejected[0]["id"] == "C3" and "already made" in rejected[0]["reason"]


# ---------------------------------------------------------------- packaging and publishing
def test_metadata_must_use_the_speakers_own_words():
    said = "Nobody talks about the day I lost everything. I was broke and homeless."
    assert faithful("Nobody talks about the day I lost everything", said)
    assert not faithful("Billionaire reveals secret crypto trick doctors hate", said)


def test_compliance_officer_keeps_drafts_unless_everything_passed(cfg):
    good = {"name": "clip_01", "review": {"ok": True},
            "package": {"status": "approved", "source": {"channel": "My Pod"},
                        "versions": {"tiktok": {"ok": True}},
                        "crew": {"status": "approved", "confidence": 0.9, "risks": [], "third_party": True}}}
    ok, why = publish_check(good, cfg)
    assert not ok and "automatic posting is off" in why[0]  # drafts by default
    cfg["posting"]["auto_schedule"] = True
    ok, why = publish_check(good, cfg)
    assert not ok and "licensed_channels" in why[0]  # someone else's content needs permission first
    cfg["posting"]["licensed_channels"] = ["My Pod"]
    assert publish_check(good, cfg) == (True, [])
    risky = json.loads(json.dumps(good))
    risky["package"]["crew"].update(status="review", review_because=["claims: unverified health claim"],
                                    risks=[{"detail": "unverified health claim"}])
    ok, why = publish_check(risky, cfg)
    assert not ok and any("human review" in w for w in why)
