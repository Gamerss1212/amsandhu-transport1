"""quality_gate.py - the gate/auditor as a PROGRAM. Jarvus cannot edit its verdicts.

Input: a list of paper/test trades (dict with date 'YYYY-MM-DD', r_net, cost_r) for ONE role (pooled).
Computes: trade count, independent days, Deflated Sharpe Ratio (Bailey & Lopez de Prado), profit at
2x costs, walk-forward folds, and a sign-flip test.
It CANNOT compute from trade lists alone: PBO, the random-direction placebo, and buy-and-hold.
You must supply those from your bar-data tests. If you do not, the gate stays CLOSED (fail closed).

Verdicts: PAPER TEST (all checks pass) / RESEARCH FURTHER (not enough evidence yet) / REJECT.
"""
from __future__ import annotations
import math, random
from dataclasses import dataclass
from statistics import NormalDist, mean, pstdev

N = NormalDist()
EULER = 0.5772156649

def _moments(r):
    n = len(r); m = mean(r); sd = pstdev(r)
    if sd == 0: return n, m, 0.0, 0.0, 3.0
    z = [(x - m) / sd for x in r]
    skew = sum(v ** 3 for v in z) / n
    kurt = sum(v ** 4 for v in z) / n          # raw kurtosis (normal = 3)
    return n, m, sd, skew, kurt

def expected_max_sr(n_trials: int, var_sr: float) -> float:
    """Expected maximum Sharpe among n_trials unskilled tries (the bar a lucky winner would clear)."""
    if n_trials <= 1: return 0.0
    return math.sqrt(var_sr) * ((1 - EULER) * N.inv_cdf(1 - 1 / n_trials) + EULER * N.inv_cdf(1 - 1 / (n_trials * math.e)))

def deflated_sharpe(r: list, n_trials: int, trial_sr_var: float | None = None) -> float:
    """Probability that the true Sharpe is above what luck from n_trials tries would give.
    Uses per-trade Sharpe (mean/sd of net R). If you do not know the spread of Sharpes across your tries,
    it assumes the spread of an unskilled estimate, 1/(n-1)."""
    n, m, sd, skew, kurt = _moments(r)
    if n < 3 or sd == 0: return 0.0
    sr = m / sd
    v = trial_sr_var if trial_sr_var is not None else 1.0 / (n - 1)
    sr0 = expected_max_sr(n_trials, v)
    denom = 1 - skew * sr + (kurt - 1) / 4.0 * sr ** 2
    if denom <= 0: return 0.0
    return N.cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom))

def sign_flip_p(r: list, draws: int = 2000, seed: int = 7) -> float:
    """One-sided p-value that the mean R is above zero by chance (random sign flips of each trade)."""
    rng = random.Random(seed); obs = mean(r); hits = 0
    for _ in range(draws):
        if mean([x if rng.random() < 0.5 else -x for x in r]) >= obs: hits += 1
    return (hits + 1) / (draws + 1)

@dataclass
class Check:
    name: str; value: object; need: str; passed: bool | None      # None = not computed

@dataclass
class GateResult:
    verdict: str
    checks: list
    def table(self):
        rows = []
        for c in self.checks:
            s = "NOT COMPUTED" if c.passed is None else ("pass" if c.passed else "FAIL")
            rows.append(f"- {c.name}: {c.value} (need {c.need}) -> {s}")
        return f"- Verdict: {self.verdict}\n" + "\n".join(rows)

def run_gate(trades: list, n_trials: int, pbo: float | None = None, beats_buy_hold: bool | None = None,
             placebo_pct: float | None = None, folds: int = 4) -> GateResult:
    r = [t["r_net"] for t in trades]
    cost = [t.get("cost_r", 0.0) for t in trades]
    days = len({t["date"] for t in trades})
    checks = []
    c_n = len(r) >= 30
    checks.append(Check("Test trades", len(r), "30+", c_n))
    checks.append(Check("Independent trading days", days, "30+", days >= 30))
    if len(r) < 3:
        return GateResult("RESEARCH FURTHER", checks)
    dsr = deflated_sharpe(r, n_trials)
    checks.append(Check(f"Deflated Sharpe ({n_trials} tries counted)", round(dsr, 3), "0.95+", dsr >= 0.95))
    net2 = [a - b for a, b in zip(r, cost)]                     # charge costs a second time
    checks.append(Check("Mean R after 2x costs", round(mean(net2), 3), "above 0", mean(net2) > 0))
    ordered = sorted(trades, key=lambda t: t["date"])
    k = max(1, len(ordered) // folds); chunks = [ordered[j:j + k] for j in range(0, len(ordered), k)][:folds] if len(ordered) >= folds else [ordered]
    pos = sum(1 for ch in chunks if mean(t["r_net"] for t in ch) > 0)
    wf = pos / len(chunks)
    checks.append(Check("Walk-forward: share of time slices with profit", round(wf, 2), "0.5+", wf >= 0.5))
    p = sign_flip_p(r)
    checks.append(Check("Sign-flip p-value (mean R above 0)", round(p, 3), "0.05 or less", p <= 0.05))
    checks.append(Check("PBO (overfit probability)", pbo, "0.2 or less", None if pbo is None else pbo <= 0.2))
    checks.append(Check("Random-direction placebo percentile", placebo_pct, "95+", None if placebo_pct is None else placebo_pct >= 95))
    checks.append(Check("Beats buy-and-hold", beats_buy_hold, "yes", beats_buy_hold))
    failed = [c for c in checks if c.passed is False]
    missing = [c for c in checks if c.passed is None]
    if not c_n or days < 30: verdict = "RESEARCH FURTHER"
    elif failed: verdict = "REJECT"
    elif missing: verdict = "RESEARCH FURTHER"
    else: verdict = "PAPER TEST"
    return GateResult(verdict, checks)
