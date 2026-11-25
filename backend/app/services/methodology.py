from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Any

import yaml

METHODOLOGY_VERSION = "v2026.11.24"

METHODOLOGY_REGISTRY: dict[str, dict[str, Any]] = {
    "version": {"value": METHODOLOGY_VERSION, "updated_at": "2026-11-24"},
    "orthogonal_dimensions": {
        "title": "4 维正交属性（run_scope / effective_coverage / scoring_protocol / official_status）",
        "anchor": "#orthogonal-dimensions",
        "summary": "run_scope 仅按严格全量题集匹配或显式别名判定，绝不按 80% 比例或挂载官方分推定 full_split；有效 0 分计入有效均分，故障与未评分留白不填 0。",
    },
    "comparative_macro_avg": {
        "title": "Comparative Macro Avg（共同可比基准等权均值）",
        "anchor": "#comparative-macro-avg",
        "summary": "顶行 Comparative Macro Avg 仅在所有参与对比列共同有分且各自基准内评分协议一致的基准集上等权计算；次行 Self-Coverage Avg 仅供非可比参考，不标 Best、不算 Δ。",
    },
    "intersection_denominators": {
        "title": "严格交集三类分母与未舍入双精度计算",
        "anchor": "#intersection-denominators",
        "summary": "默认主口径为共同有效评分集（common_valid_scored_tasks）；启用故障按零协议时在共同覆盖集（common_covered_tasks）上按各自真实通过数计分（不把共同有效通过数直接除共同覆盖分母）；各自总样本均分独立展示。所有 delta_pp 与 rel_pct 使用未舍入 float64 计算，仅展示层保留 2 位小数。",
    },
    "pass_at_k": {
        "title": "pass@k、pass^k 与 95% 置信区间",
        "anchor": "#pass-at-k",
        "summary": "单题有效评分尝试数 n_t < k 时 pass@k 与 pass^k 强制记 null（缺测 —），批次聚合明示可算题数分母（computable_tasks / total_tasks）；单尝试采用 95% Wilson CI，多尝试均值采用 1000 次固定种子非参 Bootstrap 95% CI。",
    },
    "token_semantics": {
        "title": "Token 包含语义先验校验",
        "anchor": "#token-semantics",
        "summary": "双键提取 reasoning_tokens / reasoning_output_tokens；仅对已验证 cached <= input 且 reasoning <= output 且来源在 registry 中 verified=true 的记录标为 mutually_exclusive_verified 渲染互斥 4 段条，否则标 unverified_components 并排展示。",
    },
    "recorded_cost": {
        "title": "已记录成本 vs 实付成本",
        "anchor": "#recorded-cost",
        "summary": "Harbor 原生 cost_usd 未经账单对账统一标为 recorded_native（已记录成本），仅对账通过后标 billed_reconciled（实付），价目表推算强制标 estimated（估算）。",
    },
}


def finite(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val)


def normalize_score_to_100(raw_score: float, scale_type: str) -> float:
    if scale_type == "ratio_0_1":
        return raw_score * 100.0
    if scale_type == "score_0_1000":
        return raw_score / 10.0
    return float(raw_score)


def compute_unrounded_delta(baseline_pct: float, candidate_pct: float) -> tuple[float, float | None]:
    """Compute (delta_pp, rel_pct) strictly from unrounded float64 values, rounding only at display (v2.2.1 03-2 §3.2)."""
    delta_pp = round(candidate_pct - baseline_pct, 2)
    if abs(baseline_pct) < 1e-12:
        rel_pct = None
    else:
        rel_pct = round(((candidate_pct - baseline_pct) / baseline_pct) * 100.0, 2)
    return delta_pp, rel_pct


def pass_at_k(n_scored: int, c_passed: int, k: int) -> float | None:
    """Unbiased pass@k estimator; returns None when k > n_scored (A-07)."""
    if k <= 0 or n_scored < k or c_passed < 0 or c_passed > n_scored:
        return None
    if n_scored - c_passed < k:
        return 1.0
    return 1.0 - (math.comb(n_scored - c_passed, k) / math.comb(n_scored, k))


def pass_hat_k(n_scored: int, c_passed: int, k: int) -> float | None:
    """Stability pass^k = C(c, k) / C(n, k); returns None when k > n_scored."""
    if k <= 0 or n_scored < k or c_passed < 0 or c_passed > n_scored:
        return None
    if c_passed < k:
        return 0.0
    return math.comb(c_passed, k) / math.comb(n_scored, k)


def wilson_ci95(passed: int, total: int, z: float = 1.959963984540054) -> list[float] | None:
    if total <= 0:
        return None
    p = passed / total
    denom = 1.0 + (z * z) / total
    center = (p + (z * z) / (2.0 * total)) / denom
    half = (z * math.sqrt((p * (1.0 - p) + (z * z) / (4.0 * total)) / total)) / denom
    return [round(max(0.0, center - half), 4), round(min(1.0, center + half), 4)]


def bootstrap_ci95(values: list[float], *, n_resamples: int = 1000, seed: int = 20261124) -> list[float] | None:
    if not values:
        return None
    if len(values) == 1:
        v = round(values[0], 4)
        return [v, v]
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(n_resamples):
        s = math.fsum(values[rng.randrange(n)] for _ in range(n))
        means.append(s / n)
    means.sort()
    lo = means[int(0.025 * n_resamples)]
    hi = means[min(n_resamples - 1, int(0.975 * n_resamples))]
    return [round(lo, 4), round(hi, 4)]


def compute_comparative_macro_avg(
    subject_scores: dict[str, dict[str, tuple[float | None, str | None]]],
) -> dict[str, Any]:
    """Compute Comparative Macro Avg (only over common comparable benchmarks) vs Self-Coverage Avg (A-05).
    `subject_scores[subject_id][bench_key] = (norm_score_100_or_None, scoring_protocol_or_None)`."""
    subjects = list(subject_scores.keys())
    all_benches = sorted({b for m in subject_scores.values() for b in m.keys()})
    common_benches: list[str] = []
    for b in all_benches:
        vals = [subject_scores[s].get(b, (None, None)) for s in subjects]
        if all(v[0] is not None and v[1] is not None for v in vals):
            protocols = {v[1] for v in vals}
            if len(protocols) == 1:
                common_benches.append(b)

    by_subject: dict[str, dict[str, Any]] = {}
    raw_comp_avgs: dict[str, float] = {}
    for s in subjects:
        if common_benches:
            avg_c = math.fsum(subject_scores[s][b][0] for b in common_benches) / len(common_benches)  # type: ignore[misc]
            raw_comp_avgs[s] = avg_c
        else:
            avg_c = None

        self_vals = [v[0] for v in subject_scores[s].values() if v[0] is not None]
        self_avg = (math.fsum(self_vals) / len(self_vals)) if self_vals else None
        by_subject[s] = {
            "comparative_macro_avg": round(avg_c, 2) if avg_c is not None else None,
            "comparative_macro_avg_raw": avg_c,
            "comparative_bench_count": len(common_benches),
            "is_best": False,
            "self_coverage_avg": round(self_avg, 2) if self_avg is not None else None,
            "self_bench_count": len(self_vals),
        }

    if raw_comp_avgs:
        best_val = max(raw_comp_avgs.values())
        for s, val in raw_comp_avgs.items():
            if abs(val - best_val) < 1e-9:
                by_subject[s]["is_best"] = True

    return {
        "common_comparable_benchmarks": common_benches,
        "by_subject": by_subject,
    }


class TokenSemanticsValidator:
    def __init__(self, registry_file: Path) -> None:
        self.rules: list[dict[str, Any]] = []
        if registry_file.is_file():
            cfg = yaml.safe_load(registry_file.read_text(encoding="utf-8")) or {}
            self.rules = cfg.get("sources", [])

    def classify(
        self,
        *,
        runner: str | None,
        harness: str | None,
        harness_version: str | None,
        input_tokens: int | None,
        cached_tokens: int | None,
        output_tokens: int | None,
        reasoning_tokens: int | None,
    ) -> str:
        if input_tokens is None and output_tokens is None and cached_tokens is None and reasoning_tokens is None:
            return "none"
        # Check numerical containment assertions first
        if cached_tokens is not None and (input_tokens is None or cached_tokens < 0 or cached_tokens > input_tokens):
            return "unverified_components"
        if reasoning_tokens is not None and (output_tokens is None or reasoning_tokens < 0 or reasoning_tokens > output_tokens):
            return "unverified_components"
        # Require BOTH components present and source verified in registry to draw stacked 4-seg bar
        if input_tokens is None or output_tokens is None or cached_tokens is None or reasoning_tokens is None:
            return "unverified_components"
        for r in self.rules:
            if r.get("runner") not in ("*", runner):
                continue
            if r.get("harness") not in ("*", harness):
                continue
            if r.get("harness_version") not in ("*", harness_version):
                continue
            if bool(r.get("verified")):
                return "mutually_exclusive_verified"
        return "unverified_components"
