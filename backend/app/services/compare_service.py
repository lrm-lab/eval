from __future__ import annotations

import csv
import html
import io
import json
import math
import secrets
from contextlib import nullcontext
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from app.api.errors import AppError
from app.config import Settings
from app.db.database import Database
from app.db.migrations import utcnow_iso
from app.indexing.hot_indexer import SourceApprovalRegistry
from app.security.auth import AuthUser
from app.security.authz import (
    authorized_projects_for_user,
    can_access_project,
    has_scope,
    is_holdout_dataset_or_batch,
    is_personal_project,
    personal_project_id,
    require_project_access,
    require_scope,
)
from app.security.storage_probe import resolve_within_root, validate_safe_id
from app.services.evaluation_history import build_evaluation_history
from app.services.methodology import (
    METHODOLOGY_VERSION,
    compute_comparative_macro_avg,
    compute_unrounded_delta,
    finite,
    normalize_trial_score_to_100,
)

LEGACY_BENCHMARK_DIR_MAP: dict[str, str] = {
    "aa-lcr": "AA-LCR-v1.1",
    "hle": "hle_llmjudge",
    "ifbench": "IFBench",
    "multi-lcb": "Multi-LCB",
    "cmmmu": "CMMMU",
    "mmmu-pro-10c": "MMMU_Pro_10c",
    "mm-ifeval": "MM-IFEval",
    "gpqa-diamond": "GPQA_diamond",
    "ifeval": "IFEval",
    "apex-shortlist": "Apex-shortlist",
    "multi-challenge": "Multi-Challenge",
    "ai2d-test": "AI2D_TEST",
    "mmbench-v1-1-cn": "MMBench_v1_1_CN",
    "realworldqa": "RealWorldQA",
    "mathvista": "MathVista",
    "charxiv-reasoning": "CharXiv_reasoning",
    "hallusionbench": "HallusionBench",
    "ocrbench": "OCRBench",
}


class CompareService:
    """Dynamic comparison service with:
    - Bound comparison_id snapshots (A-02)
    - Strict 3-denominator distinction: common_valid, common_covered_faults_as_zero, self_full_batch_valid
    - Unrounded float64 delta_pp & rel_pct
    - Gain vs Guardrail 3-track verdict (A-14)
    - Live re-authorization on every saved snapshot reopen (S-02)
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        approval_registry: SourceApprovalRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.approval_registry = approval_registry or SourceApprovalRegistry(settings.source_approval_file, parallel_lookup=True)

    def _legacy_csv_provenance(self) -> tuple[str, str]:
        proj_id, owner_id, _, _ = self.approval_registry.lookup_full(self.settings.legacy_root_dir / "leaderboard.csv")
        return proj_id, owner_id

    def _can_access_legacy_csv(self, user: AuthUser) -> bool:
        proj_id, owner_id = self._legacy_csv_provenance()
        return can_access_project(user, proj_id, owner_id=owner_id)

    def _require_legacy_csv_access(self, user: AuthUser) -> None:
        proj_id, owner_id = self._legacy_csv_provenance()
        require_project_access(user, proj_id, owner_id=owner_id)

    def _is_holdout_release(self, dataset_release_id: str | None, *, conn=None) -> bool:
        if not dataset_release_id:
            return False
        rel_split: str | None = None
        if conn is not None:
            r = conn.execute(
                "SELECT split FROM dataset_releases WHERE dataset_release_id = ?",
                (dataset_release_id,),
            ).fetchone()
            rel_split = r["split"] if r is not None else None
        else:
            r = self.db.fetchone(
                "SELECT split FROM dataset_releases WHERE dataset_release_id = ?",
                (dataset_release_id,),
            )
            rel_split = r["split"] if r is not None else None
        return is_holdout_dataset_or_batch(
            dataset_release_id=dataset_release_id,
            release_split=rel_split,
        )

    def _is_holdout_batch(self, batch: dict[str, Any], *, conn=None) -> bool:
        rel_id = batch.get("dataset_release_id")
        rel_split: str | None = None
        if rel_id:
            if conn is not None:
                r = conn.execute(
                    "SELECT split FROM dataset_releases WHERE dataset_release_id = ?",
                    (rel_id,),
                ).fetchone()
                rel_split = r["split"] if r is not None else None
            else:
                r = self.db.fetchone(
                    "SELECT split FROM dataset_releases WHERE dataset_release_id = ?",
                    (rel_id,),
                )
                rel_split = r["split"] if r is not None else None
        return is_holdout_dataset_or_batch(
            dataset_release_id=str(rel_id) if rel_id else None,
            release_split=rel_split,
            benchmark_slug=batch.get("benchmark_slug"),
            raw_dataset_name=batch.get("raw_dataset_name"),
            batch_name=batch.get("batch_name"),
        )

    def _holdout_task_ids_for_release(self, dataset_release_id: str | None, *, conn=None) -> set[str]:
        if not dataset_release_id:
            return set()
        sql = "SELECT task_id FROM task_definitions WHERE dataset_release_id = ? AND LOWER(COALESCE(split, '')) = 'holdout'"
        if conn is not None:
            rows = conn.execute(sql, (dataset_release_id,)).fetchall()
        else:
            rows = self.db.fetchall(sql, (dataset_release_id,))
        return {str(r["task_id"]) for r in rows}

    def _holdout_task_ids_for_releases(self, release_ids: set[str] | list[str], *, conn=None) -> set[str]:
        out: set[str] = set()
        for rid in release_ids:
            if rid:
                out |= self._holdout_task_ids_for_release(str(rid), conn=conn)
        return out

    def _contains_holdout_tasks(
        self,
        dataset_release_id: str | None,
        task_ids: list[str] | set[str] | None,
        *,
        conn=None,
    ) -> bool:
        if self._is_holdout_release(dataset_release_id, conn=conn):
            return True
        cleaned = sorted({str(t).strip() for t in (task_ids or []) if str(t).strip()})
        if not cleaned:
            return False
        ph = ",".join("?" for _ in cleaned)
        if dataset_release_id and dataset_release_id != "custom":
            sql = (
                f"SELECT 1 AS hit FROM task_definitions "
                f"WHERE dataset_release_id = ? AND task_id IN ({ph}) "
                f"AND LOWER(COALESCE(split, '')) = 'holdout' LIMIT 1"
            )
            params: list[Any] = [dataset_release_id, *cleaned]
        else:
            sql = (
                f"SELECT 1 AS hit FROM task_definitions "
                f"WHERE task_id IN ({ph}) AND LOWER(COALESCE(split, '')) = 'holdout' LIMIT 1"
            )
            params = cleaned
        if conn is not None:
            return conn.execute(sql, params).fetchone() is not None
        return self.db.fetchone(sql, params) is not None

    @staticmethod
    def _norm_checksum(val: Any) -> str:
        return str(val).strip() if val is not None else ""

    @staticmethod
    def _protocols_compatible(
        protocols: set[str],
        requested_protocol: str | None,
        benchmark_slug: str | None = None,
    ) -> bool:
        clean = {str(p) for p in protocols if p}
        if len(clean) <= 1:
            return True
        gaia_protos = {"gaia-official-scorer", "gaia-raw-correct"}
        if requested_protocol in gaia_protos and (benchmark_slug == "gaia" or clean.issubset(gaia_protos)):
            return True
        return False

    @staticmethod
    def _unmeasured_verdict(benchmark: dict[str, Any], issue: str | None = None) -> dict[str, Any]:
        if issue is None:
            issue = {
                "raw_visible_unintegrated": "not_integrated",
                "pending_version_pin": "pending_dataset_version",
            }.get(benchmark["status"], "missing_comparable_scores")
        reasons = {
            "not_integrated": "原始数据已登记，评测尚未接入。",
            "pending_dataset_version": "上游题库的版本、语言或数据划分尚待确认。",
            "different_scoring_protocol": "所选结果的评分协议不同，不能据此计算相对变化。",
            "different_dataset_release": "所选批次来自同一基准的不同数据集发行版。",
            "no_common_valid_tasks": "所选批次没有双方都取得有效评分的题目。",
            "missing_comparable_scores": "当前对比对象缺少该基准的可比成绩。",
        }
        return {
            "benchmark_slug": benchmark["benchmark_slug"],
            "dataset_release_id": None,
            "track_slug": benchmark["track_slug"],
            "scoring_protocol": None,
            "denominator_kind": None,
            "baseline_score": None,
            "candidate_score": None,
            "delta_pp": None,
            "rel_pct": None,
            "coverage": "0/0",
            "status": "undeterminable",
            "reason_code": issue,
            "reason": reasons[issue],
        }

    @staticmethod
    def _build_drift_agg_groups(
        drift_detail_groups: list[dict[str, Any]],
        *,
        excluded_task_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Aggregate per-(task_checksum, subject_ids) counts from drift_detail_groups,
        optionally excluding unauthorized task_ids (e.g. holdout tasks without dataset:holdout_read)."""
        agg_counter: dict[tuple[str, tuple[str, ...]], int] = {}
        for g in drift_detail_groups:
            tid = str(g.get("task_id") or "")
            if excluded_task_ids and tid in excluded_task_ids:
                continue
            ck = str(g.get("task_checksum") or "")
            sids = tuple(str(e.get("subject_id") or "") for e in (g.get("subjects") or []))
            agg_key = (ck, sids)
            agg_counter[agg_key] = agg_counter.get(agg_key, 0) + 1
        return [
            {
                "task_checksum": ck or None,
                "subject_ids": list(sids),
                "task_count": cnt,
            }
            for (ck, sids), cnt in sorted(agg_counter.items())
        ]

    @classmethod
    def _compute_aligned_task_sets(
        cls,
        ep_maps: dict[str, dict[str, dict[str, Any]]],
        subject_ids: list[str],
        *,
        batch_id_by_sid: dict[str, str] | None = None,
    ) -> tuple[list[str], list[str], list[str], list[dict[str, Any]], list[dict[str, Any]]]:
        """Align tasks strictly on `(task_id, task_checksum)` (v2.2.1 02-4 §2.1 & A-02/A-11).
        Returns:
          - matched_covered: sorted task_ids present in ALL subject_ids with identical normalized task_checksum
          - matched_valid: subset of matched_covered where ALL subject_ids have valid finite rewards
          - drift_task_ids: sorted task_ids present in ALL subject_ids whose task_checksum differs across subjects
          - drift_detail_groups: per-(task_id, task_checksum) independent groups (for task-diff)
          - drift_agg_groups: aggregate per-(task_checksum, subject_ids) counts without task_ids (for matrix)"""
        if not subject_ids or any(sid not in ep_maps for sid in subject_ids):
            return [], [], [], [], []
        shared_tids = sorted(set.intersection(*(set(ep_maps[sid].keys()) for sid in subject_ids)))
        matched_covered: list[str] = []
        drift_task_ids: list[str] = []
        drift_detail_groups: list[dict[str, Any]] = []

        for tid in shared_tids:
            cks = {cls._norm_checksum(ep_maps[sid][tid].get("task_checksum")) for sid in subject_ids}
            if len(cks) == 1:
                matched_covered.append(tid)
            else:
                drift_task_ids.append(tid)
                by_ck: dict[str, list[dict[str, Any]]] = {}
                for sid in subject_ids:
                    ep = ep_maps[sid][tid]
                    ck = cls._norm_checksum(ep.get("task_checksum"))
                    subj_entry: dict[str, Any] = {
                        "subject_id": sid,
                        "trial_id": ep["trial_id"],
                        "status": ep["status"],
                        "reward": ep["effective_reward"],
                        "reward_valid": bool(ep["reward_valid"]),
                        "duration_sec": ep["duration_sec"],
                        "tool_calls": ep["tool_call_count"],
                    }
                    if batch_id_by_sid and sid in batch_id_by_sid:
                        subj_entry["batch_id"] = batch_id_by_sid[sid]
                    by_ck.setdefault(ck, []).append(subj_entry)
                for ck in sorted(by_ck.keys()):
                    entries = by_ck[ck]
                    drift_detail_groups.append(
                        {
                            "task_id": tid,
                            "task_checksum": ck or None,
                            "subjects": entries,
                        }
                    )

        matched_valid = [
            tid
            for tid in matched_covered
            if all(ep_maps[sid][tid]["reward_valid"] == 1 and finite(ep_maps[sid][tid]["effective_reward"]) for sid in subject_ids)
        ]
        drift_agg_groups = cls._build_drift_agg_groups(drift_detail_groups)
        return matched_covered, matched_valid, drift_task_ids, drift_detail_groups, drift_agg_groups

    def _filter_compare_result_version_drift(
        self,
        user: AuthUser,
        result: dict[str, Any],
        cfg: dict[str, Any],
    ) -> dict[str, Any]:
        """Enforce live `dataset:holdout_read` filtering on `rows[*].version_drift.checksum_groups`
        across saved-snapshot reopens and exports (v2.2.1 06-1 §2.2 & S-02).
        - Aggregate denominators and `drifted_task_count` remain intact for all project-authorized viewers.
        - Without `dataset:holdout_read`: whole-holdout rows return `checksum_groups=[]`; mixed-dataset
          rows return ONLY public-task checksum groups (fail-closed to `[]` if bound generation episodes
          cannot be verified live).
        - With `dataset:holdout_read`: hydrates full `checksum_groups` from the exact bound `(batch_id, generation_id)`."""
        rows = result.get("rows")
        if not isinstance(rows, list) or not rows:
            return result

        can_holdout = has_scope(user, "dataset:holdout_read")
        bound = cfg.get("bound_batches") or []
        frozen_tids_raw = cfg.get("frozen_sample_task_ids")
        allow_tids = {str(t) for t in frozen_tids_raw} if frozen_tids_raw is not None else None
        req_proto = (cfg.get("payload") or {}).get("scoring_protocol")

        with self.db.read_snapshot() as snap_conn:
            live_by_bid: dict[str, dict[str, Any]] = {}
            for b_ref in bound:
                bid = str(b_ref.get("batch_id") or "")
                if bid and bid not in live_by_bid:
                    r_b = snap_conn.execute("SELECT * FROM batches WHERE batch_id = ?", (bid,)).fetchone()
                    if r_b is not None:
                        live_by_bid[bid] = dict(r_b)

            for row in rows:
                vd = row.get("version_drift")
                if not isinstance(vd, dict):
                    continue
                rel_id = str(row.get("dataset_release_id") or "")
                drifted_total = int(vd.get("drifted_task_count") or 0)
                rel_bound = [
                    b_ref
                    for b_ref in bound
                    if str(
                        b_ref.get("dataset_release_id")
                        or live_by_bid.get(str(b_ref.get("batch_id") or ""), {}).get("dataset_release_id")
                        or ""
                    )
                    == rel_id
                ]

                rel_is_whole_holdout = self._is_holdout_release(rel_id, conn=snap_conn)
                if not rel_is_whole_holdout:
                    for b_ref in rel_bound:
                        live_b = live_by_bid.get(str(b_ref.get("batch_id") or ""))
                        if live_b is not None:
                            if self._is_holdout_batch(live_b, conn=snap_conn):
                                rel_is_whole_holdout = True
                                break
                        elif is_holdout_dataset_or_batch(
                            dataset_release_id=rel_id or None,
                            batch_name=b_ref.get("alias"),
                        ):
                            rel_is_whole_holdout = True
                            break

                rel_holdout_tids = (
                    set()
                    if rel_is_whole_holdout
                    else self._holdout_task_ids_for_releases({rel_id}, conn=snap_conn)
                )

                if drifted_total == 0:
                    vd["checksum_groups"] = []
                    vd["checksum_groups_redacted"] = False
                    vd["hidden_holdout_drifted_tasks"] = 0
                    continue

                def _recompute_bound_drift() -> tuple[bool, list[str], list[dict[str, Any]], list[dict[str, Any]]]:
                    if not rel_bound:
                        return False, [], [], []
                    ep_maps_live: dict[str, dict[str, dict[str, Any]]] = {}
                    rel_sids_live: list[str] = []
                    eff_proto = req_proto or row.get("scoring_protocol")
                    for b_ref in rel_bound:
                        bid = str(b_ref.get("batch_id") or "")
                        sid = str(b_ref.get("subject_id") or "")
                        live_b = live_by_bid.get(bid)
                        if live_b is None:
                            return False, [], [], []
                        b_gen = b_ref.get("generation_id") or live_b.get("generation_id")
                        emap = self._get_batch_episode_map(
                            bid,
                            eff_proto,
                            conn=snap_conn,
                            expected_generation_id=b_gen,
                        )
                        if int(live_b.get("recorded") or 0) > 0 and not emap:
                            return False, [], [], []
                        if allow_tids is not None:
                            emap = {tid: ep for tid, ep in emap.items() if tid in allow_tids}
                        ep_maps_live[sid] = emap
                        rel_sids_live.append(sid)
                    _, _, d_tids, d_details, d_full_agg = self._compute_aligned_task_sets(ep_maps_live, rel_sids_live)
                    return True, d_tids, d_details, d_full_agg

                if can_holdout:
                    if rel_is_whole_holdout or rel_holdout_tids or vd.get("checksum_groups_redacted"):
                        ok, _, _, d_full_agg = _recompute_bound_drift()
                        if ok:
                            vd["checksum_groups"] = d_full_agg
                    vd["checksum_groups_redacted"] = False
                    vd["hidden_holdout_drifted_tasks"] = 0
                else:
                    if rel_is_whole_holdout:
                        vd["checksum_groups"] = []
                        vd["checksum_groups_redacted"] = True
                        vd["hidden_holdout_drifted_tasks"] = drifted_total
                    elif rel_holdout_tids:
                        ok, d_tids, d_details, _ = _recompute_bound_drift()
                        if ok:
                            pub_groups = self._build_drift_agg_groups(d_details, excluded_task_ids=rel_holdout_tids)
                            hidden_cnt = sum(1 for tid in d_tids if tid in rel_holdout_tids)
                            vd["checksum_groups"] = pub_groups
                            vd["checksum_groups_redacted"] = hidden_cnt > 0
                            vd["hidden_holdout_drifted_tasks"] = hidden_cnt
                        else:
                            # Fail-closed: bound generation episodes unavailable to verify per-task holdout split
                            vd["checksum_groups"] = []
                            vd["checksum_groups_redacted"] = True
                            vd["hidden_holdout_drifted_tasks"] = drifted_total
                    else:
                        vd["checksum_groups_redacted"] = False
                        vd["hidden_holdout_drifted_tasks"] = 0
        return result

    def _registered_legacy_model_cols(self, *, conn=None) -> set[str]:
        sql = "SELECT DISTINCT model_col FROM legacy_leaderboard_rows"
        if conn is not None:
            rows = conn.execute(sql).fetchall()
        else:
            rows = self.db.fetchall(sql)
        return {str(r["model_col"]) for r in rows if r["model_col"]}

    def _load_authorized_batch(
        self,
        user: AuthUser,
        batch_id: str,
        *,
        conn=None,
    ) -> dict[str, Any]:
        if conn is not None:
            r = conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
            row = dict(r) if r is not None else None
        else:
            row = self.db.fetchone("SELECT * FROM batches WHERE batch_id = ?", (batch_id,))
        if row is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"批次不存在: {batch_id}")
        require_project_access(user, row["project_id"], owner_id=row["owner_id"])
        return row

    def _get_batch_episode_map(
        self,
        batch_id: str,
        scoring_protocol: str | None = None,
        *,
        conn=None,
        expected_generation_id: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return per-task episode record for a batch (including faults, with raw pass flag for faults_as_zero).
        When `expected_generation_id` is supplied, strictly filters by `generation_id = ?` on the
        same MVCC read snapshot connection so concurrent new-generation commits can NEVER tear results."""
        if expected_generation_id is not None:
            sql = """
                SELECT trial_id, task_id, task_checksum, status, reward, reward_valid,
                       verifier_rewards_json, duration_sec, tool_call_count, exception_type, failure_kind, generation_id
                FROM episodes
                WHERE batch_id = ? AND generation_id = ?
                ORDER BY attempt_index ASC, trial_id ASC
            """
            params: tuple[Any, ...] = (batch_id, expected_generation_id)
        else:
            sql = """
                SELECT trial_id, task_id, task_checksum, status, reward, reward_valid,
                       verifier_rewards_json, duration_sec, tool_call_count, exception_type, failure_kind, generation_id
                FROM episodes
                WHERE batch_id = ?
                ORDER BY attempt_index ASC, trial_id ASC
            """
            params = (batch_id,)
        if conn is not None:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        else:
            rows = self.db.fetchall(sql, params)
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            vr = json.loads(r["verifier_rewards_json"] or "{}")
            r["verifier_rewards"] = vr
            # If GAIA raw-correct protocol requested, use verifier_rewards.correct when valid
            if scoring_protocol == "gaia-raw-correct" and r["reward_valid"] == 1:
                r["effective_reward"] = 1.0 if vr.get("correct") else 0.0
            else:
                r["effective_reward"] = r["reward"]
            # Keep first attempt or latest per task_id (for single-attempt historical files)
            out[r["task_id"]] = r
        return out

    def evaluation_history(self, user: AuthUser, items: list[dict[str, Any]]) -> dict[str, Any]:
        with self.db.read_snapshot() as conn:
            return build_evaluation_history(conn, user, items, self.preview_batch_comparison)

    def preview_batch_comparison(
        self, user: AuthUser, pair: dict[str, Any], *, conn: Connection | None = None,
    ) -> dict[str, Any]:
        subjects = [
            {
                "id": side, "kind": "batch", "batch_id": batch["batch_id"],
                "expected_generation_id": batch["generation_id"],
            }
            for side, batch in (("baseline", pair["baseline"]), ("candidate", pair["candidate"]))
        ]
        try:
            result = self.query_compare(
                user,
                {
                    "subjects": subjects,
                    "baseline_subject_id": "baseline",
                    "scoring_protocol": pair["scoring_protocol"],
                    "scope": {"dataset_release_ids": [pair["dataset_release_id"]]},
                    "include_verdict": False,
                    "enable_faults_as_zero_protocol": False,
                },
                persist_ephemeral=False,
                read_snapshot=conn,
                exact_batch_context=(pair["dataset_release_id"], pair["scoring_protocol"]),
            )
        except AppError as exc:
            if exc.status_code >= 500:
                raise
            return {"id": pair["id"], "reason": exc.code, "message": exc.message}
        row = next(iter(result["rows"]), None)
        if row is None or not row["comparable"]:
            return {"id": pair["id"], "reason": "INCOMPARABLE_RELEASE"}
        intersection = row["intersection"]
        baseline = row["cells"]["baseline"]["common_valid"]
        candidate = row["cells"]["candidate"]["common_valid"]
        return {
            "id": pair["id"],
            "reason": None if intersection["common_valid_scored_tasks"] else "NO_COMMON_VALID_TASKS",
            "common_valid": intersection["common_valid_scored_tasks"],
            "common_covered": intersection["common_covered_tasks"],
            "baseline_score": baseline["score"],
            "candidate_score": candidate["score"],
            "delta_pp": candidate["delta_pp"],
            "metric_name": row["metric_name"],
        }

    def query_compare(
        self,
        user: AuthUser,
        payload: dict[str, Any],
        *,
        persist_ephemeral: bool = True,
        exact_batch_context: tuple[str, str] | None = None,
        read_snapshot: Connection | None = None,
    ) -> dict[str, Any]:
        subjects = payload.get("subjects") or []
        if len(subjects) < 2:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "至少需要 2 个对比对象")
        if all(str(s.get("kind") or "") == "legacy_model" for s in subjects):
            return self._query_compare_legacy_models(user, payload, persist_ephemeral=persist_ephemeral)

        baseline_id = payload.get("baseline_subject_id") or subjects[0]["id"]
        scoring_protocol = payload.get("scoring_protocol")
        align_mode = payload.get("align_mode", "intersection")
        multi_attempt_agg = payload.get("multi_attempt_agg", "valid_mean")
        regression_threshold_pp = float(payload.get("regression_threshold_pp", -1.0))
        include_verdict = bool(payload.get("include_verdict", True))
        enable_faults_as_zero = bool(payload.get("enable_faults_as_zero_protocol", True))
        scope_obj = payload.get("scope") or {}
        sample_set_id = scope_obj.get("sample_set_id")
        scope_release_ids = set(scope_obj.get("dataset_release_ids") or [])

        allowed_task_ids: set[str] | None = None
        sample_set_meta: dict[str, Any] | None = None
        if sample_set_id:
            validate_safe_id(str(sample_set_id), "sample_set_id")
            ss_row = self.db.fetchone("SELECT * FROM sample_sets WHERE set_id = ?", (str(sample_set_id),))
            if ss_row is None:
                raise AppError(404, "RESOURCE_NOT_FOUND", f"自定义样本集不存在: {sample_set_id}")
            require_project_access(user, ss_row["project_id"], owner_id=ss_row["owner_id"])
            allowed_task_ids = {str(t) for t in json.loads(ss_row["task_ids_json"] or "[]")}
            if self._contains_holdout_tasks(ss_row.get("dataset_release_id"), allowed_task_ids):
                require_scope(user, "dataset:holdout_read")
            sample_set_meta = {
                "set_id": str(sample_set_id),
                "project_id": ss_row["project_id"],
                "owner_id": ss_row["owner_id"],
                "dataset_release_id": ss_row.get("dataset_release_id"),
            }

        # 1. Load & authorize every participating batch + episodes within ONE consistent WAL read snapshot
        # so bound_batches[i].generation_id and all computed episode scores come from the exact same point-in-time!
        bound_batches: list[dict[str, Any]] = []
        subj_batches: dict[str, dict[str, Any]] = {}
        rel_meta_map: dict[str, dict[str, Any]] = {}
        rel_whole_holdout_map: dict[str, bool] = {}
        rel_holdout_tids_map: dict[str, set[str]] = {}
        ep_maps_by_rel: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
        core_bms: list[dict[str, Any]] = []

        snapshot = self.db.read_snapshot() if read_snapshot is None else nullcontext(read_snapshot)
        with snapshot as snap_conn:
            for s in subjects:
                sid = str(s["id"])
                bid = str(s["batch_id"])
                b = self._load_authorized_batch(user, bid, conn=snap_conn)
                expected_generation = s.get("expected_generation_id")
                if expected_generation is not None and expected_generation != b["generation_id"]:
                    raise AppError(
                        409,
                        "GENERATION_MISMATCH",
                        "已选批次的发布代发生变化，请刷新批次后重新对比",
                        {
                            "batch_id": bid,
                            "expected_generation_id": expected_generation,
                            "current_generation_id": b["generation_id"],
                        },
                    )
                if exact_batch_context is not None:
                    release, protocol = exact_batch_context
                    if b["dataset_release_id"] != release:
                        raise AppError(400, "INCOMPARABLE_RELEASE", "所选批次的数据集发行版不同")
                    if b["scoring_protocol"] != protocol:
                        raise AppError(400, "INCOMPARABLE_PROTOCOL", "所选批次的评分协议不同")
                subj_batches[sid] = b
                bound_batches.append(
                    {
                        "subject_id": sid,
                        "alias": s.get("alias") or b["batch_name"],
                        "batch_id": bid,
                        "project_id": b["project_id"],
                        "owner_id": b["owner_id"],
                        "generation_id": b["generation_id"],
                        "dataset_release_id": b.get("dataset_release_id"),
                        "scoring_protocol": b.get("scoring_protocol"),
                    }
                )

            releases_present = sorted({b["dataset_release_id"] for b in subj_batches.values() if b.get("dataset_release_id")})
            if scope_release_ids:
                releases_present = [r for r in releases_present if r in scope_release_ids]
            for rel_id in releases_present:
                rel_r = snap_conn.execute(
                    """
                    SELECT r.*, bm.display_name AS bm_display_name, bm.track_slug, bm.goal_type
                    FROM dataset_releases r
                    JOIN benchmarks bm ON bm.benchmark_slug = r.benchmark_slug
                    WHERE r.dataset_release_id = ?
                    """,
                    (rel_id,),
                ).fetchone()
                if rel_r is None:
                    continue
                rel_d = dict(rel_r)
                rel_meta_map[rel_id] = rel_d
                rel_subjs_snap = [s for s in subjects if subj_batches[str(s["id"])].get("dataset_release_id") == rel_id]
                rel_is_ho = self._is_holdout_release(rel_id, conn=snap_conn) or any(
                    self._is_holdout_batch(subj_batches[str(s["id"])], conn=snap_conn) for s in rel_subjs_snap
                )
                if exact_batch_context is not None and rel_is_ho:
                    require_scope(user, "dataset:holdout_read")
                rel_whole_holdout_map[rel_id] = rel_is_ho
                rel_holdout_tids_map[rel_id] = (
                    set() if rel_is_ho else self._holdout_task_ids_for_releases({rel_id}, conn=snap_conn)
                )
                eff_proto_snap = scoring_protocol or rel_d["default_protocol"]
                ep_map_rel: dict[str, dict[str, dict[str, Any]]] = {}
                for s in rel_subjs_snap:
                    emap_raw = self._get_batch_episode_map(
                        subj_batches[str(s["id"])]["batch_id"],
                        eff_proto_snap,
                        conn=snap_conn,
                        expected_generation_id=subj_batches[str(s["id"])]["generation_id"],
                    )
                    if allowed_task_ids is not None:
                        emap_raw = {tid: ep for tid, ep in emap_raw.items() if tid in allowed_task_ids}
                    ep_map_rel[str(s["id"])] = emap_raw
                ep_maps_by_rel[rel_id] = ep_map_rel

            if include_verdict:
                core_bms = [
                    dict(r)
                    for r in snap_conn.execute("SELECT * FROM benchmarks ORDER BY track_slug, benchmark_slug").fetchall()
                ]

        can_holdout = has_scope(user, "dataset:holdout_read")
        rows_out: list[dict[str, Any]] = []
        rows_for_snapshot: list[dict[str, Any]] = []
        subject_scores_for_macro: dict[str, dict[str, tuple[float | None, str | None]]] = {
            str(s["id"]): {} for s in subjects
        }
        self_full_avgs: dict[str, list[float]] = {str(s["id"]): [] for s in subjects}
        verdict_agentic: list[dict[str, Any]] = []
        verdict_guardrails: list[dict[str, Any]] = []
        auto_insights: list[dict[str, Any]] = []
        issues_by_benchmark: dict[str, str] = {}

        comparison_id = f"cmp_{secrets.token_hex(8)}"

        for rel_id in releases_present:
            rel = rel_meta_map.get(rel_id)
            if rel is None:
                continue

            # Map subjects that belong to this release
            rel_subjs = [s for s in subjects if subj_batches[str(s["id"])].get("dataset_release_id") == rel_id]
            eff_proto = scoring_protocol or rel["default_protocol"]
            protocols = {str(subj_batches[str(s["id"])]["scoring_protocol"]) for s in rel_subjs if subj_batches[str(s["id"])].get("scoring_protocol")}
            protocols_compatible = self._protocols_compatible(protocols, scoring_protocol, rel["benchmark_slug"])
            comparable = len(rel_subjs) == len(subjects) and protocols_compatible

            ep_maps = ep_maps_by_rel[rel_id]
            rel_sids = [str(s["id"]) for s in rel_subjs]
            (
                common_covered_list,
                common_valid_list,
                drift_task_ids,
                drift_detail_groups,
                full_drift_agg_groups,
            ) = self._compute_aligned_task_sets(ep_maps, rel_sids)
            rel_is_ho = rel_whole_holdout_map.get(rel_id, False)
            rel_ho_tids = rel_holdout_tids_map.get(rel_id, set())
            if rel_is_ho:
                public_drift_agg_groups: list[dict[str, Any]] = []
                holdout_drifted_count = len(drift_task_ids)
            elif rel_ho_tids:
                public_drift_agg_groups = self._build_drift_agg_groups(
                    drift_detail_groups, excluded_task_ids=rel_ho_tids
                )
                holdout_drifted_count = sum(1 for tid in drift_task_ids if tid in rel_ho_tids)
            else:
                public_drift_agg_groups = full_drift_agg_groups
                holdout_drifted_count = 0
            common_covered = set(common_covered_list)
            common_valid = set(common_valid_list)

            fault_excluded = len(common_covered) - len(common_valid)
            n_valid = len(common_valid)
            n_covered = len(common_covered)

            issue = None
            if len(rel_subjs) == len(subjects):
                if not protocols_compatible:
                    issue = "different_scoring_protocol"
                elif not n_valid:
                    issue = "no_common_valid_tasks"
            elif all(
                b.get("benchmark_slug") == rel["benchmark_slug"] and b.get("dataset_release_id")
                for b in subj_batches.values()
            ):
                issue = "different_dataset_release"
            else:
                issue = "missing_comparable_scores"
            if issue:
                issues_by_benchmark[rel["benchmark_slug"]] = issue

            # Compute exact unrounded scores per subject
            raw_common_valid_pct: dict[str, float] = {}
            raw_faz_pct: dict[str, float] = {}
            cells: dict[str, dict[str, Any]] = {}

            for s in rel_subjs:
                sid = str(s["id"])
                b = subj_batches[sid]
                emap = ep_maps[sid]
                reward_protocol = "gaia-raw-correct" if eff_proto == "gaia-raw-correct" else b["scoring_protocol"]

                # 1. common_valid (primary intersection denominator)
                if n_valid > 0:
                    cv_rewards = [
                        normalize_trial_score_to_100(float(emap[t]["effective_reward"]), rel["scale_type"], reward_protocol)
                        for t in common_valid
                    ]
                    cv_pct = math.fsum(cv_rewards) / n_valid
                    cv_passed = sum(1 for t in common_valid if float(emap[t]["effective_reward"]) >= 1.0)
                    raw_common_valid_pct[sid] = cv_pct
                else:
                    cv_pct = None
                    cv_passed = 0

                # 2. common_covered_faults_as_zero (each subject's OWN actual passes across ALL common_covered tasks!)
                # NEVER divide common_valid passes by common_covered (v2.2.1 03-2 §3.2 & A-02)!
                if n_covered > 0 and enable_faults_as_zero:
                    faz_scores = []
                    faz_passed = 0
                    for t in common_covered:
                        ep = emap[t]
                        if ep["reward_valid"] == 1 and finite(ep["effective_reward"]):
                            val100 = normalize_trial_score_to_100(
                                float(ep["effective_reward"]), rel["scale_type"], reward_protocol,
                            )
                            faz_scores.append(val100)
                            if float(ep["effective_reward"]) >= 1.0:
                                faz_passed += 1
                        else:
                            faz_scores.append(0.0)
                    faz_pct = math.fsum(faz_scores) / n_covered
                    raw_faz_pct[sid] = faz_pct
                else:
                    faz_pct = None
                    faz_passed = 0

                # 3. self_full_batch_valid (independent full-batch valid mean, non-comparable reference)
                valid_all = [
                    normalize_trial_score_to_100(float(e["effective_reward"]), rel["scale_type"], reward_protocol)
                    for e in emap.values()
                    if e["reward_valid"] == 1 and finite(e["effective_reward"])
                ]
                self_pct = (math.fsum(valid_all) / len(valid_all)) if valid_all else None
                if self_pct is not None:
                    self_full_avgs[sid].append(self_pct)

                cells[sid] = {
                    "common_valid": {
                        "score": round(cv_pct, 2) if cv_pct is not None else None,
                        "passed": cv_passed,
                        "denominator": n_valid,
                        "is_best": False,
                        "delta_pp": None,
                        "rel_pct": None,
                    },
                    "common_covered_faults_as_zero": {
                        "enabled_by_protocol": enable_faults_as_zero,
                        "score": round(faz_pct, 2) if faz_pct is not None else None,
                        "passed": faz_passed,
                        "denominator": n_covered,
                        "delta_pp": None,
                    }
                    if enable_faults_as_zero
                    else {"enabled_by_protocol": False},
                    "self_full_batch_valid": {
                        "score": round(self_pct, 2) if self_pct is not None else None,
                        "valid_scored": len(valid_all),
                        "captured_tasks": len(emap),
                        "split_total": rel["known_task_count"] or len(emap),
                    },
                }
                subject_scores_for_macro[sid][rel_id] = (cv_pct, eff_proto if comparable else None)

            # Compute unrounded deltas & best flags
            if comparable and raw_common_valid_pct:
                best_cv = max(raw_common_valid_pct.values())
                base_cv = raw_common_valid_pct.get(baseline_id)
                base_faz = raw_faz_pct.get(baseline_id)
                for sid, cv_val in raw_common_valid_pct.items():
                    if abs(cv_val - best_cv) < 1e-9:
                        cells[sid]["common_valid"]["is_best"] = True
                    if base_cv is not None:
                        d_pp, r_pct = compute_unrounded_delta(base_cv, cv_val)
                        cells[sid]["common_valid"]["delta_pp"] = d_pp
                        cells[sid]["common_valid"]["rel_pct"] = r_pct
                    if enable_faults_as_zero and base_faz is not None and sid in raw_faz_pct:
                        d_faz, _ = compute_unrounded_delta(base_faz, raw_faz_pct[sid])
                        cells[sid]["common_covered_faults_as_zero"]["delta_pp"] = d_faz

            row_base = {
                "benchmark_slug": rel["benchmark_slug"],
                "dataset_release_id": rel_id,
                "raw_dataset_name": rel["bm_display_name"],
                "scoring_protocol": eff_proto,
                "metric_name": rel["metric_name"],
                "comparable": comparable,
                "comparison_issue": issue,
                "intersection": {
                    "common_covered_tasks": n_covered,
                    "common_valid_scored_tasks": n_valid,
                    "fault_excluded_tasks": fault_excluded,
                },
                "cells": cells,
            }
            resp_vd = (
                {
                    "detected": len(drift_task_ids) > 0,
                    "drifted_task_count": len(drift_task_ids),
                    "checksum_groups": full_drift_agg_groups,
                    "checksum_groups_redacted": False,
                    "hidden_holdout_drifted_tasks": 0,
                }
                if can_holdout
                else {
                    "detected": len(drift_task_ids) > 0,
                    "drifted_task_count": len(drift_task_ids),
                    "checksum_groups": public_drift_agg_groups,
                    "checksum_groups_redacted": holdout_drifted_count > 0,
                    "hidden_holdout_drifted_tasks": holdout_drifted_count,
                }
            )
            snap_vd = {
                "detected": len(drift_task_ids) > 0,
                "drifted_task_count": len(drift_task_ids),
                "checksum_groups": public_drift_agg_groups,
                "checksum_groups_redacted": holdout_drifted_count > 0,
                "hidden_holdout_drifted_tasks": holdout_drifted_count,
            }
            rows_out.append({**row_base, "version_drift": resp_vd})
            rows_for_snapshot.append({**row_base, "version_drift": snap_vd})

            # Pairwise insight & verdict entry between baseline and first candidate
            cand_id = next((str(s["id"]) for s in rel_subjs if str(s["id"]) != baseline_id), None)
            if comparable and cand_id and baseline_id in raw_common_valid_pct and cand_id in raw_common_valid_pct:
                b_val = raw_common_valid_pct[baseline_id]
                c_val = raw_common_valid_pct[cand_id]
                d_pp, r_pct = compute_unrounded_delta(b_val, c_val)
                a_wins = both_pass = both_fail = b_wins = 0
                for t in common_valid:
                    pa = float(ep_maps[baseline_id][t]["effective_reward"]) >= 1.0
                    pb = float(ep_maps[cand_id][t]["effective_reward"]) >= 1.0
                    if pa and not pb:
                        a_wins += 1
                    elif pa and pb:
                        both_pass += 1
                    elif not pa and not pb:
                        both_fail += 1
                    else:
                        b_wins += 1
                alias_a = next(b["alias"] for b in bound_batches if b["subject_id"] == baseline_id)
                alias_b = next(b["alias"] for b in bound_batches if b["subject_id"] == cand_id)
                auto_insights.append(
                    {
                        "text": (
                            f"在 {rel['bm_display_name']} 共同有效的 {n_valid} 道题上，"
                            f"{alias_a} ({round(b_val, 2):.2f}%) vs {alias_b} ({round(c_val, 2):.2f}%) "
                            f"差值 {d_pp:+.2f} pp（{a_wins} 胜 / {both_pass} 同过 / {both_fail} 同错 / {b_wins} 负；"
                            f"另 {fault_excluded} 题因运行故障未进入有效交集）。"
                        ),
                        "filter_link": {
                            "comparison_id": comparison_id,
                            "dataset_release_id": rel_id,
                            "diff_type": "a_wins",
                        },
                    }
                )
                status_v = "regressed" if d_pp < regression_threshold_pp else "pass"
                v_item = {
                    "benchmark_slug": rel["benchmark_slug"],
                    "dataset_release_id": rel_id,
                    "track_slug": rel["track_slug"],
                    "scoring_protocol": eff_proto,
                    "denominator_kind": "common_valid_scored",
                    "baseline_score": round(b_val, 2),
                    "candidate_score": round(c_val, 2),
                    "delta_pp": d_pp,
                    "rel_pct": r_pct,
                    "coverage": f"{n_valid}/{n_covered} common valid",
                    "status": status_v,
                    "reason": f"在 {n_valid} 道共同有效题上相对基线变化 {d_pp:+.2f} pp（未舍入相对变化 {r_pct:+.2f}%）"
                    if r_pct is not None
                    else f"在 {n_valid} 道共同有效题上相对基线变化 {d_pp:+.2f} pp",
                }
                if rel["goal_type"] == "optimize_gain":
                    verdict_agentic.append(v_item)
                elif rel["goal_type"] == "non_regression_guardrail":
                    verdict_guardrails.append(v_item)

        # Populate remaining core-14 benchmarks in verdict as undeterminable if not measured (A-14)
        cand_id_top = next((str(s["id"]) for s in subjects if str(s["id"]) != baseline_id), None)
        if include_verdict:
            measured_slugs = {v["benchmark_slug"] for v in verdict_agentic + verdict_guardrails}
            for bm in core_bms:
                meta = json.loads(bm["metadata_json"] or "{}")
                if not meta.get("core_14") or bm["benchmark_slug"] in measured_slugs:
                    continue
                issue = issues_by_benchmark.get(bm["benchmark_slug"])
                entry = self._unmeasured_verdict(bm, issue if issue != "missing_comparable_scores" else None)
                if bm["goal_type"] == "optimize_gain":
                    verdict_agentic.append(entry)
                elif bm["goal_type"] == "non_regression_guardrail":
                    verdict_guardrails.append(entry)

        macro = compute_comparative_macro_avg(subject_scores_for_macro)
        base_macro_raw = macro["by_subject"].get(baseline_id, {}).get("comparative_macro_avg_raw")
        by_subj_summary: dict[str, Any] = {}
        for sid, info in macro["by_subject"].items():
            d_pp = None
            if base_macro_raw is not None and info.get("comparative_macro_avg_raw") is not None:
                d_pp, _ = compute_unrounded_delta(base_macro_raw, info["comparative_macro_avg_raw"])
            s_full = self_full_avgs.get(sid) or []
            by_subj_summary[sid] = {
                "comparative_macro_avg": info["comparative_macro_avg"],
                "is_best": info["is_best"],
                "delta_pp": d_pp,
                "self_full_batch_avg": round(math.fsum(s_full) / len(s_full), 2) if s_full else None,
                "self_bench_count": info["self_bench_count"],
            }

        bound_snapshot = {
            "scoring_protocol": scoring_protocol,
            "align_mode": align_mode,
            "multi_attempt_agg": multi_attempt_agg,
            "regression_threshold_pp": regression_threshold_pp,
            "sample_set_id": (payload.get("scope") or {}).get("sample_set_id"),
            "batches": [
                {"subject_id": b["subject_id"], "batch_id": b["batch_id"], "generation_id": b["generation_id"]}
                for b in bound_batches
            ],
        }

        response = {
            "comparison_id": comparison_id,
            "methodology_version": METHODOLOGY_VERSION,
            "bound_snapshot": bound_snapshot,
            "gain_vs_guardrail_verdict": {
                "baseline_subject_id": baseline_id,
                "candidate_subject_id": cand_id_top,
                "regression_threshold_pp": regression_threshold_pp,
                "agentic_gains": verdict_agentic,
                "guardrails": verdict_guardrails,
            }
            if include_verdict
            else None,
            "auto_insights": auto_insights,
            "comparative_summary": {
                "common_comparable_benchmarks": macro["common_comparable_benchmarks"],
                "by_subject": by_subj_summary,
            },
            "rows": rows_out,
        }

        # Persist ephemeral bound comparison session for task-diff / export consistency (A-02).
        # Snapshot at rest NEVER persists holdout task_checksum values (v2.2.1 06-1 §2.2 & S-02).
        if persist_ephemeral:
            proj_id = bound_batches[0]["project_id"] if bound_batches else "default"
            now = utcnow_iso()
            snapshot_payload = {**response, "rows": rows_for_snapshot}
            with self.db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO saved_comparisons (
                        short_id, name, mode, project_id, owner_id, config_json, snapshot_json,
                        methodology_ver, created_at, updated_at
                    ) VALUES (?, ?, 'ephemeral', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        comparison_id,
                        f"Ephemeral {comparison_id}",
                        proj_id,
                        user.user_id,
                        json.dumps(
                            {
                                "payload": payload,
                                "bound_batches": bound_batches,
                                "sample_set_meta": sample_set_meta,
                                "frozen_sample_task_ids": sorted(allowed_task_ids) if allowed_task_ids is not None else None,
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(snapshot_payload, ensure_ascii=False),
                        METHODOLOGY_VERSION,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    DELETE FROM saved_comparisons
                    WHERE mode = 'ephemeral' AND short_id NOT IN (
                        SELECT short_id FROM saved_comparisons
                        WHERE mode = 'ephemeral'
                        ORDER BY created_at DESC LIMIT 300
                    )
                    """
                )
        return response

    def _resolve_legacy_benchmark_dir(
        self, benchmark_slug: str | None, dataset_release_id: str | None
    ) -> str | None:
        raw_slug = (benchmark_slug or "").strip()
        raw_rel = (dataset_release_id or "").strip()
        if raw_slug:
            validate_safe_id(raw_slug, "benchmark_slug")
        if raw_rel:
            validate_safe_id(raw_rel, "dataset_release_id")

        root = self.settings.legacy_root_dir
        if not root.is_dir():
            return None
        root_resolved = root.resolve()

        def _valid_dir(name: str) -> str | None:
            try:
                validate_safe_id(name, "benchmark_dir")
                cand = resolve_within_root(root, name)
            except AppError:
                return None
            if cand.is_dir() and cand.parent == root_resolved:
                return cand.name
            return None

        slug = raw_slug.lower()
        if not slug and raw_rel:
            slug = raw_rel.split("__", 1)[0].strip().lower()
        if slug in LEGACY_BENCHMARK_DIR_MAP:
            found = _valid_dir(LEGACY_BENCHMARK_DIR_MAP[slug])
            if found:
                return found
        if slug == "longmemeval" and raw_rel:
            rel_l = raw_rel.lower()
            for dname in ("LongMemEval_oracle_CoN", "LongMemEval_oracle_direct", "LongMemEval_S_CoN", "LongMemEval_S_direct"):
                if dname.lower().replace("_", "-") in rel_l or dname.lower() in rel_l:
                    found = _valid_dir(dname)
                    if found:
                        return found
        if slug:
            norm = slug.replace("-", "").replace("_", "")
            for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_dir() and entry.name.lower().replace("-", "").replace("_", "").startswith(norm):
                    found = _valid_dir(entry.name)
                    if found:
                        return found
        return None

    def _query_legacy_sample_diff(
        self,
        user: AuthUser,
        cmp_id: str,
        cfg: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Same-task paired diff for 2026-11-20 legacy_model comparisons (e.g., AA-LCR, CMMMU, MMMU-Pro)
        backed by real indexed `legacy_sample_offsets` (v2.2.1 A-14 & S-02).
        Strictly enforces:
        1) Source approval registry project_id/owner_id access on leaderboard.csv, target legacy dir, AND both model files;
        2) Unified holdout scope gate (`dataset:holdout_read`);
        3) Registered safe model_col identifiers constrained inside the authorized legacy directory (zero traversal/symlink escape)."""
        self._require_legacy_csv_access(user)
        subjs = (cfg.get("payload") or {}).get("subjects") or []
        if len(subjs) < 2:
            raise AppError(400, "INVALID_PATH_OR_ID", "历史模型对比对象不足 2 个")
        sub_a_id = str(payload.get("subject_a_id") or subjs[0]["id"])
        sub_b_id = str(payload.get("subject_b_id") or subjs[1]["id"])
        sub_a = next((s for s in subjs if str(s.get("id")) == sub_a_id), subjs[0])
        sub_b = next((s for s in subjs if str(s.get("id")) == sub_b_id), subjs[1])

        req_slug = payload.get("benchmark_slug")
        req_rel = payload.get("dataset_release_id")
        scoped_releases = ((cfg.get("payload") or {}).get("scope") or {}).get("dataset_release_ids") or []
        if scoped_releases:
            if not req_rel and len(scoped_releases) == 1:
                req_rel = scoped_releases[0]
            if req_rel not in scoped_releases:
                raise AppError(400, "INCOMPARABLE_PROTOCOL", "逐题回答请求超出已保存的发行版范围")
            release = self.db.fetchone(
                "SELECT benchmark_slug FROM dataset_releases WHERE dataset_release_id = ?", (req_rel,),
            )
            if release is None or (req_slug and req_slug != release["benchmark_slug"]):
                raise AppError(400, "INCOMPARABLE_PROTOCOL", "逐题回答请求与已绑定发行版不一致")
            req_slug = release["benchmark_slug"]
        if (
            is_holdout_dataset_or_batch(
                dataset_release_id=str(req_rel) if req_rel else None,
                benchmark_slug=str(req_slug) if req_slug else None,
            )
            or self._is_holdout_release(str(req_rel) if req_rel else None)
        ):
            require_scope(user, "dataset:holdout_read")

        bdir_name = self._resolve_legacy_benchmark_dir(req_slug, req_rel)
        if not bdir_name:
            raise AppError(404, "RESOURCE_NOT_FOUND", "该基准在 2026-11-20 中暂无逐题明细目录")
        validate_safe_id(bdir_name, "benchmark_dir")
        if is_holdout_dataset_or_batch(benchmark_dir=bdir_name):
            require_scope(user, "dataset:holdout_read")

        legacy_dir = resolve_within_root(self.settings.legacy_root_dir, bdir_name)
        if not legacy_dir.is_dir() or legacy_dir.parent != self.settings.legacy_root_dir.resolve():
            raise AppError(404, "RESOURCE_NOT_FOUND", "该基准在 2026-11-20 中暂无合法目录")

        # Enforce ACTUAL source directory provenance (exact same rule as QueryService.list_legacy_samples!)
        dir_proj, dir_owner, _, _ = self.approval_registry.lookup_full(legacy_dir)
        require_project_access(user, dir_proj, owner_id=dir_owner)

        registered_cols = self._registered_legacy_model_cols()

        def _pick_file(subj: dict[str, Any]) -> Path | None:
            cols = subj.get("model_cols") or ([subj["model_col"]] if subj.get("model_col") else [])
            if isinstance(cols, str):
                cols = [c.strip() for c in cols.split(",") if c.strip()]
            if not cols:
                raise AppError(400, "INVALID_PATH_OR_ID", "历史模型对比对象缺少已登记的 model_col")
            for raw_col in cols:
                col = str(raw_col).strip()
                validate_safe_id(col, "model_col")
                if col not in registered_cols:
                    raise AppError(400, "INVALID_PATH_OR_ID", f"未登记的历史模型文件标识: {col}")
            for raw_col in cols:
                col = str(raw_col).strip()
                for ext in (".json", ".jsonl"):
                    fname = f"{col}{ext}"
                    validate_safe_id(fname, "model_file")
                    cand = resolve_within_root(legacy_dir, fname)
                    if cand.parent != legacy_dir:
                        raise AppError(400, "INVALID_PATH_OR_ID", "模型文件路径超出授权目录")
                    if cand.is_file():
                        f_proj, f_owner, _, _ = self.approval_registry.lookup_full(cand)
                        require_project_access(user, f_proj, owner_id=f_owner)
                        return cand
            return None

        file_a = _pick_file(sub_a)
        file_b = _pick_file(sub_b)
        if file_a is None or file_b is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"目录 {bdir_name} 中未同时找到两组对象的逐题明细文件")

        def _load_offsets(fp: Path) -> dict[str, dict[str, Any]]:
            rows = self.db.fetchall(
                "SELECT sample_id, status, reward, category, difficulty FROM legacy_sample_offsets WHERE file_path = ?",
                (str(fp),),
            )
            return {str(r["sample_id"]): r for r in rows}

        map_a = _load_offsets(file_a)
        map_b = _load_offsets(file_b)

        # If cold indexer hasn't indexed this directory yet and DB is writable, index just this directory on demand!
        if (not map_a or not map_b) and not self.db.read_only_mode:
            from app.indexing.cold_indexer import ColdIndexer

            ci = ColdIndexer(self.settings, self.db)
            if file_a.suffix == ".jsonl" or file_b.suffix == ".jsonl":
                ci.index_legacy_jsonl_offsets(bdir_name, force=False)
            if file_a.suffix == ".json" or file_b.suffix == ".json":
                ci.index_legacy_text_json_offsets(bdir_name, force=False)
            map_a = _load_offsets(file_a)
            map_b = _load_offsets(file_b)

        common_covered = sorted(set(map_a.keys()) & set(map_b.keys()))
        common_valid = [
            sid
            for sid in common_covered
            if map_a[sid]["status"] == "scored"
            and map_b[sid]["status"] == "scored"
            and finite(map_a[sid]["reward"])
            and finite(map_b[sid]["reward"])
        ]
        fault_excluded = len(common_covered) - len(common_valid)
        diff_filter = payload.get("filter", "all")

        a_wins = both_pass = both_fail = b_wins = 0
        items: list[dict[str, Any]] = []
        per_sample_score_absent = len(common_covered) > 0 and len(common_valid) == 0

        if per_sample_score_absent:
            # Honest fallback for benchmarks like AA-LCR-v1.1 whose raw JSON stores paired predictions
            # without per-sample numeric judge scores (Invariant #1: reward stays null, never faked!)
            for sid in common_covered:
                items.append(
                    {
                        "task_id": sid,
                        "task_checksum_match": True,
                        "diff_type": "unscored_pair",
                        "a": {
                            "batch_id": bdir_name,
                            "trial_id": sid,
                            "model_file": file_a.name,
                            "status": map_a[sid]["status"],
                            "reward": None,
                            "duration_sec": None,
                            "tool_calls": None,
                        },
                        "b": {
                            "batch_id": bdir_name,
                            "trial_id": sid,
                            "model_file": file_b.name,
                            "status": map_b[sid]["status"],
                            "reward": None,
                            "duration_sec": None,
                            "tool_calls": None,
                        },
                    }
                )
        else:
            for sid in common_valid:
                ra = float(map_a[sid]["reward"])
                rb = float(map_b[sid]["reward"])
                pa = ra >= 1.0
                pb = rb >= 1.0
                if pa and not pb:
                    a_wins += 1
                    kind = "a_wins"
                elif pa and pb:
                    both_pass += 1
                    kind = "both_pass"
                elif not pa and not pb:
                    both_fail += 1
                    kind = "both_fail"
                else:
                    b_wins += 1
                    kind = "b_wins"
                if diff_filter == "divergent" and kind not in ("a_wins", "b_wins"):
                    continue
                if diff_filter in ("a_wins", "b_wins", "both_pass", "both_fail") and kind != diff_filter:
                    continue
                items.append(
                    {
                        "task_id": sid,
                        "task_checksum_match": True,
                        "diff_type": kind,
                        "a": {
                            "batch_id": bdir_name,
                            "trial_id": sid,
                            "model_file": file_a.name,
                            "status": map_a[sid]["status"],
                            "reward": ra,
                            "duration_sec": None,
                            "tool_calls": None,
                        },
                        "b": {
                            "batch_id": bdir_name,
                            "trial_id": sid,
                            "model_file": file_b.name,
                            "status": map_b[sid]["status"],
                            "reward": rb,
                            "duration_sec": None,
                            "tool_calls": None,
                        },
                    }
                )

        page = int(payload.get("page", 1))
        page_size = int(payload.get("page_size", 50))
        offset = max(0, (page - 1) * page_size)
        return {
            "comparison_id": cmp_id,
            "kind": "legacy_sample_diff",
            "benchmark_dir": bdir_name,
            "model_file_a": file_a.name,
            "model_file_b": file_b.name,
            "per_sample_score_absent": per_sample_score_absent,
            "scoring_protocol": "legacy-20261120-csv",
            "intersection": {
                "common_covered_tasks": len(common_covered),
                "common_valid_scored_tasks": len(common_valid),
            },
            "win_tie_loss": {
                "a_wins": a_wins,
                "both_pass": both_pass,
                "both_fail": both_fail,
                "b_wins": b_wins,
                "fault_excluded": 0 if per_sample_score_absent else fault_excluded,
            },
            "items": items[offset : offset + page_size],
            "total": len(items),
            "page": page,
            "page_size": page_size,
            "methodology_version": METHODOLOGY_VERSION,
        }

    def query_task_diff(self, user: AuthUser, payload: dict[str, Any]) -> dict[str, Any]:
        """Consume bound comparison_id so task-diff counts match compare/query 100% (A-02).
        - Locks N-way intersection across ALL subjects participating in the same release inside ONE
          consistent SQLite MVCC read snapshot (e.g. 3-way DeepSWE stays 39 valid / 48 covered!).
        - Freezes custom sample_set task_ids for snapshot/ephemeral comparisons while ALWAYS enforcing
          live project authorization (S-02 & A-02).
        - Supports paired sample diff for 2026-11-20 legacy_model comparisons (A-14)."""
        cmp_id = payload.get("comparison_id")
        if not cmp_id:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "必须传入绑定快照的 comparison_id")

        legacy_dispatch: tuple[dict[str, Any], dict[str, Any]] | None = None
        with self.db.read_snapshot() as snap_conn:
            saved_row = snap_conn.execute(
                "SELECT * FROM saved_comparisons WHERE short_id = ?", (cmp_id,)
            ).fetchone()
            if saved_row is None:
                raise AppError(404, "RESOURCE_NOT_FOUND", f"对比会话不存在: {cmp_id}")
            saved = dict(saved_row)
            require_project_access(user, saved["project_id"], owner_id=saved["owner_id"])

            cfg = json.loads(saved["config_json"])
            bound = cfg.get("bound_batches") or []
            payload_subjs = (cfg.get("payload") or {}).get("subjects") or []
            is_legacy_cmp = (
                not bound
                and (
                    bool(cfg.get("requires_default_project") or cfg.get("requires_legacy_csv_access"))
                    or (bool(payload_subjs) and all(str(s.get("kind") or "") == "legacy_model" for s in payload_subjs))
                )
            )
            if is_legacy_cmp:
                legacy_dispatch = (cfg, payload)
            else:
                if len(bound) < 2:
                    raise AppError(400, "INVALID_PATH_OR_ID", "无效的对比对象标识")

                sub_a_id = str(payload.get("subject_a_id") or bound[0]["subject_id"])
                sub_b_id = str(payload.get("subject_b_id") or bound[1]["subject_id"])
                sub_a = next((b for b in bound if b["subject_id"] == sub_a_id), None)
                sub_b = next((b for b in bound if b["subject_id"] == sub_b_id), None)
                if not sub_a or not sub_b:
                    raise AppError(400, "INVALID_PATH_OR_ID", "无效的对比对象标识")

                # Authorize & generation-check EVERY participating bound batch on the SAME MVCC read snapshot!
                live_by_sid: dict[str, dict[str, Any]] = {}
                for ref_meta in bound:
                    live_b = self._load_authorized_batch(user, ref_meta["batch_id"], conn=snap_conn)
                    bound_gen = ref_meta.get("generation_id")
                    if bound_gen and live_b["generation_id"] != bound_gen:
                        raise AppError(
                            409,
                            "GENERATION_MISMATCH",
                            f"批次 {live_b['batch_id']} 已更新至新发布代，拒绝用新代明细拼接旧 comparison_id，请重新查询对比",
                            {
                                "batch_id": live_b["batch_id"],
                                "bound_generation_id": bound_gen,
                                "current_generation_id": live_b["generation_id"],
                            },
                        )
                    live_by_sid[ref_meta["subject_id"]] = live_b

                ba = live_by_sid[sub_a_id]
                bb = live_by_sid[sub_b_id]
                rel_a = ba.get("dataset_release_id")
                rel_b = bb.get("dataset_release_id")
                bound_rel_a = sub_a.get("dataset_release_id")
                bound_rel_b = sub_b.get("dataset_release_id")
                if (bound_rel_a and rel_a != bound_rel_a) or (bound_rel_b and rel_b != bound_rel_b):
                    raise AppError(
                        409,
                        "GENERATION_MISMATCH",
                        "批次绑定的数据集版本已变更，拒绝跨版本拼接旧 comparison_id，请重新查询对比",
                    )

                req_rel = payload.get("dataset_release_id")
                if not rel_a or not rel_b or rel_a != rel_b:
                    raise AppError(
                        400,
                        "INCOMPARABLE_PROTOCOL",
                        "对比双方不属于同一数据集版本（dataset_release_id 不一致），拒绝跨版本逐题对比",
                        {
                            "subject_a_release": rel_a,
                            "subject_b_release": rel_b,
                            "requested_release": req_rel,
                        },
                    )
                if req_rel and str(req_rel) != str(rel_a):
                    raise AppError(
                        400,
                        "INCOMPARABLE_PROTOCOL",
                        f"请求的数据集版本 {req_rel} 与对比双方绑定版本 {rel_a} 不一致，拒绝跨版本逐题对比",
                        {
                            "requested_release": req_rel,
                            "bound_release": rel_a,
                        },
                    )

                target_rel = str(rel_a)
                req_proto = (cfg.get("payload") or {}).get("scoring_protocol")
                scoring_protocol = req_proto or ba["scoring_protocol"]
                align_mode = (cfg.get("payload") or {}).get("align_mode", "intersection")

                # Enforce strict scoring protocol comparability matching query_compare (A-02 / A-11)
                pair_protos = {str(ba.get("scoring_protocol") or ""), str(bb.get("scoring_protocol") or "")} - {""}
                if not self._protocols_compatible(pair_protos, req_proto, ba.get("benchmark_slug")):
                    raise AppError(
                        400,
                        "INCOMPARABLE_PROTOCOL",
                        "对比双方评分协议不一致，拒绝返回逐题胜负明细",
                        {
                            "subject_a_protocol": ba.get("scoring_protocol"),
                            "subject_b_protocol": bb.get("scoring_protocol"),
                            "requested_protocol": req_proto,
                        },
                    )

                # Enforce 100% consistency with the frozen comparison matrix row comparability (A-02 / A-11)
                if saved.get("snapshot_json"):
                    snap_obj = json.loads(saved["snapshot_json"] or "{}")
                    snap_rows = snap_obj.get("rows") or []
                    if snap_rows:
                        matched_row = next((r for r in snap_rows if r.get("dataset_release_id") == target_rel), None)
                        if matched_row is None:
                            raise AppError(
                                400,
                                "INCOMPARABLE_PROTOCOL",
                                f"数据集版本 {target_rel} 不在当前冻结对比范围内，拒绝返回逐题明细",
                                {"dataset_release_id": target_rel},
                            )
                        row_cells = matched_row.get("cells") or {}
                        if not matched_row.get("comparable") or sub_a_id not in row_cells or sub_b_id not in row_cells:
                            raise AppError(
                                400,
                                "INCOMPARABLE_PROTOCOL",
                                f"数据集版本 {target_rel} 在对比矩阵中为不可比，拒绝返回逐题胜负明细",
                                {"dataset_release_id": target_rel, "comparable": bool(matched_row.get("comparable"))},
                            )

                # Enforce sample_set authorization ALWAYS (even after edit/delete), and bind frozen task_ids
                # for snapshot/ephemeral comparisons so editing/deleting the sample set NEVER mutates frozen results!
                ss_id = ((cfg.get("payload") or {}).get("scope") or {}).get("sample_set_id")
                ss_meta = cfg.get("sample_set_meta")
                frozen_tids = cfg.get("frozen_sample_task_ids")
                allow_tids: set[str] | None = None
                if ss_id or isinstance(ss_meta, dict) or frozen_tids is not None:
                    ss_lookup_id = str(ss_id or (ss_meta.get("set_id") if isinstance(ss_meta, dict) else ""))
                    ss_r = (
                        snap_conn.execute("SELECT * FROM sample_sets WHERE set_id = ?", (ss_lookup_id,)).fetchone()
                        if ss_lookup_id
                        else None
                    )
                    ss_rel_check: str | None = None
                    if ss_r is not None:
                        require_project_access(user, ss_r["project_id"], owner_id=ss_r["owner_id"])
                        ss_rel_check = ss_r["dataset_release_id"]
                        live_ss_tids = {str(t) for t in json.loads(ss_r["task_ids_json"] or "[]")}
                        if self._contains_holdout_tasks(ss_rel_check, live_ss_tids, conn=snap_conn):
                            require_scope(user, "dataset:holdout_read")
                    elif isinstance(ss_meta, dict) and ss_meta.get("project_id"):
                        require_project_access(user, ss_meta["project_id"], owner_id=ss_meta.get("owner_id"))
                        ss_rel_check = ss_meta.get("dataset_release_id")
                        if self._contains_holdout_tasks(ss_rel_check, frozen_tids, conn=snap_conn):
                            require_scope(user, "dataset:holdout_read")

                    if saved["mode"] in ("snapshot", "ephemeral") and frozen_tids is not None:
                        allow_tids = {str(t) for t in frozen_tids}
                        if self._contains_holdout_tasks(ss_rel_check or target_rel, allow_tids, conn=snap_conn):
                            require_scope(user, "dataset:holdout_read")
                    elif ss_r is not None:
                        allow_tids = {str(t) for t in json.loads(ss_r["task_ids_json"] or "[]")}
                    else:
                        raise AppError(404, "RESOURCE_NOT_FOUND", f"自定义样本集已被删除: {ss_lookup_id}")

                # Select ONLY bound subjects strictly belonging to target_rel (NEVER re-append out-of-release subjects!)
                rel_refs = [
                    ref_meta
                    for ref_meta in bound
                    if live_by_sid[ref_meta["subject_id"]].get("dataset_release_id") == target_rel
                ]
                if sub_a not in rel_refs or sub_b not in rel_refs:
                    raise AppError(
                        400,
                        "INCOMPARABLE_PROTOCOL",
                        "对比双方未共同属于同一绑定数据集版本，拒绝跨版本逐题对比",
                        {"dataset_release_id": target_rel},
                    )
                if align_mode == "intersection":
                    rel_protos = {
                        str(live_by_sid[r["subject_id"]].get("scoring_protocol") or "")
                        for r in rel_refs
                        if live_by_sid[r["subject_id"]].get("scoring_protocol")
                    }
                    if not self._protocols_compatible(rel_protos, req_proto, ba.get("benchmark_slug")):
                        raise AppError(
                            400,
                            "INCOMPARABLE_PROTOCOL",
                            "交集参与批次评分协议不一致，拒绝返回逐题胜负明细",
                            {"dataset_release_id": target_rel, "protocols": sorted(rel_protos)},
                        )

                # Enforce unified holdout gate across ALL actual participating releases & batches (v2.2.1 06-1 §2.2 & S-02)
                actual_releases = {
                    str(live_by_sid[ref_meta["subject_id"]].get("dataset_release_id"))
                    for ref_meta in rel_refs
                    if live_by_sid[ref_meta["subject_id"]].get("dataset_release_id")
                }
                actual_releases.add(target_rel)
                is_holdout_cmp = any(
                    self._is_holdout_release(rid, conn=snap_conn) for rid in actual_releases
                ) or any(
                    self._is_holdout_batch(live_by_sid[ref_meta["subject_id"]], conn=snap_conn) for ref_meta in rel_refs
                )
                if is_holdout_cmp:
                    require_scope(user, "dataset:holdout_read")
                holdout_tids = (
                    set()
                    if has_scope(user, "dataset:holdout_read")
                    else self._holdout_task_ids_for_releases(actual_releases, conn=snap_conn)
                )

                all_emaps: dict[str, dict[str, dict[str, Any]]] = {}
                batch_id_by_sid: dict[str, str] = {}
                for ref_meta in rel_refs:
                    sid = ref_meta["subject_id"]
                    live_b = live_by_sid[sid]
                    batch_id_by_sid[sid] = live_b["batch_id"]
                    b_gen = ref_meta.get("generation_id") or live_b["generation_id"]
                    emap_raw = self._get_batch_episode_map(
                        live_b["batch_id"],
                        scoring_protocol,
                        conn=snap_conn,
                        expected_generation_id=b_gen,
                    )
                    # Defensive check: non-empty recorded batch with zero matching bound-generation episodes -> 409
                    if int(live_b.get("recorded") or 0) > 0 and not emap_raw:
                        raise AppError(
                            409,
                            "GENERATION_MISMATCH",
                            f"批次 {live_b['batch_id']} 绑定发布代 {ref_meta.get('generation_id')} 明细已不可用，请重新查询对比",
                            {
                                "batch_id": live_b["batch_id"],
                                "bound_generation_id": ref_meta.get("generation_id"),
                                "current_generation_id": live_b["generation_id"],
                            },
                        )
                    if allow_tids is not None:
                        emap_raw = {t: ep for t, ep in emap_raw.items() if t in allow_tids}
                    all_emaps[sid] = emap_raw

        if legacy_dispatch is not None:
            return self._query_legacy_sample_diff(user, cmp_id, legacy_dispatch[0], legacy_dispatch[1])

        emap_a = all_emaps[sub_a_id]
        emap_b = all_emaps[sub_b_id]
        active_sids = (
            [ref_meta["subject_id"] for ref_meta in rel_refs]
            if (align_mode == "intersection" and len(all_emaps) >= 2)
            else [sub_a_id, sub_b_id]
        )
        (
            common_covered,
            common_valid,
            drift_task_ids,
            drift_detail_groups,
            _,
        ) = self._compute_aligned_task_sets(all_emaps, active_sids, batch_id_by_sid=batch_id_by_sid)

        hidden_holdout_tasks = 0
        if holdout_tids and not has_scope(user, "dataset:holdout_read"):
            all_participating_tids = set(common_covered) | set(drift_task_ids)
            hidden_holdout_tasks = len(all_participating_tids & holdout_tids)
            if (common_valid or drift_task_ids) and all(t in holdout_tids for t in (set(common_valid) | set(drift_task_ids))):
                require_scope(user, "dataset:holdout_read")
            common_covered = [t for t in common_covered if t not in holdout_tids]
            common_valid = [t for t in common_valid if t not in holdout_tids]
            drift_task_ids = [t for t in drift_task_ids if t not in holdout_tids]
            drift_detail_groups = [g for g in drift_detail_groups if g["task_id"] not in holdout_tids]

        fault_excluded = len(common_covered) - len(common_valid)

        a_wins = both_pass = both_fail = b_wins = 0
        diff_filter = payload.get("filter", "all")
        items = []
        for t in common_valid:
            ea, eb = emap_a[t], emap_b[t]
            pa = float(ea["effective_reward"]) >= 1.0
            pb = float(eb["effective_reward"]) >= 1.0
            if pa and not pb:
                a_wins += 1
                kind = "a_wins"
            elif pa and pb:
                both_pass += 1
                kind = "both_pass"
            elif not pa and not pb:
                both_fail += 1
                kind = "both_fail"
            else:
                b_wins += 1
                kind = "b_wins"

            if diff_filter == "divergent" and kind not in ("a_wins", "b_wins"):
                continue
            if diff_filter in ("a_wins", "b_wins", "both_pass", "both_fail") and kind != diff_filter:
                continue

            items.append(
                {
                    "task_id": t,
                    "task_checksum": ea.get("task_checksum"),
                    "task_checksum_match": True,
                    "diff_type": kind,
                    "a": {
                        "batch_id": ba["batch_id"],
                        "trial_id": ea["trial_id"],
                        "status": ea["status"],
                        "reward": ea["effective_reward"],
                        "duration_sec": ea["duration_sec"],
                        "tool_calls": ea["tool_call_count"],
                    },
                    "b": {
                        "batch_id": bb["batch_id"],
                        "trial_id": eb["trial_id"],
                        "status": eb["status"],
                        "reward": eb["effective_reward"],
                        "duration_sec": eb["duration_sec"],
                        "tool_calls": eb["tool_call_count"],
                    },
                }
            )

        page = int(payload.get("page", 1))
        page_size = int(payload.get("page_size", 50))
        offset = max(0, (page - 1) * page_size)
        return {
            "comparison_id": cmp_id,
            "dataset_release_id": target_rel,
            "scoring_protocol": scoring_protocol,
            "intersection": {
                "common_covered_tasks": len(common_covered),
                "common_valid_scored_tasks": len(common_valid),
            },
            "win_tie_loss": {
                "a_wins": a_wins,
                "both_pass": both_pass,
                "both_fail": both_fail,
                "b_wins": b_wins,
                "fault_excluded": fault_excluded,
            },
            "checksum_mismatch_tasks": len(drift_task_ids),
            "version_drift_warning": len(drift_detail_groups) > 0,
            "checksum_drift_groups": drift_detail_groups,
            "redacted": hidden_holdout_tasks > 0,
            "hidden_holdout_tasks": hidden_holdout_tasks,
            "redacted_reason": (
                f"缺少 dataset:holdout_read 权限，已隐藏 {hidden_holdout_tasks} 道留出集题目明细"
                if hidden_holdout_tasks > 0
                else None
            ),
            "items": items[offset : offset + page_size],
            "total": len(items),
            "page": page,
            "page_size": page_size,
            "methodology_version": METHODOLOGY_VERSION,
        }

    def save_comparison(
        self,
        user: AuthUser,
        *,
        name: str,
        mode: str,
        project_id: str,
        comparison_id: str,
    ) -> dict[str, Any]:
        if mode not in ("live", "snapshot"):
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "mode 必须为 live 或 snapshot")
        require_project_access(user, project_id, owner_id=user.user_id)
        src = self.db.fetchone("SELECT * FROM saved_comparisons WHERE short_id = ?", (comparison_id,))
        if src is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", "源对比会话不存在")
        require_project_access(user, src["project_id"], owner_id=src["owner_id"])
        # Re-verify user still has live access to all bound batches & legacy/sample-set sources before saving
        src_cfg = json.loads(src["config_json"] or "{}")
        if src_cfg.get("requires_default_project") or src_cfg.get("requires_legacy_csv_access"):
            self._require_legacy_csv_access(user)
        for b_ref in src_cfg.get("bound_batches") or []:
            self._load_authorized_batch(user, b_ref["batch_id"])
        ss_meta = src_cfg.get("sample_set_meta")
        if isinstance(ss_meta, dict) and ss_meta.get("project_id"):
            require_project_access(user, ss_meta["project_id"], owner_id=ss_meta.get("owner_id"))
            if self._contains_holdout_tasks(ss_meta.get("dataset_release_id"), src_cfg.get("frozen_sample_task_ids")):
                require_scope(user, "dataset:holdout_read")

        short_id = f"s_{secrets.token_hex(6)}"
        now = utcnow_iso()
        save_cfg = dict(src_cfg)
        if mode == "snapshot":
            # Ensure sample set task IDs are strictly frozen in snapshot config
            ss_id_src = ((save_cfg.get("payload") or {}).get("scope") or {}).get("sample_set_id")
            if ss_id_src and save_cfg.get("frozen_sample_task_ids") is None:
                ss_live = self.db.fetchone("SELECT * FROM sample_sets WHERE set_id = ?", (str(ss_id_src),))
                if ss_live is not None:
                    require_project_access(user, ss_live["project_id"], owner_id=ss_live["owner_id"])
                    save_cfg["frozen_sample_task_ids"] = sorted(str(t) for t in json.loads(ss_live["task_ids_json"] or "[]"))
                    save_cfg["sample_set_meta"] = {
                        "set_id": str(ss_id_src),
                        "project_id": ss_live["project_id"],
                        "owner_id": ss_live["owner_id"],
                        "dataset_release_id": ss_live.get("dataset_release_id"),
                    }
            snap_obj = json.loads(src["snapshot_json"] or "{}")
            snap_obj["comparison_id"] = short_id
            save_snap_json: str | None = json.dumps(snap_obj, ensure_ascii=False)
        else:
            save_cfg.pop("frozen_sample_task_ids", None)
            save_snap_json = None

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO saved_comparisons (
                    short_id, name, mode, project_id, owner_id, config_json, snapshot_json,
                    methodology_ver, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    short_id,
                    name,
                    mode,
                    project_id,
                    user.user_id,
                    json.dumps(save_cfg, ensure_ascii=False),
                    save_snap_json,
                    METHODOLOGY_VERSION,
                    now,
                    now,
                ),
            )
        return {"short_id": short_id, "name": name, "mode": mode, "project_id": project_id, "created_at": now}

    def open_saved_comparison(self, user: AuthUser, short_id: str) -> dict[str, Any]:
        """Re-verify project membership AND participating batches on EVERY open — even frozen snapshots (S-02)!"""
        row = self.db.fetchone("SELECT * FROM saved_comparisons WHERE short_id = ?", (short_id,))
        if row is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", "对比短链不存在")
        # 1. Check project membership on saved_comparison itself (NO owner_id bypass for team projects!)
        require_project_access(user, row["project_id"], owner_id=row["owner_id"])

        cfg = json.loads(row["config_json"])
        bound = cfg.get("bound_batches") or []
        payload_subjs = (cfg.get("payload") or {}).get("subjects") or []
        is_legacy_cmp = bool(cfg.get("requires_default_project") or cfg.get("requires_legacy_csv_access")) or (
            bool(payload_subjs) and all(str(s.get("kind") or "") == "legacy_model" for s in payload_subjs)
        )
        if is_legacy_cmp:
            self._require_legacy_csv_access(user)
        ss_meta = cfg.get("sample_set_meta")
        frozen_tids = cfg.get("frozen_sample_task_ids")
        if isinstance(ss_meta, dict) and ss_meta.get("project_id"):
            ss_live = self.db.fetchone(
                "SELECT project_id, owner_id, dataset_release_id, task_ids_json FROM sample_sets WHERE set_id = ?",
                (ss_meta.get("set_id"),),
            )
            if ss_live is not None:
                require_project_access(user, ss_live["project_id"], owner_id=ss_live["owner_id"])
                live_tids = json.loads(ss_live["task_ids_json"] or "[]")
                if self._contains_holdout_tasks(ss_live.get("dataset_release_id"), live_tids) or self._contains_holdout_tasks(
                    ss_live.get("dataset_release_id"), frozen_tids
                ):
                    require_scope(user, "dataset:holdout_read")
            else:
                require_project_access(user, ss_meta["project_id"], owner_id=ss_meta.get("owner_id"))
                if self._contains_holdout_tasks(ss_meta.get("dataset_release_id"), frozen_tids):
                    require_scope(user, "dataset:holdout_read")
        # 2. Re-verify viewer still has access to EVERY participating batch!
        for b_ref in bound:
            b_live = self.db.fetchone("SELECT project_id, owner_id FROM batches WHERE batch_id = ?", (b_ref["batch_id"],))
            if b_live is not None:
                if not can_access_project(user, b_live["project_id"], owner_id=b_live["owner_id"]):
                    raise AppError(
                        403,
                        "PROJECT_FORBIDDEN",
                        "您已无权访问该快照包含的批次所属项目",
                        {"project_id": b_live["project_id"]},
                    )
            else:
                # Batch removed; still check recorded project_id
                if not can_access_project(user, b_ref.get("project_id", row["project_id"]), owner_id=b_ref.get("owner_id")):
                    raise AppError(403, "PROJECT_FORBIDDEN", "您已无权访问该快照所属项目")

        if row["mode"] == "live":
            payload = cfg["payload"]
            # The selection guard applies only to the initial request. Live links
            # intentionally recompute the selected batches against their current generations.
            live_subjects = [
                {key: value for key, value in subject.items() if key != "expected_generation_id"}
                for subject in payload["subjects"]
            ]
            result = self.query_compare(user, {**payload, "subjects": live_subjects})
        else:
            result = json.loads(row["snapshot_json"] or "{}")
            result["comparison_id"] = row["short_id"]
            self._filter_compare_result_version_drift(user, result, cfg)

        # Strip restricted references if viewer lacks scopes
        if not has_scope(user, "trace:read"):
            result["trace_links_redacted"] = True
        if not has_scope(user, "direct_vlm_qa:read"):
            result["qa_links_redacted"] = True
        if not has_scope(user, "dataset:holdout_read"):
            result["holdout_details_redacted"] = True

        return {
            "short_id": row["short_id"],
            "name": row["name"],
            "mode": row["mode"],
            "project_id": row["project_id"],
            "methodology_version": row["methodology_ver"],
            "config": {
                "payload": cfg.get("payload"),
                "bound_batches": bound,
                "sample_set_meta": ss_meta,
            },
            "result": result,
        }

    # --- Legacy / 3-Track Model Comparison (2026-11-20 leaderboard.csv + Core-14 Verdict) ---

    def _query_compare_legacy_models(
        self,
        user: AuthUser,
        payload: dict[str, Any],
        *,
        persist_ephemeral: bool = True,
    ) -> dict[str, Any]:
        """Compare 2+ legacy 2026-11-20 model columns (or combined text+vision column sets) across
        all 3 tracks (Agentic / LLM-Text / Vision-Image) + Extended, strictly authorized by registry provenance."""
        self._require_legacy_csv_access(user)
        subjects = payload.get("subjects") or []
        baseline_id = str(payload.get("baseline_subject_id") or subjects[0]["id"])
        regression_threshold_pp = float(payload.get("regression_threshold_pp", -1.0))
        include_verdict = bool(payload.get("include_verdict", True))
        scope = payload.get("scope") or {}
        scope_tracks = set(scope.get("tracks") or [])
        scope_release_ids = set(scope.get("dataset_release_ids") or [])
        metric = scope.get("legacy_metric")
        scale = scope.get("legacy_scale_type")

        # Load legacy leaderboard rows + benchmark metadata
        all_leg = self.db.fetchall(
            """
            SELECT lr.*, bm.display_name AS bm_display_name, bm.track_slug, bm.goal_type, bm.status AS bm_status
            FROM legacy_leaderboard_rows lr
            JOIN benchmarks bm ON bm.benchmark_slug = lr.benchmark_slug
            WHERE lr.dataset_release_id IS NOT NULL
            """
        )
        registered_cols = self._registered_legacy_model_cols()
        by_col: dict[str, dict[str, dict[str, Any]]] = {}
        rel_info: dict[str, dict[str, Any]] = {}
        for r in all_leg:
            col = str(r["model_col"])
            rel_id = str(r["dataset_release_id"])
            if (
                (scope_release_ids and rel_id not in scope_release_ids)
                or (metric is not None and r["metric"] != metric)
                or (scale is not None and r["scale_type"] != scale)
            ):
                continue
            by_col.setdefault(col, {})[rel_id] = r
            rel_info[rel_id] = r

        subj_scores_map: dict[str, dict[str, dict[str, Any]]] = {}
        subj_aliases: dict[str, str] = {}
        for s in subjects:
            sid = str(s["id"])
            cols = s.get("model_cols") or ([s["model_col"]] if s.get("model_col") else [])
            if isinstance(cols, str):
                cols = [c.strip() for c in cols.split(",") if c.strip()]
            if not cols:
                raise AppError(400, "INVALID_PATH_OR_ID", "历史模型对比对象缺少已登记的 model_col")
            merged: dict[str, dict[str, Any]] = {}
            for raw_col in cols:
                col_s = str(raw_col).strip()
                validate_safe_id(col_s, "model_col")
                if col_s not in registered_cols:
                    raise AppError(400, "INVALID_PATH_OR_ID", f"未登记的历史模型标识: {col_s}")
                for rel_id, row in by_col.get(col_s, {}).items():
                    merged[rel_id] = row
            subj_scores_map[sid] = merged
            subj_aliases[sid] = str(s.get("alias") or " + ".join(cols) or sid)

        all_rel_ids = sorted({rel_id for m in subj_scores_map.values() for rel_id in m.keys()})
        if scope_tracks:
            all_rel_ids = [r for r in all_rel_ids if rel_info[r]["track_slug"] in scope_tracks]

        comparison_id = f"cmp_{secrets.token_hex(8)}"
        rows_out: list[dict[str, Any]] = []
        subject_scores_for_macro: dict[str, dict[str, tuple[float | None, str | None]]] = {str(s["id"]): {} for s in subjects}
        verdict_agentic: list[dict[str, Any]] = []
        verdict_guardrails: list[dict[str, Any]] = []
        auto_insights: list[dict[str, Any]] = []
        cand_id = next((str(s["id"]) for s in subjects if str(s["id"]) != baseline_id), None)

        for rel_id in all_rel_ids:
            meta = rel_info[rel_id]
            metrics: set[str] = set()
            raw_scores: dict[str, float] = {}
            cells: dict[str, dict[str, Any]] = {}
            for s in subjects:
                sid = str(s["id"])
                entry = subj_scores_map[sid].get(rel_id)
                if entry is not None:
                    metrics.add(entry["metric"])
                    sc = float(entry["norm_score_100"])
                    raw_scores[sid] = sc
                    cells[sid] = {
                        "common_valid": {
                            "score": round(sc, 2),
                            "raw_score": entry["raw_score"],
                            "passed": None,
                            "denominator": None,
                            "is_best": False,
                            "delta_pp": None,
                            "rel_pct": None,
                        },
                        "common_covered_faults_as_zero": {"enabled_by_protocol": False},
                        "self_full_batch_valid": {
                            "score": round(sc, 2),
                            "valid_scored": None,
                            "captured_tasks": None,
                            "split_total": None,
                        },
                    }
                    subject_scores_for_macro[sid][rel_id] = (sc, "legacy-20261120-csv")
                else:
                    cells[sid] = {
                        "common_valid": {
                            "score": None,
                            "passed": None,
                            "denominator": None,
                            "is_best": False,
                            "delta_pp": None,
                            "rel_pct": None,
                        },
                        "common_covered_faults_as_zero": {"enabled_by_protocol": False},
                        "self_full_batch_valid": {"score": None, "valid_scored": None, "captured_tasks": None, "split_total": None},
                    }
                    subject_scores_for_macro[sid][rel_id] = (None, None)

            comparable = len(raw_scores) == len(subjects)
            if comparable and raw_scores:
                best_v = max(raw_scores.values())
                base_v = raw_scores.get(baseline_id)
                for sid, sc in raw_scores.items():
                    if abs(sc - best_v) < 1e-9:
                        cells[sid]["common_valid"]["is_best"] = True
                    if base_v is not None:
                        d_pp, r_pct = compute_unrounded_delta(base_v, sc)
                        cells[sid]["common_valid"]["delta_pp"] = d_pp
                        cells[sid]["common_valid"]["rel_pct"] = r_pct

            rows_out.append(
                {
                    "benchmark_slug": meta["benchmark_slug"],
                    "dataset_release_id": rel_id,
                    "raw_dataset_name": meta["bm_display_name"] or meta["benchmark"],
                    "track_slug": meta["track_slug"],
                    "goal_type": meta["goal_type"],
                    "scoring_protocol": "legacy-20261120-csv",
                    "legacy_metric": next(iter(metrics)) if len(metrics) == 1 else None,
                    "comparable": comparable,
                    "intersection": {
                        "common_covered_tasks": None,
                        "common_valid_scored_tasks": None,
                        "fault_excluded_tasks": 0,
                    },
                    "cells": cells,
                }
            )

            if cand_id and baseline_id in raw_scores and cand_id in raw_scores:
                baseline_entry = subj_scores_map[baseline_id][rel_id]
                candidate_entry = subj_scores_map[cand_id][rel_id]
                metric_name = baseline_entry["metric"] if (
                    baseline_entry["metric"], baseline_entry["scale_type"]
                ) == (candidate_entry["metric"], candidate_entry["scale_type"]) else None
                b_val, c_val = raw_scores[baseline_id], raw_scores[cand_id]
                d_pp, r_pct = compute_unrounded_delta(b_val, c_val)
                st_v = "regressed" if d_pp < regression_threshold_pp else "pass"
                v_item = {
                    "benchmark_slug": meta["benchmark_slug"],
                    "dataset_release_id": rel_id,
                    "metric_name": metric_name,
                    "track_slug": meta["track_slug"],
                    "scoring_protocol": "legacy-20261120-csv",
                    "denominator_kind": "legacy_20261120_csv",
                    "baseline_score": round(b_val, 2),
                    "candidate_score": round(c_val, 2),
                    "delta_pp": d_pp,
                    "rel_pct": r_pct,
                    "coverage": "full_split (2026-11-20)",
                    "status": st_v,
                    "reason": (
                        f"2026-11-20 全量基线相对变化 {d_pp:+.2f} pp（相对 {r_pct:+.2f}%）"
                        if r_pct is not None
                        else f"2026-11-20 全量基线相对变化 {d_pp:+.2f} pp"
                    ),
                }
                if meta["goal_type"] == "optimize_gain":
                    verdict_agentic.append(v_item)
                elif meta["goal_type"] == "non_regression_guardrail":
                    verdict_guardrails.append(v_item)

        if include_verdict:
            measured_slugs = {v["benchmark_slug"] for v in verdict_agentic + verdict_guardrails}
            core_bms = self.db.fetchall("SELECT * FROM benchmarks ORDER BY track_slug, benchmark_slug")
            for bm in core_bms:
                meta_j = json.loads(bm["metadata_json"] or "{}")
                if not meta_j.get("core_14") or bm["benchmark_slug"] in measured_slugs:
                    continue
                entry = self._unmeasured_verdict(bm)
                if bm["goal_type"] == "optimize_gain":
                    verdict_agentic.append(entry)
                elif bm["goal_type"] == "non_regression_guardrail":
                    verdict_guardrails.append(entry)

        macro = compute_comparative_macro_avg(subject_scores_for_macro)
        base_macro_raw = macro["by_subject"].get(baseline_id, {}).get("comparative_macro_avg_raw")
        by_subj_summary: dict[str, Any] = {}
        for sid, info in macro["by_subject"].items():
            d_pp = None
            if base_macro_raw is not None and info.get("comparative_macro_avg_raw") is not None:
                d_pp, _ = compute_unrounded_delta(base_macro_raw, info["comparative_macro_avg_raw"])
            by_subj_summary[sid] = {
                "comparative_macro_avg": info["comparative_macro_avg"],
                "is_best": info["is_best"],
                "delta_pp": d_pp,
                "self_full_batch_avg": info["self_coverage_avg"],
                "self_bench_count": info["self_bench_count"],
            }

        n_common = len(macro["common_comparable_benchmarks"])
        if cand_id and n_common > 0:
            reg_items = [g for g in verdict_guardrails if g["status"] == "regressed"]
            pass_items = [g for g in verdict_guardrails if g["status"] == "pass"]
            undet_items = [g for g in verdict_guardrails if g["status"] == "undeterminable"]
            auto_insights.append(
                {
                    "text": (
                        f"{subj_aliases[baseline_id]} vs {subj_aliases[cand_id]} 在 2026-11-20 历史总表中共有 {n_common}/23 项共同有分基准；"
                        f"防退化护栏（阈值 {regression_threshold_pp:+.1f} pp）：{len(pass_items)} 项维持/提升、"
                        f"{len(reg_items)} 项触及退步阈值、{len(undet_items)} 项未形成可比结果。"
                    ),
                    "filter_link": {"comparison_id": comparison_id},
                }
            )

        response = {
            "comparison_id": comparison_id,
            "methodology_version": METHODOLOGY_VERSION,
            "bound_snapshot": {
                "scoring_protocol": "legacy-20261120-csv",
                "align_mode": "intersection",
                "multi_attempt_agg": "valid_mean",
                "regression_threshold_pp": regression_threshold_pp,
                "sample_set_id": None,
                "batches": [],
                "legacy_subjects": [
                    {"subject_id": str(s["id"]), "alias": subj_aliases[str(s["id"])], "model_cols": s.get("model_cols") or [s.get("model_col")]}
                    for s in subjects
                ],
            },
            "gain_vs_guardrail_verdict": {
                "baseline_subject_id": baseline_id,
                "candidate_subject_id": cand_id,
                "regression_threshold_pp": regression_threshold_pp,
                "agentic_gains": verdict_agentic,
                "guardrails": verdict_guardrails,
            }
            if include_verdict
            else None,
            "auto_insights": auto_insights,
            "comparative_summary": {
                "common_comparable_benchmarks": macro["common_comparable_benchmarks"],
                "by_subject": by_subj_summary,
            },
            "rows": rows_out,
        }

        if persist_ephemeral:
            now = utcnow_iso()
            csv_proj, _ = self._legacy_csv_provenance()
            with self.db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO saved_comparisons (
                        short_id, name, mode, project_id, owner_id, config_json, snapshot_json,
                        methodology_ver, created_at, updated_at
                    ) VALUES (?, ?, 'ephemeral', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        comparison_id,
                        f"Ephemeral {comparison_id}",
                        csv_proj,
                        user.user_id,
                        json.dumps(
                            {
                                "payload": payload,
                                "bound_batches": [],
                                "requires_default_project": True,
                                "requires_legacy_csv_access": True,
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(response, ensure_ascii=False),
                        METHODOLOGY_VERSION,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    DELETE FROM saved_comparisons
                    WHERE mode = 'ephemeral' AND short_id NOT IN (
                        SELECT short_id FROM saved_comparisons
                        WHERE mode = 'ephemeral'
                        ORDER BY created_at DESC LIMIT 300
                    )
                    """
                )
        return response

    # --- Options, Presets, Saved Comparisons, Sample Sets, and Multi-Format Export ---

    def get_compare_options(self, user: AuthUser) -> dict[str, Any]:
        projs = authorized_projects_for_user(user)
        if projs is None:
            proj_sql, params = "1=1", []
        elif not projs:
            proj_sql, params = "1=0", []
        else:
            proj_sql = f"project_id IN ({','.join('?' for _ in projs)})"
            params = list(projs)

        batches = self.db.fetchall(
            f"""
            SELECT batch_id, source_locator, source_namespace, batch_name, benchmark_slug,
                   dataset_release_id, raw_dataset_name, model_canonical, harness, runner,
                   run_scope, scoring_protocol, official_status, planned, recorded, scored, valid_mean
            FROM batches
            WHERE {proj_sql} AND is_running_placeholder = 0
            ORDER BY source_namespace ASC, batch_name ASC
            """,
            params,
        )
        by_loc = {b["source_locator"]: b for b in batches}

        legacy_models: list[dict[str, Any]] = []
        if self._can_access_legacy_csv(user):
            leg_cols = self.db.fetchall(
                """
                SELECT model_col, model_canonical, checkpoint, thinking_mode, harness, COUNT(*) AS bench_count
                FROM legacy_leaderboard_rows
                GROUP BY model_col
                ORDER BY bench_count DESC, model_col ASC
                """
            )
            for c in leg_cols:
                legacy_models.append(dict(c))
            col_set = {c["model_col"] for c in leg_cols}
            if {"Qwen3_8-27B", "Qwen3_8-27B_think"}.issubset(col_set):
                legacy_models.insert(
                    0,
                    {
                        "model_col": "Qwen3_8-27B,Qwen3_8-27B_think",
                        "model_canonical": "Qwen3_8-27B (Text+Vision 合并基线)",
                        "checkpoint": "base",
                        "thinking_mode": "mixed",
                        "harness": "direct",
                        "bench_count": 22,
                    },
                )
            if {"Qwen3_8-27B_Trained_ck133", "Qwen3_8-27B_Trained_ck133_think"}.issubset(col_set):
                legacy_models.insert(
                    1,
                    {
                        "model_col": "Qwen3_8-27B_Trained_ck133,Qwen3_8-27B_Trained_ck133_think",
                        "model_canonical": "Qwen3_8-27B_Trained_ck133 (Text+Vision 合并候选)",
                        "checkpoint": "ck133",
                        "thinking_mode": "mixed",
                        "harness": "direct",
                        "bench_count": 22,
                    },
                )

        presets: list[dict[str, Any]] = []
        col_names = {c["model_col"] for c in legacy_models}
        if {"Qwen3_8-27B", "Qwen3_8-27B_Trained_ck133", "Qwen3_8-27B_think", "Qwen3_8-27B_Trained_ck133_think"}.issubset(col_names):
            presets.append(
                {
                    "preset_id": "qwen38-3track-ck133",
                    "title": "Qwen3.8-27B 基线 vs Trained ck133（Text 12 + Vision 10 三线护栏）",
                    "common_note": "22/23 共同有分（Text 12 + Vision 10）",
                    "payload": {
                        "subjects": [
                            {"id": "s1", "kind": "legacy_model", "model_cols": ["Qwen3_8-27B", "Qwen3_8-27B_think"], "alias": "Qwen3.8-27B 基线"},
                            {"id": "s2", "kind": "legacy_model", "model_cols": ["Qwen3_8-27B_Trained_ck133", "Qwen3_8-27B_Trained_ck133_think"], "alias": "Qwen3.8-27B ck133"},
                        ],
                        "baseline_subject_id": "s1",
                        "regression_threshold_pp": -1.0,
                        "include_verdict": True,
                    },
                }
            )
            presets.append(
                {
                    "preset_id": "qwen38-text-ck133",
                    "title": "Qwen3.8-27B 基线 vs Trained ck133（LLM-Text 12/23 项）",
                    "common_note": "12/23 共同有分",
                    "payload": {
                        "subjects": [
                            {"id": "s1", "kind": "legacy_model", "model_cols": ["Qwen3_8-27B"], "alias": "Qwen3.8-27B"},
                            {"id": "s2", "kind": "legacy_model", "model_cols": ["Qwen3_8-27B_Trained_ck133"], "alias": "Qwen3.8-27B ck133"},
                        ],
                        "baseline_subject_id": "s1",
                        "regression_threshold_pp": -1.0,
                        "include_verdict": True,
                    },
                }
            )
            presets.append(
                {
                    "preset_id": "qwen38-vision-ck133",
                    "title": "Qwen3.8-27B Think vs ck133 Think（Vision 10/23 项）",
                    "common_note": "10/23 共同有分",
                    "payload": {
                        "subjects": [
                            {"id": "s1", "kind": "legacy_model", "model_cols": ["Qwen3_8-27B_think"], "alias": "Qwen3.8-27B Think"},
                            {"id": "s2", "kind": "legacy_model", "model_cols": ["Qwen3_8-27B_Trained_ck133_think"], "alias": "Qwen3.8-27B ck133 Think"},
                        ],
                        "baseline_subject_id": "s1",
                        "regression_threshold_pp": -1.0,
                        "include_verdict": True,
                    },
                }
            )
        if {"Qwen3_5-4B", "Qwen3_5-4B_Trained_ck105"}.issubset(col_names):
            presets.append(
                {
                    "preset_id": "qwen35-4b-ck105",
                    "title": "Qwen3.5-4B 基线 vs Trained ck105（共同 5/23 项）",
                    "common_note": "5/23 共同有分",
                    "payload": {
                        "subjects": [
                            {"id": "s1", "kind": "legacy_model", "model_cols": ["Qwen3_5-4B"], "alias": "Qwen3.5-4B"},
                            {"id": "s2", "kind": "legacy_model", "model_cols": ["Qwen3_5-4B_Trained_ck105"], "alias": "Qwen3.5-4B ck105"},
                        ],
                        "baseline_subject_id": "s1",
                        "regression_threshold_pp": -1.0,
                        "include_verdict": True,
                    },
                }
            )

        ds_pi5 = by_loc.get("legacy_json:DeepSWE/Pi_v5_Qwen3_8-27B_think.json")
        ds_oc = by_loc.get("legacy_json:DeepSWE/OpenClaw_Qwen3_8-27B_think.json")
        ds_pi0 = by_loc.get("legacy_json:DeepSWE/Pi_Qwen3_8-27B_think.json")
        if ds_pi5 and ds_oc:
            presets.append(
                {
                    "preset_id": "deepswe-2way",
                    "title": "DeepSWE 1.1 两组对照：Pi v5 vs OpenClaw（78 覆盖 / 60 有效）",
                    "common_note": "60/78 共同有效 · 45.00% vs 23.33%",
                    "payload": {
                        "subjects": [
                            {"id": "s1", "kind": "batch", "batch_id": ds_pi5["batch_id"], "alias": "Pi v5"},
                            {"id": "s2", "kind": "batch", "batch_id": ds_oc["batch_id"], "alias": "OpenClaw"},
                        ],
                        "baseline_subject_id": "s1",
                        "scoring_protocol": "deepswe-f2p-p2p",
                        "regression_threshold_pp": -1.0,
                        "include_verdict": True,
                    },
                }
            )
        if ds_pi5 and ds_oc and ds_pi0:
            presets.append(
                {
                    "preset_id": "deepswe-3way",
                    "title": "DeepSWE 1.1 三组全量对照（48 覆盖 / 39 有效）",
                    "common_note": "39/48 共同有效",
                    "payload": {
                        "subjects": [
                            {"id": "s1", "kind": "batch", "batch_id": ds_pi5["batch_id"], "alias": "Pi v5"},
                            {"id": "s2", "kind": "batch", "batch_id": ds_oc["batch_id"], "alias": "OpenClaw"},
                            {"id": "s3", "kind": "batch", "batch_id": ds_pi0["batch_id"], "alias": "Pi"},
                        ],
                        "baseline_subject_id": "s1",
                        "scoring_protocol": "deepswe-f2p-p2p",
                        "include_verdict": True,
                    },
                }
            )
        gaia_pi = by_loc.get("legacy_json:GAIA/Qwen3_8-27B_think_Pi.json")
        gaia_oc = by_loc.get("legacy_json:GAIA/Qwen3_8-27B_think_OpenClaw.json")
        if gaia_pi and gaia_oc:
            presets.append(
                {
                    "preset_id": "gaia-2way",
                    "title": "GAIA validation 两组历史对照（165 覆盖 / 118 有效）",
                    "common_note": "118/165 共同有效",
                    "payload": {
                        "subjects": [
                            {"id": "s1", "kind": "batch", "batch_id": gaia_pi["batch_id"], "alias": "GAIA Pi"},
                            {"id": "s2", "kind": "batch", "batch_id": gaia_oc["batch_id"], "alias": "GAIA OpenClaw"},
                        ],
                        "baseline_subject_id": "s1",
                        "scoring_protocol": "gaia-official-scorer",
                        "include_verdict": True,
                    },
                }
            )
        tb_batches = [b for b in batches if b["source_namespace"] == "native_harbor" and b["benchmark_slug"] == "terminal-bench"]
        if len(tb_batches) >= 2:
            presets.append(
                {
                    "preset_id": "tb21-6way",
                    "title": f"Terminal-Bench 2.1 原生 Harbor {len(tb_batches)} 组全量对照（71 共同有效）",
                    "common_note": f"{len(tb_batches)} 组 · 71 共同有效题",
                    "payload": {
                        "subjects": [
                            {"id": f"s{i + 1}", "kind": "batch", "batch_id": b["batch_id"], "alias": b["batch_name"]}
                            for i, b in enumerate(tb_batches[:8])
                        ],
                        "baseline_subject_id": "s1",
                        "scoring_protocol": "harbor-native-reward-valid-v1",
                        "include_verdict": True,
                    },
                }
            )

        if user.role == "admin":
            db_projs = {
                str(r["project_id"])
                for r in self.db.fetchall("SELECT DISTINCT project_id FROM batches WHERE project_id IS NOT NULL")
                if r["project_id"] and not is_personal_project(str(r["project_id"]))
            }
            admin_projs = {"default"} | set(user.projects) | db_projs
            admin_projs = {p for p in admin_projs if p and not is_personal_project(p)}
            writable_projects = (["default"] if "default" in admin_projs else []) + sorted(
                p for p in admin_projs if p != "default"
            )
            writable_projects.append(personal_project_id(user.user_id))
        else:
            team_projs = sorted(p for p in user.projects if p and not is_personal_project(p))
            writable_projects = team_projs + [personal_project_id(user.user_id)]

        return {
            "methodology_version": METHODOLOGY_VERSION,
            "writable_projects": writable_projects,
            "batch_subjects": batches,
            "legacy_model_subjects": legacy_models,
            "presets": presets,
            "saved_comparisons": self.list_saved_comparisons(user)["items"],
            "sample_sets": self.list_sample_sets(user)["items"],
        }

    def list_saved_comparisons(self, user: AuthUser) -> dict[str, Any]:
        projs = authorized_projects_for_user(user)
        if projs is None:
            proj_sql, params = "1=1", []
        elif not projs:
            proj_sql, params = "1=0", []
        else:
            proj_sql = f"project_id IN ({','.join('?' for _ in projs)})"
            params = list(projs)
        rows = self.db.fetchall(
            f"""
            SELECT short_id, name, mode, project_id, owner_id, methodology_ver, created_at, updated_at
            FROM saved_comparisons
            WHERE {proj_sql} AND mode IN ('live', 'snapshot')
            ORDER BY updated_at DESC
            LIMIT 100
            """,
            params,
        )
        return {"items": rows, "total": len(rows), "methodology_version": METHODOLOGY_VERSION}

    def delete_saved_comparison(self, user: AuthUser, short_id: str) -> dict[str, Any]:
        validate_safe_id(short_id, "short_id")
        row = self.db.fetchone("SELECT * FROM saved_comparisons WHERE short_id = ?", (short_id,))
        if row is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", "对比短链不存在")
        require_project_access(user, row["project_id"], owner_id=row["owner_id"])
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM saved_comparisons WHERE short_id = ?", (short_id,))
        return {"ok": True, "short_id": short_id}

    def list_sample_sets(self, user: AuthUser) -> dict[str, Any]:
        projs = authorized_projects_for_user(user)
        if projs is None:
            proj_sql, params = "1=1", []
        elif not projs:
            proj_sql, params = "1=0", []
        else:
            proj_sql = f"project_id IN ({','.join('?' for _ in projs)})"
            params = list(projs)
        rows = self.db.fetchall(
            f"SELECT * FROM sample_sets WHERE {proj_sql} ORDER BY updated_at DESC LIMIT 200",
            params,
        )
        can_holdout = has_scope(user, "dataset:holdout_read")
        items = []
        for r in rows:
            tids = json.loads(r.pop("task_ids_json") or "[]")
            if not can_holdout and self._contains_holdout_tasks(r.get("dataset_release_id"), tids):
                continue
            r["sample_set_id"] = r["set_id"]
            r["task_ids"] = tids
            r["task_count"] = len(tids)
            items.append(r)
        return {"items": items, "total": len(items), "methodology_version": METHODOLOGY_VERSION}

    def create_sample_set(self, user: AuthUser, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "样本集名称不能为空")
        project_id = str(payload.get("project_id") or "default").strip()
        require_project_access(user, project_id, owner_id=user.user_id)
        rel_id = str(payload.get("dataset_release_id") or "custom").strip()
        validate_safe_id(rel_id, "dataset_release_id")
        task_ids = [str(t).strip() for t in (payload.get("task_ids") or []) if str(t).strip()]
        if not task_ids:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "样本集至少需要包含 1 个 task_id")
        if self._contains_holdout_tasks(rel_id, task_ids):
            require_scope(user, "dataset:holdout_read")
        ss_id = f"ss_{secrets.token_hex(6)}"
        now = utcnow_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sample_sets (
                    set_id, name, description, dataset_release_id, task_ids_json,
                    filter_spec_json, project_id, owner_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ss_id,
                    name,
                    payload.get("description"),
                    rel_id,
                    json.dumps(sorted(set(task_ids)), ensure_ascii=False),
                    json.dumps(payload.get("filter_spec") or {}, ensure_ascii=False),
                    project_id,
                    user.user_id,
                    now,
                ),
            )
        return {
            "sample_set_id": ss_id,
            "set_id": ss_id,
            "name": name,
            "project_id": project_id,
            "dataset_release_id": rel_id,
            "task_count": len(set(task_ids)),
            "updated_at": now,
        }

    def update_sample_set(self, user: AuthUser, sample_set_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        validate_safe_id(sample_set_id, "sample_set_id")
        row = self.db.fetchone("SELECT * FROM sample_sets WHERE set_id = ?", (sample_set_id,))
        if row is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", "样本集不存在")
        require_project_access(user, row["project_id"], owner_id=row["owner_id"])
        existing_tids = json.loads(row["task_ids_json"] or "[]")
        if self._contains_holdout_tasks(row.get("dataset_release_id"), existing_tids):
            require_scope(user, "dataset:holdout_read")
        name = str(payload.get("name") or row["name"]).strip()
        desc = payload.get("description", row["description"])
        task_ids = payload.get("task_ids")
        if isinstance(task_ids, list):
            next_tids = sorted({str(t).strip() for t in task_ids if str(t).strip()})
            if self._contains_holdout_tasks(row.get("dataset_release_id"), next_tids):
                require_scope(user, "dataset:holdout_read")
            tids_json = json.dumps(next_tids, ensure_ascii=False)
        else:
            tids_json = row["task_ids_json"]
        now = utcnow_iso()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE sample_sets SET name = ?, description = ?, task_ids_json = ?, updated_at = ? WHERE set_id = ?",
                (name, desc, tids_json, now, sample_set_id),
            )
        return {"sample_set_id": sample_set_id, "name": name, "task_count": len(json.loads(tids_json)), "updated_at": now}

    def delete_sample_set(self, user: AuthUser, sample_set_id: str) -> dict[str, Any]:
        validate_safe_id(sample_set_id, "sample_set_id")
        row = self.db.fetchone("SELECT * FROM sample_sets WHERE set_id = ?", (sample_set_id,))
        if row is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", "样本集不存在")
        require_project_access(user, row["project_id"], owner_id=row["owner_id"])
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM sample_sets WHERE set_id = ?", (sample_set_id,))
        return {"ok": True, "sample_set_id": sample_set_id}

    def export_comparison(self, user: AuthUser, comparison_id: str, fmt: str = "markdown") -> tuple[bytes, str, str]:
        """Export a bound comparison_id as CSV / JSON / Markdown / standalone offline HTML.
        Re-verifies live project authorization AND enforces 6-scope boundaries (S-02)."""
        validate_safe_id(comparison_id, "comparison_id")
        opened = self.open_saved_comparison(user, comparison_id)
        result = opened["result"]
        fmt_l = fmt.lower().strip()
        rows = result.get("rows") or []
        verdict = result.get("gain_vs_guardrail_verdict") or {}
        comp_sum = result.get("comparative_summary") or {}
        subj_ids = list((comp_sum.get("by_subject") or {}).keys())

        if fmt_l == "json":
            export_obj = {
                "comparison_id": comparison_id,
                "methodology_version": result.get("methodology_version", METHODOLOGY_VERSION),
                "bound_snapshot": result.get("bound_snapshot"),
                "comparative_summary": comp_sum,
                "gain_vs_guardrail_verdict": verdict,
                "auto_insights": result.get("auto_insights", []),
                "rows": rows,
                "trace_links_redacted": not has_scope(user, "trace:read"),
                "qa_links_redacted": not has_scope(user, "direct_vlm_qa:read"),
                "holdout_details_redacted": not has_scope(user, "dataset:holdout_read"),
            }
            return (
                json.dumps(export_obj, indent=2, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                f"lrm-eval-{comparison_id}.json",
            )

        if fmt_l == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            header = ["benchmark_slug", "dataset_release_id", "scoring_protocol", "common_covered", "common_valid"]
            for sid in subj_ids:
                header.extend([f"{sid}_score", f"{sid}_delta_pp", f"{sid}_rel_pct"])
            writer.writerow(header)
            for r in rows:
                inter = r.get("intersection") or {}
                line: list[Any] = [
                    r.get("benchmark_slug"),
                    r.get("dataset_release_id"),
                    r.get("scoring_protocol"),
                    inter.get("common_covered_tasks"),
                    inter.get("common_valid_scored_tasks"),
                ]
                cells = r.get("cells") or {}
                for sid in subj_ids:
                    cv = (cells.get(sid) or {}).get("common_valid") or {}
                    line.extend([cv.get("score"), cv.get("delta_pp"), cv.get("rel_pct")])
                writer.writerow(line)
            return buf.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", f"lrm-eval-{comparison_id}.csv"

        if fmt_l == "markdown":
            lines = [
                f"# LRM Eval Comparison Report (`{comparison_id}`)",
                f"- Methodology: `{result.get('methodology_version', METHODOLOGY_VERSION)}`",
                f"- Common Comparable Benchmarks: `{len(comp_sum.get('common_comparable_benchmarks') or [])}`",
                "",
                "## Comparison Matrix",
                "",
                "| Benchmark | Protocol | Common Valid / Covered | " + " | ".join(subj_ids) + " |",
                "|---|---|---|" + "|".join("---" for _ in subj_ids) + "|",
            ]
            for r in rows:
                inter = r.get("intersection") or {}
                denom_s = (
                    f"{inter.get('common_valid_scored_tasks')}/{inter.get('common_covered_tasks')}"
                    if inter.get("common_valid_scored_tasks") is not None
                    else "legacy_full_split"
                )
                vals = []
                for sid in subj_ids:
                    cv = ((r.get("cells") or {}).get(sid) or {}).get("common_valid") or {}
                    sc = cv.get("score")
                    dp = cv.get("delta_pp")
                    if sc is None:
                        vals.append("—")
                    elif dp is not None and dp != 0:
                        vals.append(f"{sc:.2f}% ({dp:+.2f} pp)")
                    else:
                        vals.append(f"{sc:.2f}%")
                lines.append(f"| {r.get('raw_dataset_name')} | `{r.get('scoring_protocol')}` | {denom_s} | " + " | ".join(vals) + " |")
            return "\n".join(lines).encode("utf-8"), "text/markdown; charset=utf-8", f"lrm-eval-{comparison_id}.md"

        if fmt_l == "html":
            esc = html.escape
            row_trs = []
            for r in rows:
                inter = r.get("intersection") or {}
                denom_s = (
                    f"{inter.get('common_valid_scored_tasks')}/{inter.get('common_covered_tasks')}"
                    if inter.get("common_valid_scored_tasks") is not None
                    else "2026-11-20 full_split"
                )
                tds = [
                    f"<td><strong>{esc(str(r.get('raw_dataset_name') or ''))}</strong></td>",
                    f"<td><code>{esc(str(r.get('scoring_protocol') or ''))}</code></td>",
                    f"<td class='num'>{esc(denom_s)}</td>",
                ]
                for sid in subj_ids:
                    cv = ((r.get("cells") or {}).get(sid) or {}).get("common_valid") or {}
                    sc = cv.get("score")
                    dp = cv.get("delta_pp")
                    if sc is None:
                        tds.append("<td class='num muted'>—</td>")
                    else:
                        dp_s = f" <span class='delta'>({dp:+.2f} pp)</span>" if dp is not None else ""
                        best_cls = " best" if cv.get("is_best") else ""
                        tds.append(f"<td class='num{best_cls}'>{sc:.2f}%{dp_s}</td>")
                row_trs.append("<tr>" + "".join(tds) + "</tr>")

            insights_li = "".join(f"<li>{esc(str(it.get('text') or ''))}</li>" for it in (result.get("auto_insights") or []))
            doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>LRM Eval Comparison Report - {esc(comparison_id)}</title>
<style>
  body {{ font-family: Inter, -apple-system, "PingFang SC", sans-serif; background:#f6f8fa; color:#0f172a; margin:0; padding:32px; }}
  .card {{ background:#fff; border:1px solid #e5e7eb; border-radius:12px; padding:24px; max-width:1120px; margin:0 auto 20px; }}
  h1 {{ margin:0 0 8px; font-size:22px; }}
  .meta {{ color:#475569; font-size:13px; margin-bottom:16px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th, td {{ padding:10px 12px; border-bottom:1px solid #e5e7eb; text-align:left; }}
  th {{ background:#f8fafc; color:#475569; font-weight:600; }}
  .num {{ font-family: "JetBrains Mono", monospace; font-variant-numeric: tabular-nums; }}
  .best {{ color:#b45309; font-weight:700; }}
  .delta {{ color:#475569; font-size:12px; }}
  .muted {{ color:#94a3b8; }}
  @media print {{ body {{ background:#fff; padding:0; }} .card {{ border:none; }} }}
</style>
</head>
<body>
  <div class="card">
    <h1>LRM Eval 评测对比报告</h1>
    <div class="meta">comparison_id: <code>{esc(comparison_id)}</code> · methodology: <code>{esc(str(result.get('methodology_version', METHODOLOGY_VERSION)))}</code> · 共同可比基准数: {len(comp_sum.get('common_comparable_benchmarks') or [])}</div>
    <ul>{insights_li}</ul>
    <table>
      <thead><tr><th>Benchmark</th><th>Protocol</th><th>Denominator</th>{''.join(f'<th>{esc(sid)}</th>' for sid in subj_ids)}</tr></thead>
      <tbody>{''.join(row_trs)}</tbody>
    </table>
  </div>
</body>
</html>"""
            return doc.encode("utf-8"), "text/html; charset=utf-8", f"lrm-eval-{comparison_id}.html"

        raise AppError(422, "SCHEMA_VALIDATION_FAILED", f"不支持的导出格式: {fmt}")
