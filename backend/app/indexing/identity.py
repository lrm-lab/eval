from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_CHECKPOINT_RE = re.compile(r"(?:^|[_-])(?:Trained[_-])?(ck\d+)(?:$|[_-])", re.IGNORECASE)
_THINK_RE = re.compile(r"(?:^|[_-])(think|thinking|nonthinking|nothink)(?:$|[_-])", re.IGNORECASE)


def make_batch_id(source_locator: str) -> str:
    """Deterministic URL-safe stable batch_id (`b_<20hex>`), guaranteed free of '/' or ':'."""
    digest = hashlib.sha256(source_locator.encode("utf-8")).hexdigest()[:20]
    return f"b_{digest}"


def normalize_dataset_slug(raw_name: str, *, disambiguate_with: str | None = None) -> str:
    base = _NON_ALNUM_RE.sub("-", raw_name.strip().lower()).strip("-")
    if not base:
        base = "dataset"
    if disambiguate_with is not None:
        h = hashlib.sha256(disambiguate_with.encode("utf-8")).hexdigest()[:6]
        return f"{base}-{h}"
    return base


def make_collision_safe_slug(raw_name: str, slug_to_raw: dict[str, str]) -> str:
    """Deterministic slug generator that avoids silent collisions when two different
    raw dataset names normalize to the same base slug (A-11)."""
    base = normalize_dataset_slug(raw_name)
    existing = slug_to_raw.get(base)
    if existing is None or existing == raw_name:
        slug_to_raw[base] = raw_name
        return base
    suffixed = normalize_dataset_slug(raw_name, disambiguate_with=raw_name)
    slug_to_raw[suffixed] = raw_name
    return suffixed


def canonicalize_model(
    model_route: str | None,
    *,
    explicit_checkpoint: str | None = None,
    explicit_thinking: str | None = None,
    batch_name: str | None = None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Extract (model_canonical, model_provider, checkpoint, thinking_mode) from model_route / name."""
    if not model_route:
        return None, None, explicit_checkpoint, explicit_thinking

    raw = model_route.strip()
    provider: str | None = None
    core = raw
    if "/" in raw:
        parts = raw.split("/", 1)
        provider = parts[0].strip() or None
        core = parts[1].strip() or raw

    # Strip trailing harness suffix if present (e.g. Qwen3_8-27B_think_Pi / _OpenClaw)
    for harness_suf in ("_OpenClaw", "_Pi", "_pi", "_openclaw", "_opencode", "_codex"):
        if core.endswith(harness_suf):
            core = core[: -len(harness_suf)]

    checkpoint = explicit_checkpoint
    m_ck = _CHECKPOINT_RE.search(core)
    if m_ck and not checkpoint:
        checkpoint = m_ck.group(1).lower()

    thinking = explicit_thinking
    m_th = _THINK_RE.search(core)
    if m_th and not thinking:
        tok = m_th.group(1).lower()
        thinking = "off" if tok in ("nonthinking", "nothink") else "on"
    elif not thinking and batch_name:
        m_b = _THINK_RE.search(batch_name)
        if m_b:
            tok = m_b.group(1).lower()
            thinking = "off" if tok in ("nonthinking", "nothink") else "on"

    # Strip _Trained_ckXXX / _think suffixes to obtain canonical family name
    canonical = re.sub(r"_Trained_ck\d+", "", core, flags=re.IGNORECASE)
    canonical = re.sub(r"_(?:think|thinking|nonthinking|nothink)$", "", canonical, flags=re.IGNORECASE)
    canonical = canonical.strip("_- ") or core
    return canonical, provider, checkpoint, thinking


def resolve_dataset_identity(
    conn: sqlite3.Connection,
    raw_dataset_name: str | None,
    source_namespace: str,
    runner: str,
) -> tuple[str | None, str | None, str, str | None]:
    """Resolve raw dataset name against verified dataset_source_aliases (A-11).
    Returns (benchmark_slug, dataset_release_id, identity_status, inferred_run_scope).
    Unverified names remain (None, None, 'unmapped', None) so unrelated versions are never merged."""
    if not raw_dataset_name:
        return None, None, "unmapped", None

    candidates = [
        (raw_dataset_name, source_namespace, runner),
        (raw_dataset_name, source_namespace, "*"),
        (raw_dataset_name, "*", runner),
        (raw_dataset_name, "*", "*"),
    ]
    for raw, ns, rnr in candidates:
        row = conn.execute(
            """
            SELECT a.dataset_release_id, a.inferred_run_scope, r.benchmark_slug
            FROM dataset_source_aliases a
            JOIN dataset_releases r ON r.dataset_release_id = a.dataset_release_id
            WHERE a.raw_dataset_name = ? AND a.source_namespace = ? AND a.runner = ?
            LIMIT 1
            """,
            (raw, ns, rnr),
        ).fetchone()
        if row is not None:
            return row["benchmark_slug"], row["dataset_release_id"], "mapped", row["inferred_run_scope"]

    return None, None, "unmapped", None


def determine_run_scope(
    *,
    harness: str | None,
    raw_dataset_name: str | None,
    batch_name: str | None,
    planned: int | None,
    recorded: int,
    unique_tasks: int,
    known_task_count: int | None = None,
    inferred_run_scope: str | None = None,
    exact_full_task_match: bool = False,
) -> str:
    """Orthogonal run_scope determination (v2.2.1 03-2 §2 & A-06):
    NEVER infer full_split from 80% threshold or from official-result attachment!"""
    if (harness or "").lower() in ("oracle", "nop"):
        return "reference"

    if inferred_run_scope in ("full_split", "subset", "smoke", "reference"):
        return inferred_run_scope

    ds_low = (raw_dataset_name or "").lower()
    bn_low = (batch_name or "").lower()
    if "smoke" in ds_low or "demo" in ds_low or "smoke" in bn_low:
        return "smoke"

    if (planned is not None and planned <= 2) or (planned is None and 0 < recorded <= 2):
        return "smoke"

    if exact_full_task_match:
        return "full_split"

    if known_task_count is not None and known_task_count > 0 and unique_tasks == known_task_count:
        # Only exact 100% task-count match with verified release qualifies as full_split
        return "full_split"

    if recorded > 0 or (planned is not None and planned > 2):
        return "subset"

    return "unknown"


def make_canonical_trial_key(
    *,
    trial_id: str | None,
    batch_id: str,
    task_id: str,
    checkpoint: str | None,
    harness_version: str | None,
    attempt_index: int = 1,
) -> str:
    """Canonical trial identity (v2.2.1 04-1 & A-13):
    - Real UUID trials keep their UUID so cross-source copies share canonical_trial_key.
    - Non-UUID historical trials MUST include real run_id (batch_id) + checkpoint + harness_version
      so two independent reruns with identical config remain 2 distinct records!"""
    if trial_id and re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        trial_id.strip(),
    ):
        return trial_id.strip().lower()
    ck = checkpoint or "none"
    hv = harness_version or "none"
    return f"run:{batch_id}|ck:{ck}|hv:{hv}|task:{task_id}|att:{attempt_index}"


def infer_scoring_protocol(
    *,
    source_namespace: str,
    runner: str,
    benchmark_slug: str | None,
    has_official_result: bool = False,
) -> str:
    if source_namespace == "native_harbor":
        return "harbor-native-reward-valid-v1"
    if source_namespace == "legacy_csv":
        return "legacy-20261120-csv"
    if source_namespace == "legacy_json":
        if benchmark_slug == "deepswe":
            return "deepswe-f2p-p2p"
        if benchmark_slug == "gaia":
            return "gaia-official-scorer"
        if benchmark_slug == "wildclawbench":
            return "wildclaw-overall-score-v1"
        return "legacy-20261120-json"
    if runner == "vlmevalkit":
        return "vlmevalkit-native-v1"
    if runner == "direct":
        return "direct-native-v1"
    if benchmark_slug == "wildclawbench":
        return "wildclaw-overall-score-v1"
    return "lrm-eval-report-v1"


def source_priority_rank(source_namespace: str, scoring_protocol: str) -> int:
    """Deterministic tie-breaker inside an authorized project set for the same (canonical_trial_key, scoring_protocol)."""
    if scoring_protocol == "harbor-native-reward-valid-v1":
        return 1 if source_namespace == "native_harbor" else 5
    order = {
        "harbor_runs": 1,
        "runner_published": 1,
        "native_harbor": 2,
        "legacy_json": 3,
        "legacy_csv": 4,
    }
    return order.get(source_namespace, 10)
