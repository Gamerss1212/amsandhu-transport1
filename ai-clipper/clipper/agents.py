"""The agent team: 150 specialised agents that work at the same time.

Every agent has one role and is separately accountable: the board knows what each agent is doing right
now, how many jobs it finished, how many failed, and when it last showed a sign of life. Agents of the
same role share a job queue (see `crew.registry`), so whoever finishes first takes the next job: no
capacity sits idle while work is waiting, and no two agents ever get the same assignment.

Divisions
  command       6  plan the work, dispatch it, compare findings, resolve duplicates, rank, audit coverage
  intake       10  study trends, find videos, download them, transcribe every word
  full video   10  map the whole video (scenes, voices, topics, structure, names) and review it end to end
  section      60  12 specialist lenses x 5 scouts; each scout reads its own sections, independently
  gate         32  16 quality / integrity / risk reviewers x 2, each judging shortlisted clips on its own
  verify       10  targeted re-checks of promising clips only (boundaries, hook, context, stability...)
  production   16  edit, make platform versions, write hooks / titles / captions / hashtags, inspect exports
  publishing    6  audience, timing, posting order, compliance gate, publisher, shared memory
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from contextlib import contextmanager

# (role, division, display name, how many agents, what they do)
ROLES = [
    # ---- command
    ("chief", "command", "Chief coordinator", 1, "Splits every video into overlapping sections and owns the assignment registry"),
    ("dispatch", "command", "Dispatcher", 1, "Hands the next job to whoever finishes first; retries failed or stalled work"),
    ("consensus", "command", "Consensus coordinator", 1, "Opens sealed findings only after every reviewer submitted; counts support and dissent"),
    ("dedupe", "command", "Duplicate resolver", 1, "Merges overlapping candidates and keeps the strongest version"),
    ("ranker", "command", "Ranking coordinator", 1, "Scores every category, sets confidence, approves or sends to human review"),
    ("coverage", "command", "Coverage auditor", 1, "Proves every second of the video was reviewed, and names any gap"),
    # ---- intake
    ("tt_scan", "intake", "TikTok scanner", 2, "Reads TikTok accounts live: views, likes, shares and saves on every recent video, and follows @mentions to new accounts"),
    ("ig_scan", "intake", "Instagram scanner", 1, "Reads Instagram Reels live from creators and clip pages, pacing itself so Instagram keeps answering"),
    ("trend", "intake", "Trend analyst", 1, "Scans YouTube Shorts, re-checks hot videos minutes apart to measure views per minute, and learns what is going viral"),
    ("creators", "intake", "Creator scout", 1, "Ranks the people whose clips go viral and checks who has fresh long videos on YouTube to clip"),
    ("scout", "intake", "Video scout", 2, "Searches for long-form videos worth clipping, best-ranked creators first"),
    ("download", "intake", "Downloader", 2, "Fetches videos (or just the audio of very long ones)"),
    ("listen", "intake", "Listener", 2, "Transcribes every word with word-level timing"),
    # ---- full video: mappers (their maps are the shared evidence) ...
    ("scenes", "full", "Scene mapper", 1, "Scans the whole picture at high speed for camera cuts and scene changes"),
    ("sound", "full", "Voice & sound mapper", 1, "Maps speaker turns, loudness, laughter and reactions across the whole video"),
    ("topics", "full", "Topic mapper", 1, "Finds where the conversation changes subject"),
    ("structure", "full", "Structure mapper", 1, "Finds chapters, show breaks, intros, outros and sponsor reads"),
    ("entities", "full", "Name & reference librarian", 1, "Indexes who and what is named where, so clips never lose their referents"),
    # ... and full-video reviewers (independent nominations from the whole-video view)
    ("narrative", "full", "Narrative-arc analyst", 1, "Follows the story of the whole video and nominates its climaxes"),
    ("callbacks", "full", "Callback tracker", 1, "Finds running jokes and callbacks that only make sense with earlier context"),
    ("audience", "full", "Audience-signal analyst", 1, "Reads most-replayed data and timestamped comments"),
    ("continuity", "full", "Continuity analyst", 1, "Finds questions answered minutes later and stories that span sections"),
    ("gems", "full", "Quiet-gem hunter", 1, "Finds subtle, low-volume moments that loudness-driven reviewers miss"),
    # ---- section scouts: 12 lenses, each reads its sections independently
    ("s_hook", "section", "Hook scout", 5, "Judges the first seconds: would they stop a scroll?"),
    ("s_retention", "section", "Retention scout", 5, "Judges pacing, dead air and open loops that keep people watching"),
    ("s_humor", "section", "Humor scout", 5, "Finds laughs, punchlines and comic timing"),
    ("s_emotion", "section", "Emotion scout", 5, "Finds vulnerability, joy, anger and fear"),
    ("s_story", "section", "Story scout", 5, "Finds complete stories: setup, turn, payoff"),
    ("s_education", "section", "Education scout", 5, "Finds clear, useful explanations and advice"),
    ("s_insight", "section", "Insight scout", 5, "Finds surprising, counter-intuitive ideas"),
    ("s_controversy", "section", "Debate scout", 5, "Finds strong opinions and confrontations people will argue about"),
    ("s_quotes", "section", "Quote scout", 5, "Finds lines people will quote and share"),
    ("s_authentic", "section", "Authenticity scout", 5, "Rewards real, unscripted moments; rejects ad reads and plugs"),
    ("s_relevance", "section", "Audience-fit scout", 5, "Matches moments to what audiences want right now"),
    ("s_energy", "section", "Reaction & energy scout", 5, "Finds reactions, applause and energy peaks in the audio"),
    # ---- gate reviewers: each judges shortlisted clips on its own
    ("g_visual", "gate", "Composition reviewer", 2, "Checks framing, faces in shot, exposure, black or text-heavy frames"),
    ("g_camera", "gate", "Camera-change reviewer", 2, "Checks cuts and shot changes inside and at the edges of a clip"),
    ("g_faces", "gate", "Expression reviewer", 2, "Checks that faces are visible and animated"),
    ("g_audio", "gate", "Audio-quality reviewer", 2, "Checks level, noise, clipping and dropouts in the source audio"),
    ("g_music", "gate", "Music reviewer", 2, "Detects music beds and songs under or between the speech"),
    ("g_captions", "gate", "Caption reviewer", 2, "Checks captions are readable at their speed and length"),
    ("g_subtitles", "gate", "Subtitle-accuracy reviewer", 2, "Checks the transcript for timing errors and recognition loops"),
    ("g_boundary", "gate", "Boundary reviewer", 2, "Checks no sentence, reaction or joke is cut off at either end"),
    ("g_context", "gate", "Context reviewer", 2, "Checks the clip is not misleading without what came before or after"),
    ("g_claims", "gate", "Claims reviewer", 2, "Lists factual claims and flags ones that need checking"),
    ("g_misinfo", "gate", "Misinformation reviewer", 2, "Flags known misinformation patterns and absolute health or money claims"),
    ("g_privacy", "gate", "Privacy reviewer", 2, "Flags phone numbers, emails, addresses and other personal data"),
    ("g_brand", "gate", "Brand-safety reviewer", 2, "Scores profanity, sexual content, drugs, violence and hate"),
    ("g_copyright", "gate", "Copyright reviewer", 2, "Flags music, broadcasts and third-party content that needs permission"),
    ("g_policy", "gate", "Platform-policy reviewer", 2, "Checks TikTok, Instagram and YouTube community rules"),
    ("g_reputation", "gate", "Reputation reviewer", 2, "Flags accusations, sarcasm cut short and statements unfair out of context"),
    # ---- verification: targeted re-checks of promising clips only
    ("v_boundary", "verify", "Boundary verifier", 1, "Re-checks start and end word by word, with alternatives"),
    ("v_hook", "verify", "Hook verifier", 1, "Re-scores the opening with alternative first words"),
    ("v_context", "verify", "Context verifier", 1, "Looks further back each pass to confirm nothing essential is missing"),
    ("v_stability", "verify", "Stability verifier", 2, "Re-analyzes the clip under small changes until its score is certain"),
    ("v_frames", "verify", "Frame verifier", 1, "Samples new frames each pass: black, frozen, nobody in shot"),
    ("v_audio", "verify", "Audio verifier", 1, "Re-measures random stretches of the clip's audio"),
    ("v_transcript", "verify", "Transcript verifier", 1, "Re-listens to the clip and compares it with the transcript"),
    ("v_claims", "verify", "Claims & policy verifier", 1, "Re-checks claims, hedges and policy on the final boundaries"),
    ("v_duplicates", "verify", "Duplicate verifier", 1, "Compares the clip with every other finalist and past clips"),
    # ---- production
    ("editor", "production", "Editor", 3, "Cuts, reframes, captions and mixes the master edit"),
    ("tiktok", "production", "TikTok packager", 1, "Makes and checks the TikTok version"),
    ("reels", "production", "Reels packager", 1, "Makes and checks the Instagram Reels version"),
    ("shorts", "production", "Shorts packager", 1, "Makes and checks the YouTube Shorts version"),
    ("director", "production", "Style director", 1, "Chooses the edit and caption style each clip needs"),
    ("subtitles", "production", "Subtitle writer", 1, "Writes SRT and VTT files with speaker labels"),
    ("hookwriter", "production", "Hook writer", 1, "Writes several on-screen hook options, faithful to the words"),
    ("titles", "production", "Title writer", 1, "Writes title and description options"),
    ("captions", "production", "Caption writer", 1, "Writes post captions for each platform"),
    ("hashtags", "production", "Hashtag strategist", 1, "Builds broad, niche and mixed hashtag sets"),
    ("cta", "production", "Call-to-action writer", 1, "Writes honest calls to action"),
    ("thumbs", "production", "Thumbnail designer", 1, "Picks several cover frames"),
    ("inspector", "production", "Export inspector", 1, "Watches every finished file and fixes problems"),
    # ---- publishing
    ("strategist", "publishing", "Audience strategist", 1, "Recommends the best platform and audience for each clip"),
    ("timing", "publishing", "Timing analyst", 1, "Learns the best time to post from your own results"),
    ("order", "publishing", "Posting-order planner", 1, "Plans which clip goes out first, and where"),
    ("compliance", "publishing", "Compliance officer", 1, "Lets a clip auto-post only when every check passed"),
    ("publish", "publishing", "Publisher", 1, "Posts approved clips to TikTok and Instagram"),
    ("brain", "publishing", "Brain curator", 1, "Keeps the shared memory: what worked, what you removed, no repeats"),
]
TEAM_SIZE = sum(n for _, _, _, n, _ in ROLES)
DIVISIONS = [("command", "Command"), ("intake", "Intake"), ("full", "Full-video analysts"),
             ("section", "Section scouts"), ("gate", "Gate reviewers"), ("verify", "Verification"),
             ("production", "Production"), ("publishing", "Publishing")]
ALIASES = {"hook": "hookwriter", "review": "inspector", "audio": "sound", "judge": "consensus"}


def parallel_videos(cfg) -> int:
    """How many videos are watched at the same time: listening is the heavy part, so it scales with
    the machine (an NVIDIA GPU or many cores lets several listeners work at once)."""
    n = cfg["analysis"].get("parallel_videos", "auto")
    if n in (None, "auto"):
        cores = os.cpu_count() or 4
        n = 1 if cores < 8 else 2 if cores < 16 else 3
    return max(1, min(3, int(n)))


class AgentBoard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.agents: list[dict] = []
        for role, division, name, n, about in ROLES:
            for i in range(n):
                self.agents.append({"id": f"{role}-{i + 1}", "role": role, "division": division,
                                    "name": f"{name} {i + 1}", "about": about})
        self._by_id = {a["id"]: a for a in self.agents}
        self._roles = {role: [a["id"] for a in self.agents if a["role"] == role] for role, *_ in ROLES}
        self._busy: dict[str, tuple[str, float]] = {}   # agent id -> (task, since)
        self._done = {a["id"]: 0 for a in self.agents}
        self._failed = {a["id"]: 0 for a in self.agents}
        self._alive: dict[str, float] = {}               # agent id -> last own heartbeat (crew agents)
        self._duty: dict[str, tuple[str, float, str | None]] = {}  # role -> (standing duty, heartbeat, reason)
        self._last: dict[str, tuple[str, float]] = {}    # agent id -> (latest finding, when)
        self._hist: dict[str, deque] = {a["id"]: deque(maxlen=8) for a in self.agents}
        self._feed: deque = deque(maxlen=400)            # every agent's findings, newest last
        self._seq = 0

    def report(self, agent_id: str | None, text: str, kind: str = "done") -> None:
        """What an agent just concluded, in one sentence: shown on its card and in the live feed."""
        if not agent_id or agent_id not in self._by_id:
            return
        with self._lock:
            now = time.time()
            self._seq += 1
            self._last[agent_id] = (text[:220], now)
            self._hist[agent_id].append({"t": now, "text": text[:220], "kind": kind})
            a = self._by_id[agent_id]
            self._feed.append({"seq": self._seq, "t": now, "id": agent_id, "name": a["name"],
                               "division": a["division"], "text": text[:220], "kind": kind})

    def feed(self, after: int = 0) -> list[dict]:
        with self._lock:
            return [e for e in self._feed if e["seq"] > after][-120:]

    def history(self, agent_id: str) -> list[dict]:
        with self._lock:
            return list(self._hist.get(agent_id, ()))

    def role_of(self, role: str) -> str:
        return ALIASES.get(role, role)

    def beat(self, role: str, duty: str, reason: str | None = None) -> None:
        """A role's heartbeat while it is not busy: what it is watching for (or why it is on standby)."""
        with self._lock:
            self._duty[self.role_of(role)] = (duty, time.time(), reason)

    def alive(self, agent_id: str) -> None:
        """An individual agent's own heartbeat (the crew's worker threads send these)."""
        with self._lock:
            self._alive[agent_id] = time.time()

    def start(self, agent_id: str, task: str) -> None:
        with self._lock:
            self._busy[agent_id] = (task[:160], time.time())
            self._alive[agent_id] = time.time()

    def finish(self, agent_id: str, ok: bool = True) -> None:
        with self._lock:
            self._busy.pop(agent_id, None)
            self._alive[agent_id] = time.time()
            (self._done if ok else self._failed)[agent_id] += 1

    @contextmanager
    def work(self, role: str, task: str):
        """Run a piece of work as one of this role's agents (it shows as busy on the board)."""
        role = self.role_of(role)
        with self._lock:
            ids = self._roles[role]
            agent = next((a for a in ids if a not in self._busy), None)
            if agent is not None:
                self._busy[agent] = (task[:160], time.time())
        ok, t0 = False, time.time()
        try:
            yield agent
            ok = True
        finally:
            with self._lock:
                if agent is not None:
                    self._busy.pop(agent, None)
                    self._alive[agent] = time.time()
                    (self._done if ok else self._failed)[agent] += 1
                    fresh = self._last.get(agent, ("", 0.0))[1] >= t0
            if agent is not None and not fresh:
                self.report(agent, (f"Done in {time.time() - t0:.1f}s: " if ok else "Could not finish: ") + task,
                            "done" if ok else "fail")

    def snapshot(self) -> list[dict]:
        """Every agent's live state: working (on a job), watching (alive, on its standing duty),
        standby (alive but waiting on something it names), or offline (no heartbeat - and why)."""
        now = time.time()
        with self._lock:
            out = []
            for a in self.agents:
                aid, role = a["id"], a["role"]
                duty, beat, reason = self._duty.get(role, ("", 0.0, None))
                own = self._alive.get(aid, 0.0)
                beat = max(beat, own)
                busy = self._busy.get(aid)
                mates = [self._busy[x][0] for x in self._roles[role] if x in self._busy]
                if busy:
                    state, task = "working", busy[0]
                elif mates and (now - beat > 120 or not duty):
                    state, task = "watching", "Standing by to help - " + mates[0][:1].lower() + mates[0][1:]
                elif now - beat > 120:
                    state = "offline"
                    task = reason or ("Starting up..." if not beat else
                                      f"No heartbeat for {int((now - beat) // 60)} min - restarting")
                elif reason:
                    state, task = "standby", reason
                else:
                    state, task = "watching", duty or "Waiting for the next job"
                out.append({**a, "state": state, "busy": bool(busy), "task": task,
                            "for": round(now - busy[1]) if busy else 0, "done": self._done[aid],
                            "failed": self._failed[aid], "beat": round(now - beat) if beat else None,
                            "last": self._last.get(aid, ("", 0))[0],
                            "last_ago": round(now - self._last[aid][1]) if aid in self._last else None})
            return out

    def busy(self) -> int:
        with self._lock:
            return len(self._busy)

    def ids(self, role: str) -> list[str]:
        return list(self._roles[self.role_of(role)])

    def get(self, agent_id: str) -> dict:
        return self._by_id[agent_id]


BOARD = AgentBoard()  # one team per app, shared by every part of it
