"""Learn what makes short-form videos go viral from 1000+ real examples.

Output is a JSON-serialisable "viral profile":
  * a virality score for every video (views, velocity, reach beyond followers,
    engagement, shares/saves) ranked within its platform,
  * lift tables (how much more likely a trait is among viral videos) for hook
    styles, duration buckets, hashtags and posting hours,
  * a text model (TF-IDF + logistic regression) whose term weights are reused to
    score candidate clips, plus its cross-validated AUC so you can see how
    predictable virality currently is.
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter

import numpy as np

HOOK_PATTERNS: dict[str, str] = {
    "question": r"\?|^\s*(why|how|what|who|when|would|should|did|do|is|are|can)\b",
    "pov": r"\bpov\b",
    "number_list": r"\b\d+\s+(things|ways|reasons|tips|signs|mistakes|secrets|rules|habits)\b",
    "watch_till_end": r"wait for it|till the end|until the end|watch till|stay till|the ending",
    "story": r"story ?time|\bwhen i\b|\bthe day\b|\bi was\b|\bthis happened\b",
    "controversial": r"unpopular opinion|nobody talks about|hot take|controvers|truth about|"
                     r"stop doing|you('re| are) wrong|\blies?\b|exposed",
    "shock": r"insane|crazy|shocking|unbelievable|can'?t believe|\bwild\b|mind ?blow|no way",
    "howto": r"how to|tutorial|\bhack\b|\btips?\b|secret|learn|guide",
    "emotional": r"\bcry|tears|emotional|heartbreak|\bsad\b|\blost\b|passed away|miss (him|her|you)",
    "funny": r"funny|\blol\b|lmao|😂|🤣|hilarious|i'?m dead|\bjoke",
    "money": r"money|\$\d|million|billion|\brich\b|\bbroke\b|salary|business|invest",
    "reaction": r"react|reaction|responds|roast|destroy|shut (him|her|them)? ?down|claps back",
}
DURATION_BINS = [(0, 10), (10, 20), (20, 30), (30, 45), (45, 60), (60, 90), (90, 180), (180, 10_000)]


def pct_rank(x: np.ndarray) -> np.ndarray:
    """Percentile rank in [0, 1], ties averaged."""
    if len(x) < 2:
        return np.full(len(x), 0.5)
    order = x.argsort(kind="mergesort")
    ranks = np.empty(len(x))
    ranks[order] = np.arange(len(x))
    # average ties
    _, inv, counts = np.unique(x, return_inverse=True, return_counts=True)
    sums = np.bincount(inv, weights=ranks)
    ranks = sums[inv] / counts[inv]
    return ranks / (len(x) - 1)


def score_videos(videos: list[dict], viral_top: float, flop_bottom: float,
                 now: float | None = None) -> list[dict]:
    """Adds `virality` (0-100) and `label` ('viral' | 'flop' | 'mid') to each video."""
    now = now or time.time()
    by_platform: dict[str, list[dict]] = {}
    for v in videos:
        by_platform.setdefault(v["platform"], []).append(v)

    for group in by_platform.values():
        views = np.array([max(v["views"], 1.0) for v in group])
        age_h = np.array([max(1.0, (now - v["created_at"]) / 3600) if v.get("created_at") else np.nan
                          for v in group])
        age_h = np.where(np.isnan(age_h), np.nanmedian(age_h) if np.isfinite(age_h).any() else 72.0, age_h)
        followers = np.array([v.get("author_followers") or np.nan for v in group], dtype=float)
        reach = np.log10(views / np.maximum(followers, 1000.0))
        reach = np.where(np.isnan(reach), np.nanmedian(reach) if np.isfinite(reach).any() else 0.0, reach)
        interactions = np.array([v["likes"] + v["comments"] + v["shares"] + v["saves"] for v in group])
        shares = np.array([v["shares"] + v["saves"] for v in group])

        composite = (
            0.25 * pct_rank(np.log10(views))
            + 0.20 * pct_rank(np.log10(views / age_h))
            + 0.25 * pct_rank(reach)
            + 0.15 * pct_rank(interactions / views)
            + 0.15 * pct_rank(shares / views)
        )
        final = pct_rank(composite)
        for v, c, pr in zip(group, composite, final):
            v["virality"] = round(float(c) * 100, 2)
            v["label"] = "viral" if pr >= 1 - viral_top else ("flop" if pr < flop_bottom else "mid")
    return videos


def hooks_in(text: str) -> list[str]:
    low = text.lower()
    return [name for name, pat in HOOK_PATTERNS.items() if re.search(pat, low)]


def duration_bin(seconds: float) -> str:
    for lo, hi in DURATION_BINS:
        if lo <= seconds < hi:
            return f"{lo}-{hi}s" if hi < 10_000 else f"{lo}s+"
    return "unknown"


def _lift_table(flags: dict[str, np.ndarray], viral: np.ndarray, min_support: int) -> list[dict]:
    base = viral.mean() if len(viral) else 0.0
    rows = []
    for name, mask in flags.items():
        support = int(mask.sum())
        if support < min_support or base == 0:
            continue
        rate = viral[mask].mean()
        rows.append({"feature": name, "lift": round(float(rate / base), 3), "support": support,
                     "viral_rate": round(float(rate), 3)})
    return sorted(rows, key=lambda r: -r["lift"])


def _text_of(v: dict) -> str:
    return f"{v['caption']} {' '.join('#' + h for h in v['hashtags'])}"


def _dense_features(videos: list[dict]) -> np.ndarray:
    rows = []
    for v in videos:
        hooks = set(hooks_in(v["caption"]))
        created = v.get("created_at")
        hour = time.gmtime(created).tm_hour if created else 12
        rows.append([
            math.log1p(v.get("duration") or 0),
            len(v["caption"]),
            len(v["hashtags"]),
            math.sin(2 * math.pi * hour / 24),
            math.cos(2 * math.pi * hour / 24),
            *[1.0 if h in hooks else 0.0 for h in HOOK_PATTERNS],
        ])
    return np.array(rows, dtype=float)


def _text_model(videos: list[dict], viral: np.ndarray) -> dict:
    """TF-IDF + logistic regression. Returns term weights and cross-validated AUC."""
    try:
        from scipy.sparse import csr_matrix, hstack
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import cross_val_score
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        return {"auc": None, "terms": {}, "top_viral_terms": [], "top_flop_terms": []}

    if viral.sum() < 10 or (~viral).sum() < 10:
        return {"auc": None, "terms": {}, "top_viral_terms": [], "top_flop_terms": []}

    texts = [_text_of(v) for v in videos]
    vec = TfidfVectorizer(ngram_range=(1, 2), min_df=3, max_features=6000,
                          stop_words="english", token_pattern=r"(?u)#?\b\w\w+\b")
    try:
        x_text = vec.fit_transform(texts)
    except ValueError:  # vocabulary empty
        return {"auc": None, "terms": {}, "top_viral_terms": [], "top_flop_terms": []}
    y = viral.astype(int)
    folds = int(min(5, y.sum(), (1 - y).sum()))

    # predictive power of everything together (words + format features)
    x_dense = StandardScaler().fit_transform(_dense_features(videos))
    x_all = hstack([x_text, csr_matrix(x_dense)]).tocsr()
    auc = None
    if folds >= 3:
        full = LogisticRegression(C=1.0, class_weight="balanced", max_iter=2000)
        auc = float(np.mean(cross_val_score(full, x_all, y, cv=folds, scoring="roc_auc")))

    # words alone, so each term gets full credit for its effect (used to score clips)
    model = LogisticRegression(C=1.0, class_weight="balanced", max_iter=2000)
    model.fit(x_text, y)
    coefs = model.coef_[0]
    vocab = np.array(vec.get_feature_names_out())
    order = coefs.argsort()
    top_pos = [(vocab[i], float(coefs[i])) for i in order[::-1][:250] if coefs[i] > 0]
    top_neg = [(vocab[i], float(coefs[i])) for i in order[:250] if coefs[i] < 0]
    return {
        "auc": round(auc, 3) if auc is not None else None,
        "terms": {t: round(w, 4) for t, w in top_pos + top_neg},
        "top_viral_terms": [t for t, _ in top_pos[:40]],
        "top_flop_terms": [t for t, _ in top_neg[:25]],
    }


def build_profile(videos: list[dict], viral_top: float = 0.2, flop_bottom: float = 0.4) -> dict:
    videos = score_videos(videos, viral_top, flop_bottom)
    n = len(videos)
    viral = np.array([v["label"] == "viral" for v in videos])
    min_support = max(8, n // 100)

    hook_flags = {h: np.array([h in hooks_in(v["caption"]) for v in videos]) for h in HOOK_PATTERNS}
    bins = [duration_bin(v.get("duration") or 0) for v in videos]
    has_durations = any(v.get("duration") for v in videos)
    dur_flags = {b: np.array([x == b for x in bins]) for b in sorted(set(bins))} if has_durations else {}
    tag_counts = Counter(t for v in videos for t in v["hashtags"])
    tag_flags = {"#" + t: np.array([t in v["hashtags"] for v in videos])
                 for t, c in tag_counts.most_common(300) if c >= min_support}
    hours = [time.gmtime(v["created_at"]).tm_hour if v.get("created_at") else None for v in videos]
    hour_flags = {f"{h:02d}:00 UTC": np.array([x == h for x in hours]) for h in range(24)}

    viral_durations = np.array([v["duration"] for v in videos if v["label"] == "viral" and v.get("duration")])
    if len(viral_durations):
        q25, q50, q75 = np.percentile(viral_durations, [25, 50, 75])
    else:
        q25 = q50 = q75 = 0.0

    platforms = {}
    for p in sorted({v["platform"] for v in videos}):
        group = [v for v in videos if v["platform"] == p]
        platforms[p] = {
            "videos": len(group),
            "viral": sum(v["label"] == "viral" for v in group),
            "median_views": float(np.median([v["views"] for v in group])),
            "median_viral_views": float(np.median([v["views"] for v in group if v["label"] == "viral"]
                                                  or [0])),
        }

    ranked = sorted(videos, key=lambda v: -v["virality"])
    model = _text_model(videos, viral)
    return {
        "created_at": time.time(),
        "n_videos": n,
        "n_viral": int(viral.sum()),
        "platforms": platforms,
        "hook_lift": _lift_table(hook_flags, viral, min_support),
        "duration_lift": _lift_table(dur_flags, viral, min_support),
        "hashtag_lift": _lift_table(tag_flags, viral, min_support)[:40],
        "hour_lift": _lift_table(hour_flags, viral, min_support)[:6],
        "viral_duration": {"p25": float(q25), "median": float(q50), "p75": float(q75)},
        "model_auc": model["auc"],
        "term_weights": model["terms"],
        "top_viral_terms": model["top_viral_terms"],
        "top_flop_terms": model["top_flop_terms"],
        "viral_examples": [{"caption": v["caption"][:220], "views": v["views"], "platform": v["platform"],
                            "duration": v.get("duration")} for v in ranked[:30]],
        "flop_examples": [{"caption": v["caption"][:220], "views": v["views"], "platform": v["platform"]}
                          for v in ranked[-12:]],
    }


def trend_fit(profile: dict | None, text: str, duration: float) -> float:
    """0-100: how well a clip (its transcript/title + length) matches what is going viral now."""
    if not profile:
        return 50.0
    low = text.lower()
    tokens = re.findall(r"#?\b\w\w+\b", low)
    grams = set(tokens) | {f"{a} {b}" for a, b in zip(tokens, tokens[1:])}
    weights = profile.get("term_weights", {})
    term_score = sum(w for t, w in weights.items() if t in grams)
    term_part = 1 / (1 + math.exp(-term_score))

    lifts = {r["feature"]: r["lift"] for r in profile.get("hook_lift", [])}
    hooks = [lifts[h] for h in hooks_in(text) if h in lifts]
    hook_part = 1 / (1 + math.exp(-sum(math.log2(max(l, 1e-3)) for l in hooks))) if hooks else 0.5

    dur_lifts = {r["feature"]: r["lift"] for r in profile.get("duration_lift", [])}
    best = max(dur_lifts.values(), default=1.0)
    dur_part = min(1.0, dur_lifts.get(duration_bin(duration), 1.0) / best) if best > 0 else 0.5

    return round(100 * (0.5 * term_part + 0.25 * hook_part + 0.25 * dur_part), 1)


def playbook_text(profile: dict) -> str:
    """Compact, human-readable summary that is fed to the AI when it picks clips."""
    lines = [f"Analyzed {profile['n_videos']} short videos ({profile['n_viral']} labelled viral)."]
    if profile.get("model_auc"):
        lines.append(f"Caption/format model predictive power (AUC): {profile['model_auc']}.")
    d = profile.get("viral_duration", {})
    if d.get("median"):
        lines.append(f"Viral video length: median {d['median']:.0f}s (middle half {d['p25']:.0f}-{d['p75']:.0f}s).")
    for title, key in (("Hook styles", "hook_lift"), ("Durations", "duration_lift")):
        rows = profile.get(key, [])[:6]
        if rows:
            lines.append(f"{title} by viral lift: " + ", ".join(f"{r['feature']} x{r['lift']}" for r in rows))
    if profile.get("top_viral_terms"):
        lines.append("Words/topics linked to viral videos: " + ", ".join(profile["top_viral_terms"][:30]))
    if profile.get("top_flop_terms"):
        lines.append("Words/topics linked to flops: " + ", ".join(profile["top_flop_terms"][:15]))
    if profile.get("hashtag_lift"):
        lines.append("Best hashtags: " + ", ".join(r["feature"] for r in profile["hashtag_lift"][:15]))
    live = profile.get("live") or {}
    if live.get("viral_now"):
        lines.append("Gaining views fastest right now (live scan):")
        lines += [f"  - ({r['per_min']:,.0f} views/min, {r['platform']}) {r['caption']}" for r in live["viral_now"][:10]]
    if live.get("creators"):
        lines.append("People whose clips go viral now: " + ", ".join(c["name"] for c in live["creators"][:12]))
    if profile.get("viral_examples"):
        lines.append("Top viral captions right now:")
        lines += [f"  - ({int(e['views']):,} views) {e['caption']}" for e in profile["viral_examples"][:15]]
    return "\n".join(lines)
