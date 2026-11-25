from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import statistics
import tarfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.api.errors import AppError
from app.config import Settings
from app.db.database import Database
from app.db.migrations import load_taxonomy_config
from app.indexing.cold_indexer import MAX_TASK_ATTACHMENT_BYTES, read_legacy_sample_by_offset
from app.indexing.hot_indexer import SourceApprovalRegistry, _inspect_trial_result_and_media
from app.security.auth import AuthUser
from app.security.authz import (
    authorized_projects_for_user,
    can_access_project,
    has_scope,
    is_holdout_dataset_or_batch,
    require_project_access,
    require_scope,
    sql_task_holdout_exclude_clause,
)
from app.security.redaction import redact_json_recursive, redact_text
from app.security.storage_probe import resolve_within_root, validate_safe_id, validate_safe_task_id
from app.services.methodology import (
    METHODOLOGY_VERSION,
    bootstrap_ci95,
    compute_comparative_macro_avg,
    finite,
    normalize_score_to_100,
    pass_at_k,
    pass_hat_k,
    wilson_ci95,
)

_DATA_IMAGE_RE = re.compile(r"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]{16,})")

# Fields that MUST NEVER appear in any API batch DTO (S-03)
_FORBIDDEN_BATCH_DTO_KEYS = frozenset({"internal_source_fingerprint"})


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _project_filter_sql(user: AuthUser, column: str = "project_id") -> tuple[str, list[Any]]:
    projs = authorized_projects_for_user(user)
    if projs is None:
        return "1=1", []
    if not projs:
        return "1=0", []
    placeholders = ",".join("?" for _ in projs)
    return f"{column} IN ({placeholders})", list(projs)


def _sanitize_batch_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if k not in _FORBIDDEN_BATCH_DTO_KEYS}
    for jf in (
        "source_tags_json",
        "absent_optional_json",
        "outcomes_json",
        "failure_kinds_json",
        "vlmeval_metrics_json",
        "run_config_json",
        "usage_json",
        "checks_json",
    ):
        if jf in out:
            raw = out.pop(jf)
            key = jf[:-5]
            if raw is None:
                out[key] = None
            else:
                try:
                    out[key] = json.loads(raw)
                except Exception:
                    out[key] = raw
    out["is_running_placeholder"] = bool(out.get("is_running_placeholder"))
    out["has_gateway"] = bool(out.get("has_gateway"))
    out["has_selection"] = bool(out.get("has_selection"))
    return out


class QueryService:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.approval_registry = SourceApprovalRegistry(settings.source_approval_file)

    def _resolve_batch_root(self, batch: dict[str, Any]) -> Path | None:
        ns = batch.get("source_namespace")
        loc = str(batch.get("source_locator") or "")
        if ns == "harbor_runs" and loc.startswith("harbor_runs:"):
            return self.settings.harbor_runs_dir / loc.split(":", 1)[1]
        if ns == "runner_published" and loc.startswith("runner_published:") and self.settings.published_batches_dir is not None:
            return self.settings.published_batches_dir / loc.split(":", 1)[1]
        if ns == "native_harbor" and loc.startswith("native_harbor:"):
            return self.settings.legacy_root_dir / loc.split(":", 1)[1]
        if ns == "legacy_json" and loc.startswith("legacy_json:"):
            return self.settings.legacy_root_dir / loc.split(":", 1)[1]
        return None

    def _can_see_legacy_csv(self, user: AuthUser) -> bool:
        proj_id, owner_id, _, _ = self.approval_registry.lookup_full(self.settings.legacy_root_dir / "leaderboard.csv")
        return can_access_project(user, proj_id, owner_id=owner_id)

    def _is_holdout_batch(self, batch: dict[str, Any]) -> bool:
        rel_id = batch.get("dataset_release_id")
        rel_split: str | None = None
        if rel_id:
            rel = self.db.fetchone("SELECT split FROM dataset_releases WHERE dataset_release_id = ?", (rel_id,))
            if rel:
                rel_split = rel.get("split")
        return is_holdout_dataset_or_batch(
            dataset_release_id=str(rel_id) if rel_id else None,
            release_split=rel_split,
            benchmark_slug=batch.get("benchmark_slug"),
            raw_dataset_name=batch.get("raw_dataset_name"),
            batch_name=batch.get("batch_name"),
        )

    def _holdout_task_ids_for_release(self, dataset_release_id: str | None) -> set[str]:
        if not dataset_release_id:
            return set()
        rows = self.db.fetchall(
            """
            SELECT task_id FROM task_definitions
            WHERE dataset_release_id = ? AND LOWER(COALESCE(split, '')) = 'holdout'
            """,
            (dataset_release_id,),
        )
        return {str(r["task_id"]) for r in rows}

    def _is_holdout_task(self, dataset_release_id: str | None, task_id: str | None) -> bool:
        if not dataset_release_id or not task_id:
            return False
        row = self.db.fetchone(
            """
            SELECT 1 AS hit FROM task_definitions
            WHERE dataset_release_id = ? AND task_id = ? AND LOWER(COALESCE(split, '')) = 'holdout'
            LIMIT 1
            """,
            (dataset_release_id, task_id),
        )
        return row is not None

    def _is_holdout_batch_or_task(self, batch: dict[str, Any], task_id: str | None) -> bool:
        if self._is_holdout_batch(batch):
            return True
        return self._is_holdout_task(batch.get("dataset_release_id"), task_id)

    def _check_evidence_class_approved(self, batch: dict[str, Any], evidence_class: str) -> bool:
        b_root = self._resolve_batch_root(batch)
        return self.approval_registry.is_evidence_class_approved(
            b_root, evidence_class, str(batch.get("evidence_approval") or "pending")
        )

    def _require_evidence_class(self, batch: dict[str, Any], evidence_class: str, message: str) -> None:
        if not self._check_evidence_class_approved(batch, evidence_class):
            raise AppError(
                403,
                "SCOPE_FORBIDDEN",
                message,
                {"evidence_class": evidence_class, "approval": batch.get("evidence_approval")},
            )

    def _resolve_evidence_ref_path(self, batch: dict[str, Any], ref: dict[str, Any]) -> Path:
        """Resolve an episode evidence_refs entry across abs_path, isolated 0700 evidence_store, or batch_root (S-06)."""
        if ref.get("abs_path"):
            return Path(ref["abs_path"])
        rel_path = ref.get("rel_path")
        if not isinstance(rel_path, str) or not rel_path:
            raise AppError(404, "RESOURCE_NOT_FOUND", "证据引用缺少路径")
        if ref.get("storage_tier") == "evidence_store_0700":
            if self.settings.evidence_store_dir is None:
                raise AppError(503, "STAGING_PERMISSION_UNSAFE", "隔离证据存储目录未配置")
            return resolve_within_root(self.settings.evidence_store_dir, rel_path)
        b_root = self._resolve_batch_root(batch)
        if not b_root:
            raise AppError(404, "RESOURCE_NOT_FOUND", "无法解析批次目录")
        return resolve_within_root(b_root, rel_path)

    def _verify_lazy_file(self, target_path: Path, expected_sha256: str | None) -> bytes:
        """Read lazy evidence file and verify sha256 against generation digest if recorded (I-01).
        Raises 409 GENERATION_MISMATCH on cross-generation overwrite!"""
        if not target_path.is_file():
            raise AppError(404, "RESOURCE_NOT_FOUND", "目标证据文件不存在")
        try:
            data = target_path.read_bytes()
        except OSError as exc:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"证据文件无法读取: {exc}") from exc
        if expected_sha256:
            actual = _sha256_bytes(data)
            if actual != expected_sha256:
                raise AppError(
                    409,
                    "GENERATION_MISMATCH",
                    "检测到明细文件跨代覆写（摘要与已索引 generation_id 不一致），拒绝跨代拼接，请刷新重试",
                    {"expected_sha256": expected_sha256[:16], "actual_sha256": actual[:16]},
                )
        return data

    # --- 1. Auth-Before-Dedup Core Primitive (v2.2.1 04-1 & S-02) ---

    def query_authorized_protocol_scores(
        self,
        user: AuthUser,
        *,
        scoring_protocol: str | None = None,
        dataset_release_id: str | None = None,
        source_namespaces: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """STRICTLY filters by user's authorized projects & selected sources FIRST,
        then deduplicates by (canonical_trial_key, scoring_protocol) via ROW_NUMBER()!
        Also excludes holdout per-task trial rows unless user holds dataset:holdout_read (S-02)."""
        proj_sql, params = _project_filter_sql(user, "t.project_id")
        where = [proj_sql]
        if scoring_protocol:
            where.append("t.scoring_protocol = ?")
            params.append(scoring_protocol)
        if dataset_release_id:
            where.append("t.dataset_release_id = ?")
            params.append(dataset_release_id)
        if source_namespaces:
            ph = ",".join("?" for _ in source_namespaces)
            where.append(f"t.source_namespace IN ({ph})")
            params.extend(source_namespaces)
        if not has_scope(user, "dataset:holdout_read"):
            where.append("LOWER(COALESCE(r.split, '')) != 'holdout'")
            where.append("LOWER(COALESCE(t.dataset_release_id, b.dataset_release_id, '')) NOT LIKE '%holdout%'")
            where.append("LOWER(COALESCE(b.raw_dataset_name, '')) NOT LIKE '%holdout%'")
            where.append("LOWER(COALESCE(b.batch_name, '')) NOT LIKE '%holdout%'")
            where.append(sql_task_holdout_exclude_clause("COALESCE(t.dataset_release_id, b.dataset_release_id)", "t.task_id"))
        sql = f"""
            WITH authorized_candidates AS (
                SELECT t.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY t.canonical_trial_key, t.scoring_protocol
                           ORDER BY t.source_priority_rank ASC, t.batch_id ASC
                       ) AS rn
                FROM trial_protocol_scores t
                JOIN batches b ON b.batch_id = t.batch_id
                LEFT JOIN dataset_releases r ON r.dataset_release_id = COALESCE(t.dataset_release_id, b.dataset_release_id)
                WHERE {' AND '.join(where)}
            )
            SELECT canonical_trial_key, scoring_protocol, batch_id, trial_id, project_id,
                   source_namespace, source_priority_rank, dataset_release_id, task_id,
                   status, reward, reward_valid, exception_type, exception_policy, source_tags_json
            FROM authorized_candidates
            WHERE rn = 1
            ORDER BY scoring_protocol ASC, task_id ASC, batch_id ASC
        """
        return self.db.fetchall(sql, params)

    # --- 2. Overview & Search ---

    def get_system_overview(self, user: AuthUser) -> dict[str, Any]:
        """Production overview using auth-before-dedup SQL for deduplicated trial KPIs alongside raw batch totals."""
        proj_sql, params = _project_filter_sql(user, "project_id")
        bm_total = self.db.fetchone("SELECT COUNT(*) AS c FROM benchmarks")["c"]  # type: ignore[index]
        rel_total = self.db.fetchone("SELECT COUNT(*) AS c FROM dataset_releases")["c"]  # type: ignore[index]
        batch_stats = self.db.fetchone(
            f"""
            SELECT COUNT(*) AS total_batches,
                   SUM(CASE WHEN is_running_placeholder = 1 THEN 1 ELSE 0 END) AS running_placeholders,
                   COALESCE(SUM(scored), 0) AS total_scored_trials,
                   COALESCE(SUM(recorded), 0) AS total_recorded_trials
            FROM batches
            WHERE {proj_sql}
            """,
            params,
        ) or {}

        # Auth-before-dedup trial KPIs (per (canonical_trial_key, scoring_protocol) within user's authorized projects!)
        dedup_stats = self.db.fetchone(
            f"""
            WITH auth_dedup AS (
                SELECT canonical_trial_key, scoring_protocol, reward_valid,
                       ROW_NUMBER() OVER (
                           PARTITION BY canonical_trial_key, scoring_protocol
                           ORDER BY source_priority_rank ASC, batch_id ASC
                       ) AS rn
                FROM trial_protocol_scores
                WHERE {proj_sql}
            )
            SELECT COUNT(*) AS dedup_recorded_trials,
                   COALESCE(SUM(CASE WHEN reward_valid = 1 THEN 1 ELSE 0 END), 0) AS dedup_scored_trials
            FROM auth_dedup
            WHERE rn = 1
            """,
            params,
        ) or {}

        combos = self.db.fetchone(
            f"""
            SELECT COUNT(DISTINCT COALESCE(model_canonical,'') || '|' || COALESCE(harness,'')) AS c
            FROM batches
            WHERE {proj_sql} AND is_running_placeholder = 0
            """,
            params,
        )["c"]  # type: ignore[index]
        warnings_count = self.db.fetchone("SELECT COUNT(*) AS c FROM index_warnings")["c"]  # type: ignore[index]
        recent = self.db.fetchall(
            f"""
            SELECT * FROM batches
            WHERE {proj_sql}
            ORDER BY is_running_placeholder DESC, COALESCE(finished_at, indexed_at) DESC
            LIMIT 8
            """,
            params,
        )
        return {
            "methodology_version": METHODOLOGY_VERSION,
            "kpis": {
                "benchmarks": bm_total,
                "dataset_releases": rel_total,
                "readable_batches": int(batch_stats.get("total_batches") or 0),
                "running_placeholders": int(batch_stats.get("running_placeholders") or 0),
                "total_recorded_trials": int(batch_stats.get("total_recorded_trials") or 0),
                "total_scored_trials": int(batch_stats.get("total_scored_trials") or 0),
                "dedup_recorded_trials": int(dedup_stats.get("dedup_recorded_trials") or 0),
                "dedup_scored_trials": int(dedup_stats.get("dedup_scored_trials") or 0),
                "model_harness_combos": int(combos or 0),
                "index_warnings": int(warnings_count or 0),
            },
            "recent_batches": [_sanitize_batch_row(r) for r in recent],
        }

    def search_global(self, user: AuthUser, q: str, limit: int = 30) -> dict[str, Any]:
        query = (q or "").strip()
        if not query:
            return {"query": query, "items": [], "methodology_version": METHODOLOGY_VERSION}
        proj_sql, params = _project_filter_sql(user, "project_id")
        safe_fts = " ".join(f'"{tok.replace(chr(34), "")}"*' for tok in re.findall(r"[A-Za-z0-9_\u4e00-\u9fff.-]+", query))
        if not safe_fts:
            return {"query": query, "items": [], "methodology_version": METHODOLOGY_VERSION}
        # Rank high-signal entity hits (authorized models, harnesses, benchmarks) FIRST so
        # many matching batches NEVER push model/benchmark profiles out of `items[:limit]`!
        top_items: list[dict[str, Any]] = []

        model_rows = self.db.fetchall(
            f"""
            SELECT model_canonical, MIN(project_id) AS project_id
            FROM batches
            WHERE {proj_sql} AND is_running_placeholder = 0 AND model_canonical IS NOT NULL
              AND LOWER(model_canonical) LIKE ?
            GROUP BY model_canonical
            ORDER BY model_canonical ASC
            LIMIT 6
            """,
            [*params, f"%{query.lower()}%"],
        )
        seen_models: set[str] = set()
        for m in model_rows:
            mc = str(m["model_canonical"])
            seen_models.add(mc)
            top_items.append(
                {
                    "entity_type": "model",
                    "entity_id": mc,
                    "title": mc,
                    "subtitle": "模型档案 (Model Profile)",
                    "project_id": m["project_id"],
                }
            )
        if self._can_see_legacy_csv(user):
            leg_models = self.db.fetchall(
                """
                SELECT DISTINCT model_canonical
                FROM legacy_leaderboard_rows
                WHERE LOWER(model_canonical) LIKE ? OR LOWER(model_col) LIKE ?
                ORDER BY model_canonical ASC
                LIMIT 6
                """,
                (f"%{query.lower()}%", f"%{query.lower()}%"),
            )
            for lm in leg_models:
                mc = str(lm["model_canonical"])
                if mc not in seen_models:
                    seen_models.add(mc)
                    top_items.append(
                        {
                            "entity_type": "model",
                            "entity_id": mc,
                            "title": mc,
                            "subtitle": "模型档案 (2026-11-20 历史基线)",
                            "project_id": "default",
                        }
                    )

        bm_rows = self.db.fetchall(
            """
            SELECT benchmark_slug, display_name, track_display_name, capability
            FROM benchmarks
            WHERE benchmark_slug LIKE ? OR display_name LIKE ? OR capability LIKE ?
            LIMIT 6
            """,
            (f"%{query}%", f"%{query}%", f"%{query}%"),
        )
        for b in bm_rows:
            top_items.append(
                {
                    "entity_type": "benchmark",
                    "entity_id": b["benchmark_slug"],
                    "title": b["display_name"],
                    "subtitle": f"{b['track_display_name']} · {b['capability']}",
                    "project_id": "default",
                }
            )

        rows = self.db.fetchall(
            f"""
            SELECT entity_type, entity_id, title, subtitle, project_id, required_scope
            FROM search_fts
            WHERE search_fts MATCH ? AND {proj_sql}
            LIMIT ?
            """,
            [safe_fts, *params, limit * 2],
        )
        fts_items: list[dict[str, Any]] = []
        can_read_holdout = has_scope(user, "dataset:holdout_read")
        for r in rows:
            req_scope = (r.get("required_scope") or "").strip()
            if req_scope and not has_scope(user, req_scope):
                continue
            if not can_read_holdout and r.get("entity_type") == "task":
                eid = str(r.get("entity_id") or "")
                if "/" in eid:
                    rel_part, _, tid_part = eid.partition("/")
                    if is_holdout_dataset_or_batch(dataset_release_id=rel_part) or self._is_holdout_task(rel_part, tid_part):
                        continue
                elif self.db.fetchone(
                    "SELECT 1 AS hit FROM task_definitions WHERE task_id = ? AND LOWER(COALESCE(split, '')) = 'holdout' LIMIT 1",
                    (eid,),
                ):
                    continue
            fts_items.append(
                {
                    "entity_type": r["entity_type"],
                    "entity_id": r["entity_id"],
                    "title": r["title"],
                    "subtitle": r["subtitle"],
                    "project_id": r["project_id"],
                }
            )
            if len(top_items) + len(fts_items) >= limit:
                break
        return {"query": query, "items": (top_items + fts_items)[:limit], "methodology_version": METHODOLOGY_VERSION}

    # --- 3. Batches & Trials ---

    def list_batches(
        self,
        user: AuthUser,
        *,
        runner: str | None = None,
        benchmark_slug: str | None = None,
        run_scope: str | None = None,
        scoring_protocol: str | None = None,
        source_namespace: str | None = None,
        model_canonical: str | None = None,
        harness: str | None = None,
        batch_status: str | None = None,
        modality: str | None = None,
        language: str | None = None,
        has_gateway: bool | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        proj_sql, params = _project_filter_sql(user, "b.project_id")
        where = [proj_sql]
        if runner:
            where.append("b.runner = ?")
            params.append(runner)
        if benchmark_slug:
            where.append("b.benchmark_slug = ?")
            params.append(benchmark_slug)
        if run_scope:
            where.append("b.run_scope = ?")
            params.append(run_scope)
        if scoring_protocol:
            where.append("b.scoring_protocol = ?")
            params.append(scoring_protocol)
        if source_namespace:
            where.append("b.source_namespace = ?")
            params.append(source_namespace)
        if model_canonical:
            where.append("b.model_canonical = ?")
            params.append(model_canonical)
        if harness:
            where.append("COALESCE(b.harness, b.runner) = ?")
            params.append(harness)
        if batch_status:
            if batch_status == "running_placeholder":
                where.append("b.is_running_placeholder = 1")
            else:
                where.append("b.batch_status = ?")
                params.append(batch_status)
        if modality:
            where.append("bm.modality = ?")
            params.append(modality)
        if language:
            where.append("bm.language = ?")
            params.append(language)
        if has_gateway is True:
            where.append("b.has_gateway = 1")
        if q and q.strip():
            like_q = f"%{q.strip()}%"
            where.append("(b.batch_name LIKE ? OR b.raw_dataset_name LIKE ? OR b.model_canonical LIKE ? OR b.harness LIKE ?)")
            params.extend([like_q, like_q, like_q, like_q])

        where_sql = " AND ".join(where)
        total = self.db.fetchone(
            f"""
            SELECT COUNT(*) AS c
            FROM batches b
            LEFT JOIN benchmarks bm ON bm.benchmark_slug = b.benchmark_slug
            WHERE {where_sql}
            """,
            params,
        )["c"]  # type: ignore[index]
        offset = max(0, (page - 1) * page_size)
        rows = self.db.fetchall(
            f"""
            SELECT b.*, o.metric AS official_metric, o.score AS official_score,
                   o.denominator AS official_denominator, o.source AS official_source,
                   bm.modality AS bm_modality, bm.language AS bm_language, bm.track_slug AS bm_track_slug
            FROM batches b
            LEFT JOIN official_results o ON o.batch_id = b.batch_id
            LEFT JOIN benchmarks bm ON bm.benchmark_slug = b.benchmark_slug
            WHERE {where_sql}
            ORDER BY b.is_running_placeholder DESC, COALESCE(b.finished_at, b.indexed_at) DESC
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, offset],
        )
        items = []
        for r in rows:
            off = None
            if r.get("official_score") is not None:
                off = {
                    "metric": r["official_metric"],
                    "score": r["official_score"],
                    "denominator": r["official_denominator"],
                    "source": r["official_source"],
                }
            mod = r.pop("bm_modality", None)
            lang = r.pop("bm_language", None)
            tr_slug = r.pop("bm_track_slug", None)
            for k in ("official_metric", "official_score", "official_denominator", "official_source"):
                r.pop(k, None)
            dto = _sanitize_batch_row(r)
            dto["official_result"] = off
            dto["modality"] = mod
            dto["language"] = lang
            dto["track_slug"] = tr_slug
            items.append(dto)
        return {
            "items": items,
            "total": int(total),
            "page": page,
            "page_size": page_size,
            "methodology_version": METHODOLOGY_VERSION,
        }

    def get_batch_detail(self, user: AuthUser, batch_id: str) -> dict[str, Any]:
        validate_safe_id(batch_id, "batch_id")
        row = self.db.fetchone("SELECT * FROM batches WHERE batch_id = ?", (batch_id,))
        if row is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"批次不存在: {batch_id}")
        require_project_access(user, row["project_id"], owner_id=row["owner_id"])
        off = self.db.fetchone(
            "SELECT dataset, metric, score, denominator, source, origin, fault_policy FROM official_results WHERE batch_id = ?",
            (batch_id,),
        )
        dto = _sanitize_batch_row(row)
        dto["official_result"] = off
        dto["totals"] = {
            "unique_tasks": dto["unique_tasks"],
            "planned": dto["planned"],
            "recorded": dto["recorded"],
            "scored": dto["scored"],
            "unscored": dto["unscored"],
            "coverage": dto["coverage"],
            "valid_mean": dto["valid_mean"],
            "passed": dto["passed"],
            "pass_threshold": dto["pass_threshold"],
            "outcomes": dto.get("outcomes") or {},
        }
        dto["methodology_version"] = METHODOLOGY_VERSION
        return dto

    def get_batch_tasks(self, user: AuthUser, batch_id: str) -> dict[str, Any]:
        batch = self.get_batch_detail(user, batch_id)
        eps = self.db.fetchall(
            """
            SELECT trial_id, task_id, attempt_index, status, reward, reward_valid,
                   failure_kind, exception_type, duration_sec, input_tokens, output_tokens
            FROM episodes
            WHERE batch_id = ?
            ORDER BY task_id ASC, attempt_index ASC, trial_id ASC
            """,
            (batch_id,),
        )
        sel_rows = self.db.fetchall(
            "SELECT task_id, lane, attempts, scored, passed, excluded_json FROM selection_tasks WHERE batch_id = ?",
            (batch_id,),
        )
        sel_map = {r["task_id"]: r for r in sel_rows}
        pass_thr = batch.get("pass_threshold") if finite(batch.get("pass_threshold")) else 1.0

        by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in eps:
            by_task[e["task_id"]].append(e)

        all_task_ids = sorted(set(by_task.keys()) | set(sel_map.keys()))
        max_attempts = max((len(by_task[t]) for t in all_task_ids), default=1)
        items: list[dict[str, Any]] = []
        k_values = [1] + ([max_attempts] if max_attempts > 1 else [])
        k_buckets: dict[int, list[float]] = {k: [] for k in k_values}

        for tid in all_task_ids:
            sub = by_task[tid]
            scored_rows = [r for r in sub if r["status"] == "scored" and finite(r["reward"])]
            n_scored = len(scored_rows)
            n_passed = sum(1 for r in scored_rows if float(r["reward"]) >= float(pass_thr))
            valid_mean = (math.fsum(float(r["reward"]) for r in scored_rows) / n_scored) if n_scored > 0 else None

            p1 = pass_at_k(n_scored, n_passed, 1)
            if p1 is not None:
                k_buckets[1].append(p1)
            pk = pass_at_k(n_scored, n_passed, max_attempts) if max_attempts > 1 else None
            phk = pass_hat_k(n_scored, n_passed, max_attempts) if max_attempts > 1 else None
            if max_attempts > 1 and pk is not None:
                k_buckets[max_attempts].append(pk)

            sel = sel_map.get(tid)
            planned_t = sel["attempts"] if sel else len(sub)
            item: dict[str, Any] = {
                "task_id": tid,
                "planned": planned_t,
                "recorded": len(sub),
                "scored": n_scored,
                "passed": n_passed,
                "valid_mean": round(valid_mean, 4) if valid_mean is not None else None,
                "pass_at_1": round(p1, 4) if p1 is not None else None,
                "attempt_dots": [
                    {
                        "trial_id": r["trial_id"],
                        "status": r["status"],
                        "reward": r["reward"],
                        "failure_kind": r["failure_kind"],
                        "exception_type": r["exception_type"],
                        "duration_sec": r["duration_sec"],
                    }
                    for r in sub
                ],
                "selection_lane": sel["lane"] if sel else None,
            }
            if max_attempts > 1:
                item[f"pass_at_{max_attempts}"] = round(pk, 4) if pk is not None else None
                item[f"pass_hat_{max_attempts}"] = round(phk, 4) if phk is not None else None
            items.append(item)

        total_tasks = len(all_task_ids)
        pass_at_k_summary: dict[str, Any] = {}
        for k in k_values:
            vals = k_buckets[k]
            comp = len(vals)
            mean_v = (math.fsum(vals) / comp) if comp > 0 else None
            ci = wilson_ci95(int(round(math.fsum(vals))), comp) if (k == 1 and max_attempts == 1) else bootstrap_ci95(vals)
            missing = total_tasks - comp
            pass_at_k_summary[f"k{k}"] = {
                "value": round(mean_v, 4) if mean_v is not None else None,
                "computable_tasks": comp,
                "total_tasks": total_tasks,
                "ci95": ci,
                "note": f"{missing} 题因存在失败尝试有效数 <{k} 记缺测" if missing > 0 else None,
            }

        can_read_holdout = has_scope(user, "dataset:holdout_read")
        whole_holdout = self._is_holdout_batch(batch)
        visible_items = items
        hidden_holdout_tasks = 0
        holdout_redacted = False
        redacted_reason = None
        if not can_read_holdout:
            if whole_holdout:
                holdout_redacted = True
                hidden_holdout_tasks = len(items)
                visible_items = []
                redacted_reason = "缺少 dataset:holdout_read 权限，仅展示聚合指标"
            else:
                holdout_tids = self._holdout_task_ids_for_release(batch.get("dataset_release_id"))
                if holdout_tids:
                    visible_items = [it for it in items if it["task_id"] not in holdout_tids]
                    hidden_holdout_tasks = len(items) - len(visible_items)
                    if hidden_holdout_tasks > 0:
                        holdout_redacted = True
                        redacted_reason = (
                            f"缺少 dataset:holdout_read 权限，已隐藏 {hidden_holdout_tasks} 道留出集题目明细（保留完整聚合指标）"
                        )
        return {
            "batch_id": batch_id,
            "unique_tasks": total_tasks,
            "total_attempts": len(eps),
            "pass_at_k_summary": pass_at_k_summary,
            "redacted": holdout_redacted,
            "hidden_holdout_tasks": hidden_holdout_tasks,
            "redacted_reason": redacted_reason,
            "items": visible_items,
            "methodology_version": METHODOLOGY_VERSION,
        }

    def get_batch_episodes(self, user: AuthUser, batch_id: str, page: int = 1, page_size: int = 100) -> dict[str, Any]:
        batch = self.get_batch_detail(user, batch_id)
        if self._is_holdout_batch(batch):
            require_scope(user, "dataset:holdout_read")
        can_read_holdout = has_scope(user, "dataset:holdout_read")
        rel_id = batch.get("dataset_release_id")
        total_all = int(
            self.db.fetchone("SELECT COUNT(*) AS c FROM episodes WHERE batch_id = ?", (batch_id,))["c"]  # type: ignore[index]
        )
        ep_where = "batch_id = ?"
        ep_params: list[Any] = [batch_id]
        if not can_read_holdout and rel_id:
            ep_where += (
                " AND NOT EXISTS ("
                "SELECT 1 FROM task_definitions td "
                "WHERE td.dataset_release_id = ? AND td.task_id = episodes.task_id "
                "AND LOWER(COALESCE(td.split, '')) = 'holdout'"
                ")"
            )
            ep_params.append(rel_id)
        total = int(
            self.db.fetchone(f"SELECT COUNT(*) AS c FROM episodes WHERE {ep_where}", ep_params)["c"]  # type: ignore[index]
        )
        hidden_holdout_episodes = max(0, total_all - total)
        if not can_read_holdout and total_all > 0 and total == 0:
            require_scope(user, "dataset:holdout_read")

        offset = max(0, (page - 1) * page_size)
        rows = self.db.fetchall(
            f"""
            SELECT batch_id, trial_id, canonical_trial_key, generation_id, task_id, task_checksum,
                   attempt_index, status, reward, reward_valid, native_reward_valid, native_reward,
                   exception_type, exception_policy, http_status, failure_kind, failure_summary,
                   failure_host_redacted, failure_log_redacted, verifier_rewards_json,
                   started_at, finished_at, cached_tokens, reasoning_tokens, tool_call_count,
                   phase_timings_json, input_tokens, output_tokens, token_semantics, reasoning_key_source,
                   cost_usd, cost_source, duration_sec, trajectory_status, gateway_status,
                   training_status, has_verifier_output, has_artifacts, has_inline_media
            FROM episodes
            WHERE {ep_where}
            ORDER BY task_id ASC, attempt_index ASC, trial_id ASC
            LIMIT ? OFFSET ?
            """,
            [*ep_params, page_size, offset],
        )
        items = []
        for r in rows:
            r["verifier_rewards"] = json.loads(r.pop("verifier_rewards_json") or "{}")
            pt = r.pop("phase_timings_json")
            r["phase_timings"] = json.loads(pt) if pt else None
            r["reward_valid"] = bool(r["reward_valid"])
            r["has_verifier_output"] = bool(r["has_verifier_output"])
            r["has_artifacts"] = bool(r["has_artifacts"])
            r["has_inline_media"] = bool(r["has_inline_media"])
            items.append(r)
        return {
            "items": items,
            "total": total,
            "total_including_holdout": total_all,
            "redacted": hidden_holdout_episodes > 0,
            "hidden_holdout_episodes": hidden_holdout_episodes,
            "redacted_reason": (
                f"缺少 dataset:holdout_read 权限，已隐藏 {hidden_holdout_episodes} 条留出集试验明细"
                if hidden_holdout_episodes > 0
                else None
            ),
            "page": page,
            "page_size": page_size,
            "methodology_version": METHODOLOGY_VERSION,
        }

    def _load_batch_and_episode(self, user: AuthUser, batch_id: str, trial_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        validate_safe_id(batch_id, "batch_id")
        validate_safe_id(trial_id, "trial_id")
        batch = self.db.fetchone("SELECT * FROM batches WHERE batch_id = ?", (batch_id,))
        if batch is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", "批次不存在")
        require_project_access(user, batch["project_id"], owner_id=batch["owner_id"])
        ep = self.db.fetchone(
            "SELECT * FROM episodes WHERE batch_id = ? AND trial_id = ?",
            (batch_id, trial_id),
        )
        if ep is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", "试验不存在")
        if self._is_holdout_batch_or_task(batch, ep["task_id"]):
            require_scope(user, "dataset:holdout_read")
        return batch, ep

    def get_trial_detail(self, user: AuthUser, batch_id: str, trial_id: str) -> dict[str, Any]:
        batch, ep = self._load_batch_and_episode(user, batch_id, trial_id)
        ev_refs = json.loads(ep.get("evidence_refs_json") or "{}")
        dto: dict[str, Any] = {
            "batch_id": batch_id,
            "generation_id": ep["generation_id"],
            "trial_id": trial_id,
            "canonical_trial_key": ep["canonical_trial_key"],
            "task_id": ep["task_id"],
            "task_checksum": ep["task_checksum"],
            "status": ep["status"],
            "reward": ep["reward"],
            "reward_valid": bool(ep["reward_valid"]),
            "native_reward_valid": bool(ep["native_reward_valid"]) if ep["native_reward_valid"] is not None else None,
            "native_reward": ep["native_reward"],
            "exception_type": ep["exception_type"],
            "exception_policy": ep["exception_policy"],
            "http_status": ep["http_status"],
            "failure": {
                "kind": ep["failure_kind"],
                "summary": ep["failure_summary"],
                "host_redacted": ep["failure_host_redacted"],
                "log_redacted": ep["failure_log_redacted"],
            }
            if ep["failure_kind"] or ep["failure_summary"]
            else None,
            "verifier_rewards": json.loads(ep["verifier_rewards_json"] or "{}"),
            "started_at": ep["started_at"],
            "finished_at": ep["finished_at"],
            "duration_sec": ep["duration_sec"],
            "phase_timings": json.loads(ep["phase_timings_json"]) if ep.get("phase_timings_json") else None,
            "tokens": {
                "semantics": ep["token_semantics"],
                "input": ep["input_tokens"],
                "cached": ep["cached_tokens"],
                "output": ep["output_tokens"],
                "reasoning": ep["reasoning_tokens"],
                "reasoning_key_source": ep["reasoning_key_source"],
            },
            "cost": {"amount_usd": ep["cost_usd"], "source": ep["cost_source"]},
            "tool_call_count": ep["tool_call_count"],
            "trajectory_status": ep["trajectory_status"],
            "gateway_status": ep["gateway_status"],
            "training_status": ep["training_status"],
            "has_verifier_output": bool(ep["has_verifier_output"]),
            "has_artifacts": bool(ep["has_artifacts"]),
            "has_inline_media": bool(ep["has_inline_media"]),
            "evidence_approval": batch["evidence_approval"],
            "methodology_version": METHODOLOGY_VERSION,
        }

        # Direct / VLMEvalKit QA body gated strictly by:
        # 1) approved_evidence_classes containing "direct_vlm_qa"
        # 2) user Scope "direct_vlm_qa:read"
        # 3) holdout cross-check ("dataset:holdout_read" if holdout split)
        # 4) recursive path/credential redaction + answer stripping unless "dataset:answer_read"
        if batch["runner"] in ("direct", "vlmevalkit"):
            if not self._check_evidence_class_approved(batch, "direct_vlm_qa"):
                dto["qa_redacted"] = True
                dto["qa_redacted_reason"] = "来源未批准 direct_vlm_qa 证据类，仅展示状态与分母"
            elif not has_scope(user, "direct_vlm_qa:read"):
                dto["qa_redacted"] = True
                dto["qa_redacted_reason"] = "缺少 direct_vlm_qa:read 权限"
            elif self._is_holdout_batch(batch) and not has_scope(user, "dataset:holdout_read"):
                dto["qa_redacted"] = True
                dto["qa_redacted_reason"] = "留出集样本缺少 dataset:holdout_read 权限"
            else:
                res_ref = ev_refs.get("result") or {}
                b_root = self._resolve_batch_root(batch)
                if b_root and res_ref.get("rel_path"):
                    target = resolve_within_root(b_root, res_ref["rel_path"])
                    raw = self._verify_lazy_file(target, res_ref.get("sha256"))
                    obj = json.loads(raw.decode("utf-8", errors="replace"))
                    strip_ans = not has_scope(user, "dataset:answer_read")
                    qa: dict[str, Any] = {}
                    if "vlmeval_result" in obj and isinstance(obj["vlmeval_result"], dict):
                        vlm_raw = json.loads(
                            _DATA_IMAGE_RE.sub(
                                "<inline-media-via-endpoint>",
                                json.dumps(obj["vlmeval_result"], ensure_ascii=False),
                            )
                        )
                        qa["vlmeval_result"] = redact_json_recursive(vlm_raw, strip_answers=strip_ans)
                    if "direct_result" in obj and isinstance(obj["direct_result"], dict):
                        qa["direct_result"] = redact_json_recursive(obj["direct_result"], strip_answers=strip_ans)
                    # If Direct QA was stored strictly in isolated 0700 evidence_store, load & SHA-256 verify it here!
                    dqa_ref = ev_refs.get("direct_qa")
                    if isinstance(dqa_ref, dict) and dqa_ref.get("rel_path"):
                        dqa_target = self._resolve_evidence_ref_path(batch, dqa_ref)
                        dqa_raw = self._verify_lazy_file(dqa_target, dqa_ref.get("sha256"))
                        dqa_obj = json.loads(dqa_raw.decode("utf-8", errors="replace"))
                        if isinstance(dqa_obj, dict):
                            qa["direct_result"] = redact_json_recursive(dqa_obj, strip_answers=strip_ans)
                    media_list = ev_refs.get("media") or []
                    if not media_list and b_root:
                        lazy_info = _inspect_trial_result_and_media(b_root, res_ref.get("rel_path"), trial_id)
                        media_list = lazy_info["media_refs"]
                    qa["media_count"] = len(media_list)
                    dto["qa"] = qa
                    dto["qa_redacted"] = False
        return dto

    def read_trial_trajectory(
        self,
        user: AuthUser,
        batch_id: str,
        trial_id: str,
        *,
        step_offset: int = 0,
        step_limit: int = 50,
        only_tools: bool = False,
    ) -> dict[str, Any]:
        batch, ep = self._load_batch_and_episode(user, batch_id, trial_id)
        require_scope(user, "trace:read")
        if self._is_holdout_batch(batch):
            require_scope(user, "dataset:holdout_read")
        self._require_evidence_class(batch, "atif_trajectory", "来源未批准 atif_trajectory 证据类，隐藏受限轨迹正文")
        is_vlm = batch["runner"] == "vlmevalkit"
        if is_vlm:
            require_scope(user, "direct_vlm_qa:read")
            self._require_evidence_class(batch, "direct_vlm_qa", "来源未批准 direct_vlm_qa 证据类，隐藏多模态问答轨迹正文")
        strip_vlm_answers = is_vlm and not has_scope(user, "dataset:answer_read")

        ev_refs = json.loads(ep.get("evidence_refs_json") or "{}")
        t_ref = ev_refs.get("trajectory")
        if not t_ref:
            raise AppError(404, "RESOURCE_NOT_FOUND", "该 Trial 不包含 ATIF 轨迹文件")
        target = self._resolve_evidence_ref_path(batch, t_ref)

        raw = self._verify_lazy_file(target, t_ref.get("sha256"))
        data = json.loads(raw.decode("utf-8", errors="replace"))
        raw_steps = data.get("steps") if isinstance(data, dict) and isinstance(data.get("steps"), list) else []

        paired_steps: list[dict[str, Any]] = []
        for idx, step in enumerate(raw_steps):
            if not isinstance(step, dict):
                continue
            obs_map: dict[str, dict[str, Any]] = {}
            for res in (step.get("observation") or {}).get("results") or []:
                if isinstance(res, dict) and res.get("source_call_id"):
                    obs_map[str(res["source_call_id"])] = res
            paired_tools = []
            for tc in step.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                tc_id = str(tc.get("tool_call_id") or "")
                obs = obs_map.get(tc_id)
                paired_tools.append(
                    {
                        "tool_call_id": tc_id,
                        "function_name": tc.get("function_name"),
                        "arguments": redact_json_recursive(tc.get("arguments")),
                        "observation": {
                            "content": redact_text(str(obs.get("content", ""))) if obs and obs.get("content") is not None else None,
                            "is_error": bool(obs.get("is_error")) if obs else False,
                        }
                        if obs
                        else None,
                    }
                )
            if only_tools and not paired_tools:
                continue
            paired_steps.append(
                {
                    "step_id": step.get("step_id", idx + 1),
                    "source": step.get("source"),
                    "timestamp": step.get("timestamp"),
                    "message": redact_json_recursive(step.get("message"), strip_answers=strip_vlm_answers),
                    "reasoning_content": redact_text(step.get("reasoning_content")) if isinstance(step.get("reasoning_content"), str) else None,
                    "paired_tools": paired_tools,
                    "metrics": step.get("metrics"),
                }
            )

        sliced = paired_steps[step_offset : step_offset + step_limit]
        return {
            "batch_id": batch_id,
            "generation_id": ep["generation_id"],
            "trial_id": trial_id,
            "trajectory_status": ep["trajectory_status"],
            "total_steps": len(paired_steps),
            "step_offset": step_offset,
            "step_limit": step_limit,
            "agent": data.get("agent") if isinstance(data, dict) else None,
            "token_semantics": ep["token_semantics"],
            "final_metrics": data.get("final_metrics") if isinstance(data, dict) else None,
            "steps": sliced,
            "methodology_version": METHODOLOGY_VERSION,
        }

    def read_trial_gateway(
        self,
        user: AuthUser,
        batch_id: str,
        trial_id: str,
        *,
        turn_offset: int = 0,
        turn_limit: int = 10,
        include_tools: bool = False,
    ) -> dict[str, Any]:
        batch, ep = self._load_batch_and_episode(user, batch_id, trial_id)
        ev_refs = json.loads(ep.get("evidence_refs_json") or "{}")
        g_ref = ev_refs.get("gateway")
        if not g_ref:
            raise AppError(404, "RESOURCE_NOT_FOUND", "该 Trial 不包含网关抓包文件")
        if "pending_file_identity" in g_ref:
            raise AppError(409, "GENERATION_MISMATCH", "网关明细尚未完成当前版本摘要校验，请刷新后重试")
        target = self._resolve_evidence_ref_path(batch, g_ref)
        raw = self._verify_lazy_file(target, g_ref.get("sha256"))
        data = json.loads(raw.decode("utf-8", errors="replace"))
        turns = data.get("turns") if isinstance(data, dict) and isinstance(data.get("turns"), list) else []

        summary = {
            "model_calls": data.get("model_calls", len(turns)) if isinstance(data, dict) else len(turns),
            "validated": data.get("validated") if isinstance(data, dict) else None,
            "turn_count": len(turns),
            "discarded_turns": sum(1 for t in turns if isinstance(t, dict) and t.get("discarded")),
            "finish_reasons": sorted({str(t.get("finish_reason")) for t in turns if isinstance(t, dict) and t.get("finish_reason")}),
        }

        can_full = (
            self._check_evidence_class_approved(batch, "gateway_capture")
            and has_scope(user, "gateway:read")
            and (not self._is_holdout_batch(batch) or has_scope(user, "dataset:holdout_read"))
        )
        if not can_full:
            return {
                "batch_id": batch_id,
                "generation_id": ep["generation_id"],
                "trial_id": trial_id,
                "redacted": True,
                "summary": summary,
                "turns": [],
                "methodology_version": METHODOLOGY_VERSION,
            }

        sliced = []
        for t in turns[turn_offset : turn_offset + turn_limit]:
            if not isinstance(t, dict):
                continue
            item = dict(t)
            if not include_tools:
                item.pop("request_tools", None)
            sliced.append(redact_json_recursive(item))

        return {
            "batch_id": batch_id,
            "generation_id": ep["generation_id"],
            "trial_id": trial_id,
            "redacted": False,
            "summary": summary,
            "turn_offset": turn_offset,
            "turn_limit": turn_limit,
            "turns": sliced,
            "methodology_version": METHODOLOGY_VERSION,
        }

    def read_trial_verifier_output(self, user: AuthUser, batch_id: str, trial_id: str) -> dict[str, Any]:
        batch, ep = self._load_batch_and_episode(user, batch_id, trial_id)
        require_scope(user, "verifier_log:read")
        if self._is_holdout_batch(batch):
            require_scope(user, "dataset:holdout_read")
        self._require_evidence_class(batch, "verifier_log", "来源未批准 verifier_log 证据类，隐藏验证器日志")

        ev_refs = json.loads(ep.get("evidence_refs_json") or "{}")
        v_ref = ev_refs.get("verifier_output")
        if not v_ref:
            raise AppError(404, "RESOURCE_NOT_FOUND", "该批次未包含验证器原始日志")
        target = self._resolve_evidence_ref_path(batch, v_ref)
        raw = self._verify_lazy_file(target, v_ref.get("sha256"))
        truncated = len(raw) > 131072
        if truncated:
            text = (
                raw[:32768].decode("utf-8", errors="replace")
                + "\n... [truncated to 128 KiB: head 32 KiB + tail 96 KiB] ...\n"
                + raw[-98304:].decode("utf-8", errors="replace")
            )
        else:
            text = raw.decode("utf-8", errors="replace")
        return {
            "batch_id": batch_id,
            "generation_id": ep["generation_id"],
            "trial_id": trial_id,
            "truncated": truncated,
            "size_bytes": len(raw),
            "content": redact_text(text),
            "methodology_version": METHODOLOGY_VERSION,
        }

    def read_trial_artifacts(self, user: AuthUser, batch_id: str, trial_id: str) -> dict[str, Any]:
        batch, ep = self._load_batch_and_episode(user, batch_id, trial_id)
        require_scope(user, "verifier_log:read")
        if self._is_holdout_batch(batch):
            require_scope(user, "dataset:holdout_read")
        self._require_evidence_class(batch, "verifier_log", "来源未批准 verifier_log 证据类，隐藏沙箱产物")

        ev_refs = json.loads(ep.get("evidence_refs_json") or "{}")
        items: list[dict[str, Any]] = []
        res_ref = ev_refs.get("result") or {}
        art_dir: Path | None = None
        if res_ref.get("abs_path"):
            cand = Path(res_ref["abs_path"]).parent / "artifacts"
            if cand.is_dir():
                art_dir = cand
        else:
            b_root = self._resolve_batch_root(batch)
            if b_root:
                try:
                    cand = resolve_within_root(b_root, f"trials/{trial_id}/artifacts")
                    if cand.is_dir():
                        art_dir = cand
                except AppError:
                    art_dir = None

        if art_dir is not None:
            for p in sorted(art_dir.rglob("*"))[:50]:
                if not p.is_file():
                    continue
                rel = str(p.relative_to(art_dir))
                try:
                    sz = p.stat().st_size
                except OSError:
                    continue
                preview = None
                if sz <= 32768 and p.suffix.lower() in (".patch", ".diff", ".txt", ".log", ".json", ".py", ".md", ".sh", ".yaml", ".yml"):
                    try:
                        preview = redact_text(p.read_text(encoding="utf-8", errors="replace"))
                    except OSError:
                        preview = None
                items.append({"rel_path": rel, "size_bytes": sz, "preview": preview})

        return {
            "batch_id": batch_id,
            "generation_id": ep["generation_id"],
            "trial_id": trial_id,
            "items": items,
            "total": len(items),
            "methodology_version": METHODOLOGY_VERSION,
        }

    def read_trial_training_capture(self, user: AuthUser, batch_id: str, trial_id: str) -> dict[str, Any]:
        batch, ep = self._load_batch_and_episode(user, batch_id, trial_id)
        require_scope(user, "trace:read")
        if self._is_holdout_batch(batch):
            require_scope(user, "dataset:holdout_read")
        ev_refs = json.loads(ep.get("evidence_refs_json") or "{}")
        tr_ref = ev_refs.get("training")
        if not tr_ref:
            return {
                "batch_id": batch_id,
                "generation_id": ep["generation_id"],
                "trial_id": trial_id,
                "captured": False,
                "training_status": ep.get("training_status") or "not_captured",
                "message": "该 Trial 未开启训练轨迹采集（not_captured）",
                "methodology_version": METHODOLOGY_VERSION,
            }
        target = self._resolve_evidence_ref_path(batch, tr_ref)
        raw = self._verify_lazy_file(target, tr_ref.get("sha256"))
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            obj = {}
        return {
            "batch_id": batch_id,
            "generation_id": ep["generation_id"],
            "trial_id": trial_id,
            "captured": True,
            "training_status": ep.get("training_status") or "captured",
            "trace_sha256": tr_ref.get("sha256") or _sha256_bytes(raw),
            "summary": redact_json_recursive(obj) if isinstance(obj, dict) else {},
            "methodology_version": METHODOLOGY_VERSION,
        }

    def read_trial_media(self, user: AuthUser, batch_id: str, trial_id: str, idx: int) -> tuple[bytes, str]:
        """Serve trial media with strict scope, evidence-class approval, holdout check,
        AND full SHA-256 verification (raises 409 GENERATION_MISMATCH if overwritten on disk!)."""
        batch, ep = self._load_batch_and_episode(user, batch_id, trial_id)
        require_scope(user, "direct_vlm_qa:read")
        if self._is_holdout_batch(batch):
            require_scope(user, "dataset:holdout_read")
        self._require_evidence_class(batch, "direct_vlm_qa", "来源未批准 direct_vlm_qa 证据类，隐藏多模态图像")

        ev_refs = json.loads(ep.get("evidence_refs_json") or "{}")
        media_list = ev_refs.get("media") or []
        b_root = self._resolve_batch_root(batch)
        if not b_root:
            raise AppError(404, "RESOURCE_NOT_FOUND", "无法解析批次目录")

        # Verify indexed result.json SHA-256 BEFORE lazy fallback so overwriting result.json
        # (even removing the inline image) ALWAYS triggers 409 GENERATION_MISMATCH rather than 404!
        res_ref = ev_refs.get("result") or {}
        if not media_list and res_ref.get("rel_path"):
            res_target = resolve_within_root(b_root, res_ref["rel_path"])
            verified_res_bytes = self._verify_lazy_file(res_target, res_ref.get("sha256"))
            lazy_info = _inspect_trial_result_and_media(
                b_root,
                res_ref.get("rel_path"),
                trial_id,
                raw_result_bytes=verified_res_bytes,
            )
            media_list = lazy_info["media_refs"]

        if idx < 0 or idx >= len(media_list):
            raise AppError(404, "RESOURCE_NOT_FOUND", f"媒体下标越界: {idx}")
        entry = media_list[idx]

        src_kind = entry.get("source")
        if src_kind == "trial_media_dir":
            target = self._resolve_evidence_ref_path(batch, entry)
            data = self._verify_lazy_file(target, entry.get("sha256"))
            ext = target.suffix.lower()
            mime = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".gif": "image/gif",
            }.get(ext, "application/octet-stream")
            return data, mime

        if src_kind in ("result_inline_base64", "trajectory_inline_base64"):
            ev_kind = "result" if src_kind == "result_inline_base64" else "trajectory"
            ev_entry = ev_refs.get(ev_kind) or {}
            target = (
                self._resolve_evidence_ref_path(batch, ev_entry)
                if ev_entry.get("storage_tier") == "evidence_store_0700"
                else resolve_within_root(b_root, entry["rel_path"])
            )
            # Prefer indexed generation SHA from ev_refs over lazily re-discovered file_sha256!
            expected_file_sha = ev_entry.get("sha256") or entry.get("file_sha256")
            raw_bytes = self._verify_lazy_file(target, expected_file_sha)
            raw_text = raw_bytes.decode("utf-8", errors="replace")
            matches = list(_DATA_IMAGE_RE.finditer(raw_text))
            ord_idx = int(entry.get("inline_ordinal", 0))
            if ord_idx >= len(matches):
                raise AppError(
                    409,
                    "GENERATION_MISMATCH",
                    "内联图像在当前磁盘版本中已缺失，拒绝跨代读取",
                )
            m = matches[ord_idx]
            mime = m.group(1)
            b64_clean = re.sub(r"\s+", "", m.group(2))
            expected_img_sha = entry.get("sha256")
            if expected_img_sha:
                actual_img_sha = hashlib.sha256(b64_clean.encode("ascii")).hexdigest()
                if actual_img_sha != expected_img_sha:
                    raise AppError(
                        409,
                        "GENERATION_MISMATCH",
                        "检测到内联图像跨代覆写（图像摘要不匹配），拒绝跨代返回",
                    )
            return base64.b64decode(b64_clean), mime

        raise AppError(404, "RESOURCE_NOT_FOUND", "不支持的媒体引用类型")

    # --- 4. Leaderboard & ARC Prize Efficiency Scatter (Auth-Before-Dedup + Protocol/Scope Isolation!) ---

    def get_leaderboard(
        self,
        user: AuthUser,
        *,
        track_slug: str | None = None,
        run_scope: str | None = None,
        include_legacy: bool = True,
        protocol: str | None = None,
        harness: str | None = None,
        runner: str | None = None,
        source_namespace: str | None = None,
        thinking_mode: str | None = None,
        trained_only: bool = False,
        has_atif: bool = False,
        has_gateway: bool = False,
        model_family: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """Production leaderboard strictly enforcing:
        1) Auth-before-dedup via trial_protocol_scores window SQL (`WHERE project_id IN (...)` BEFORE `ROW_NUMBER()`).
        2) Strict protocol & run_scope isolation: `best_by_task` and `comparative_macro_avg` NEVER mix different
           `scoring_protocol` or different `run_scope` (default best_by_task scope is `full_split` unless explicitly overridden).
        3) Includes `legacy_leaderboard_rows` (2026-11-20/leaderboard.csv) cleanly under `legacy-20261120-csv` when `include_legacy=true`."""
        proj_sql, params = _project_filter_sql(user, "b.project_id")
        where = [proj_sql, "b.is_running_placeholder = 0", "b.dataset_release_id IS NOT NULL"]
        if track_slug and track_slug != "all":
            where.append("bm.track_slug = ?")
            params.append(track_slug)
        if run_scope:
            where.append("b.run_scope = ?")
            params.append(run_scope)
        if protocol:
            where.append("b.scoring_protocol = ?")
            params.append(protocol)
        if harness:
            where.append("COALESCE(b.harness, b.runner) = ?")
            params.append(harness)
        if runner:
            where.append("b.runner = ?")
            params.append(runner)
        if source_namespace:
            where.append("b.source_namespace = ?")
            params.append(source_namespace)
        if thinking_mode:
            where.append("b.thinking_mode = ?")
            params.append(thinking_mode)
        if trained_only:
            where.append("b.checkpoint IS NOT NULL AND b.checkpoint != '' AND b.checkpoint != 'base'")
        if has_gateway:
            where.append("b.has_gateway = 1")
        if has_atif:
            where.append("EXISTS (SELECT 1 FROM episodes e2 WHERE e2.batch_id = b.batch_id AND e2.trajectory_status IS NOT NULL)")
        if model_family:
            where.append("LOWER(COALESCE(b.model_canonical, b.model_route, '')) LIKE ?")
            params.append(f"%{model_family.strip().lower()}%")
        if not include_legacy:
            where.append("b.source_namespace IN ('harbor_runs', 'runner_published')")

        # Auth-before-dedup trial aggregation per (model, harness, release, scoring_protocol, run_scope)
        dedup_rows = self.db.fetchall(
            f"""
            WITH auth_tps AS (
                SELECT t.canonical_trial_key, t.scoring_protocol, t.batch_id, t.dataset_release_id,
                       t.status, t.reward, t.reward_valid,
                       b.model_canonical, b.model_route, b.harness, b.harness_version, b.runner,
                       b.run_scope, b.source_namespace, b.raw_dataset_name, b.official_status,
                       b.planned, b.recorded, b.scored, b.coverage, b.valid_mean, b.benchmark_slug,
                       bm.track_slug, bm.track_display_name, bm.capability, bm.display_name AS bm_display_name,
                       r.scale_type, r.default_protocol,
                       ROW_NUMBER() OVER (
                           PARTITION BY t.canonical_trial_key, t.scoring_protocol
                           ORDER BY t.source_priority_rank ASC, t.batch_id ASC
                       ) AS rn
                FROM trial_protocol_scores t
                JOIN batches b ON b.batch_id = t.batch_id
                JOIN benchmarks bm ON bm.benchmark_slug = b.benchmark_slug
                JOIN dataset_releases r ON r.dataset_release_id = b.dataset_release_id
                WHERE {' AND '.join(where)}
            )
            SELECT model_canonical, model_route, harness, harness_version, runner,
                   benchmark_slug, dataset_release_id, raw_dataset_name, bm_display_name,
                   capability, scale_type, default_protocol, run_scope, scoring_protocol,
                   source_namespace, official_status,
                   MIN(batch_id) AS sample_batch_id,
                   COUNT(*) AS dedup_recorded,
                   SUM(CASE WHEN reward_valid = 1 THEN 1 ELSE 0 END) AS dedup_scored,
                   AVG(CASE WHEN reward_valid = 1 THEN reward END) AS dedup_valid_mean
            FROM auth_tps
            WHERE rn = 1
            GROUP BY COALESCE(model_canonical, model_route, 'unknown'),
                     COALESCE(harness, runner, 'unknown'),
                     harness_version, dataset_release_id, scoring_protocol, run_scope
            """,
            params,
        )

        # Also fetch official_results-only batches (e.g. vlmevalkit summary-only CMMMU where scored == 0)
        official_only_rows = self.db.fetchall(
            f"""
            SELECT b.batch_id, b.model_canonical, b.model_route, b.harness, b.harness_version, b.runner,
                   b.benchmark_slug, b.dataset_release_id, b.raw_dataset_name, b.run_scope,
                   b.scoring_protocol, b.source_namespace, b.official_status, b.planned, b.recorded, b.scored,
                   bm.capability, bm.display_name AS bm_display_name, r.scale_type, r.default_protocol,
                   o.score AS official_score, o.denominator AS official_denominator
            FROM batches b
            JOIN benchmarks bm ON bm.benchmark_slug = b.benchmark_slug
            JOIN dataset_releases r ON r.dataset_release_id = b.dataset_release_id
            JOIN official_results o ON o.batch_id = b.batch_id
            WHERE {' AND '.join(where)}
            ORDER BY COALESCE(b.finished_at, b.indexed_at) DESC, b.batch_id ASC
            """,
            params,
        )
        off_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for o in official_only_rows:
            mc = o.get("model_canonical") or o.get("model_route") or "unknown"
            har = o.get("harness") or o.get("runner") or "unknown"
            hv = o.get("harness_version")
            sid = f"model:{mc}|harness:{har}" + (f"@{hv}" if hv else "")
            off_by_key.setdefault((sid, o["dataset_release_id"], o["scoring_protocol"], o["run_scope"]), o)

        subjects: dict[str, dict[str, Any]] = {}
        subject_scores: dict[str, dict[str, tuple[float | None, str | None]]] = defaultdict(dict)
        # Key best_by_task strictly by (dataset_release_id, scoring_protocol, run_scope) -> ZERO cross-protocol/cross-scope mixing!
        best_scope_filter = run_scope or "full_split"
        best_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}

        def _cell_key(rel_id: str, proto: str, rscope: str) -> str:
            """Preserve every dimension NOT explicitly constrained by query filters:
            - No filters -> `rel|proto|scope`
            - Only `protocol` filtered -> `rel|scope` (keeps full_split AND smoke!)
            - Only `run_scope` filtered -> `rel|proto` (keeps protocol A AND protocol B!)
            - Both `protocol` & `run_scope` filtered -> `rel`"""
            parts = [rel_id]
            if protocol is None:
                parts.append(proto)
            if run_scope is None:
                parts.append(rscope)
            return "|".join(parts)

        def _ensure_subject(sid: str, mc: str, har: str, hv: str | None, runner: str) -> None:
            if sid not in subjects:
                subjects[sid] = {
                    "subject_id": sid,
                    "model_canonical": mc,
                    "harness": har,
                    "harness_version": hv,
                    "runner": runner,
                    "benchmarks": {},
                }

        for r in dedup_rows:
            mc = r.get("model_canonical") or r.get("model_route") or "unknown"
            har = r.get("harness") or r.get("runner") or "unknown"
            hv = r.get("harness_version")
            sid = f"model:{mc}|harness:{har}" + (f"@{hv}" if hv else "")
            _ensure_subject(sid, mc, har, hv, r["runner"])

            rel_id = r["dataset_release_id"]
            proto = r["scoring_protocol"]
            rscope = r["run_scope"]
            off_match = off_by_key.get((sid, rel_id, proto, rscope))
            if off_match is not None and off_match.get("official_score") is not None:
                raw_score = float(off_match["official_score"])
                denom_kind = "official_summary" if int(r["dedup_scored"] or 0) == 0 else "official_verified"
                batch_id_ref = off_match["batch_id"]
                planned = int(off_match["official_denominator"])
                cov_str = f"{planned}/{planned}"
                official_status = off_match["official_status"]
                cell_source_namespace = off_match["source_namespace"]
            else:
                raw_score = float(r["dedup_valid_mean"]) if r["dedup_valid_mean"] is not None else None
                denom_kind = "dedup_valid"
                batch_id_ref = r["sample_batch_id"]
                cov_str = f"{r['dedup_scored']}/{r['dedup_recorded']}"
                planned = int(r["dedup_recorded"] or 0)
                official_status = r["official_status"]
                cell_source_namespace = r["source_namespace"]

            norm_score = round(normalize_score_to_100(raw_score, r["scale_type"]), 2) if raw_score is not None else None
            cell_key = _cell_key(rel_id, proto, rscope)

            if cell_key not in subjects[sid]["benchmarks"]:
                subjects[sid]["benchmarks"][cell_key] = {
                    "benchmark_slug": r["benchmark_slug"],
                    "dataset_release_id": rel_id,
                    "raw_dataset_name": r["raw_dataset_name"] or r["bm_display_name"],
                    "score": norm_score,
                    "raw_score": raw_score,
                    "scale_type": r["scale_type"],
                    "run_scope": rscope,
                    "scoring_protocol": proto,
                    "official_status": official_status,
                    "source_type": cell_source_namespace,
                    "scored": int(r["dedup_scored"] or 0),
                    "planned": planned,
                    "batch_id": batch_id_ref,
                }
                # Include in macro avg strictly under (rel_id, proto, rscope) composite key so different protocols/scopes NEVER mix!
                subject_scores[sid][f"{rel_id}|{proto}|{rscope}"] = (norm_score, f"{proto}|{rscope}")

                # best_by_task ONLY compares entries with the SAME (dataset_release_id, scoring_protocol, run_scope)
                # and defaults to formal full_split unless caller explicitly requested another run_scope!
                if norm_score is not None and rscope == best_scope_filter:
                    bk = (rel_id, proto, rscope)
                    cur_best = best_by_key.get(bk)
                    if cur_best is None or norm_score > cur_best["score"]:
                        best_by_key[bk] = {
                            "dimension": r["capability"],
                            "benchmark_slug": r["benchmark_slug"],
                            "dataset_release_id": rel_id,
                            "model_canonical": mc,
                            "harness": har,
                            "runner": r["runner"],
                            "run_scope": rscope,
                            "scoring_protocol": proto,
                            "denominator_kind": denom_kind,
                            "score": norm_score,
                            "coverage": cov_str,
                            "batch_id": batch_id_ref,
                        }

        # Also surface official_results-only batches that have 0 trial_protocol_scores rows
        seen_off_keys = {
            (
                f"model:{r.get('model_canonical') or r.get('model_route') or 'unknown'}|harness:{r.get('harness') or r.get('runner') or 'unknown'}"
                + (f"@{r.get('harness_version')}" if r.get("harness_version") else ""),
                r["dataset_release_id"],
                r["scoring_protocol"],
                r["run_scope"],
            )
            for r in dedup_rows
        }
        for (sid, rel_id, proto, rscope), o in off_by_key.items():
            if (sid, rel_id, proto, rscope) in seen_off_keys or o.get("official_score") is None:
                continue
            mc = o.get("model_canonical") or o.get("model_route") or "unknown"
            har = o.get("harness") or o.get("runner") or "unknown"
            hv = o.get("harness_version")
            _ensure_subject(sid, mc, har, hv, o["runner"])
            raw_score = float(o["official_score"])
            norm_score = round(normalize_score_to_100(raw_score, o["scale_type"]), 2)
            cell_key = _cell_key(rel_id, proto, rscope)
            if cell_key not in subjects[sid]["benchmarks"]:
                subjects[sid]["benchmarks"][cell_key] = {
                    "benchmark_slug": o["benchmark_slug"],
                    "dataset_release_id": rel_id,
                    "raw_dataset_name": o["raw_dataset_name"] or o["bm_display_name"],
                    "score": norm_score,
                    "raw_score": raw_score,
                    "scale_type": o["scale_type"],
                    "run_scope": rscope,
                    "scoring_protocol": proto,
                    "official_status": o["official_status"],
                    "source_type": o["source_namespace"],
                    "scored": int(o.get("scored") or 0),
                    "planned": int(o.get("official_denominator") or o.get("planned") or 0),
                    "batch_id": o["batch_id"],
                }
                subject_scores[sid][f"{rel_id}|{proto}|{rscope}"] = (norm_score, f"{proto}|{rscope}")
                if rscope == best_scope_filter:
                    bk = (rel_id, proto, rscope)
                    cur_best = best_by_key.get(bk)
                    if cur_best is None or norm_score > cur_best["score"]:
                        best_by_key[bk] = {
                            "dimension": o["capability"],
                            "benchmark_slug": o["benchmark_slug"],
                            "dataset_release_id": rel_id,
                            "model_canonical": mc,
                            "harness": har,
                            "runner": o["runner"],
                            "run_scope": rscope,
                            "scoring_protocol": proto,
                            "denominator_kind": "official_summary",
                            "score": norm_score,
                            "coverage": f"{o['official_denominator']}/{o['official_denominator']}",
                            "batch_id": o["batch_id"],
                        }

        # Surface legacy 2026-11-20 leaderboard.csv rows (IFEval, AI2D_TEST, RealWorldQA, etc.) when include_legacy=true
        if (
            include_legacy
            and not has_atif
            and not has_gateway
            and (source_namespace in (None, "legacy_20261120_csv"))
            and (runner in (None, "legacy"))
            and (protocol is None or protocol == "legacy-20261120-csv")
            and (run_scope in (None, "full_split"))
        ):
            # Check user has access to the project governing 2026-11-20 leaderboard.csv
            if self._can_see_legacy_csv(user):
                leg_where = ["lr.dataset_release_id IS NOT NULL"]
                leg_params: list[Any] = []
                if track_slug and track_slug != "all":
                    leg_where.append("bm.track_slug = ?")
                    leg_params.append(track_slug)
                if harness:
                    leg_where.append("lr.harness = ?")
                    leg_params.append(harness)
                if thinking_mode:
                    leg_where.append("lr.thinking_mode = ?")
                    leg_params.append(thinking_mode)
                if trained_only:
                    leg_where.append("lr.checkpoint IS NOT NULL AND lr.checkpoint != '' AND lr.checkpoint != 'base'")
                if model_family:
                    leg_where.append("LOWER(lr.model_canonical) LIKE ?")
                    leg_params.append(f"%{model_family.strip().lower()}%")
                leg_rows = self.db.fetchall(
                    f"""
                    SELECT lr.*, bm.capability, bm.display_name AS bm_display_name
                    FROM legacy_leaderboard_rows lr
                    JOIN benchmarks bm ON bm.benchmark_slug = lr.benchmark_slug
                    WHERE {' AND '.join(leg_where)}
                    """,
                    leg_params,
                )
                for lr in leg_rows:
                    mc = lr["model_canonical"]
                    ck = lr.get("checkpoint")
                    th = lr.get("thinking_mode")
                    har = lr["harness"]
                    label_suffix = "".join(
                        [f"_{ck}" if ck else "", f"_{th}" if th else ""]
                    )
                    sid = f"model:{mc}{label_suffix}|harness:{har}"
                    _ensure_subject(sid, f"{mc}{label_suffix}", har, None, "legacy")
                    rel_id = lr["dataset_release_id"]
                    proto = "legacy-20261120-csv"
                    rscope = "full_split"
                    norm_score = round(float(lr["norm_score_100"]), 2)
                    cell_key = _cell_key(rel_id, proto, rscope)
                    if cell_key not in subjects[sid]["benchmarks"]:
                        subjects[sid]["benchmarks"][cell_key] = {
                            "benchmark_slug": lr["benchmark_slug"],
                            "dataset_release_id": rel_id,
                            "raw_dataset_name": lr["benchmark"],
                            "score": norm_score,
                            "raw_score": lr["raw_score"],
                            "scale_type": lr["scale_type"],
                            "run_scope": rscope,
                            "scoring_protocol": proto,
                            "official_status": "unofficial_valid_mean",
                            "source_type": "legacy_20261120_csv",
                            "batch_id": None,
                        }
                        subject_scores[sid][f"{rel_id}|{proto}|{rscope}"] = (norm_score, f"{proto}|{rscope}")
                        if rscope == best_scope_filter:
                            bk = (rel_id, proto, rscope)
                            cur_best = best_by_key.get(bk)
                            if cur_best is None or norm_score > cur_best["score"]:
                                best_by_key[bk] = {
                                    "dimension": lr["capability"],
                                    "benchmark_slug": lr["benchmark_slug"],
                                    "dataset_release_id": rel_id,
                                    "model_canonical": f"{mc}{label_suffix}",
                                    "harness": har,
                                    "runner": "legacy",
                                    "run_scope": rscope,
                                    "scoring_protocol": proto,
                                    "denominator_kind": "legacy_csv_summary",
                                    "score": norm_score,
                                    "coverage": "full_split",
                                    "batch_id": None,
                                }

        # Enrich subjects with real efficiency & token telemetry (duration, tokens, token_semantics, has_gateway)
        eff_rows = self.db.fetchall(
            f"""
            SELECT COALESCE(b.model_canonical, b.model_route, 'unknown') AS mc,
                   COALESCE(b.harness, b.runner, 'unknown') AS har,
                   b.harness_version AS hv,
                   AVG(e.duration_sec) AS avg_dur,
                   AVG(e.input_tokens) AS avg_in,
                   AVG(e.cached_tokens) AS avg_cache,
                   AVG(e.output_tokens) AS avg_out,
                   AVG(e.reasoning_tokens) AS avg_reason,
                   MAX(b.has_gateway) AS has_gw,
                   MAX(CASE WHEN e.trajectory_status IS NOT NULL THEN 1 ELSE 0 END) AS has_atif,
                   MAX(b.token_semantics) AS tok_sem,
                   MAX(b.checkpoint) AS ck,
                   MAX(b.thinking_mode) AS th,
                   MAX(COALESCE(b.finished_at, b.indexed_at)) AS latest_ts
            FROM batches b
            LEFT JOIN episodes e ON e.batch_id = b.batch_id
            WHERE {proj_sql} AND b.is_running_placeholder = 0
            GROUP BY mc, har, hv
            """,
            _project_filter_sql(user, "b.project_id")[1],
        )
        eff_by_sid: dict[str, dict[str, Any]] = {}
        for er in eff_rows:
            sid_e = f"model:{er['mc']}|harness:{er['har']}" + (f"@{er['hv']}" if er.get("hv") else "")
            eff_by_sid[sid_e] = er

        macro = compute_comparative_macro_avg(dict(subject_scores)) if subject_scores else {
            "common_comparable_benchmarks": [],
            "by_subject": {},
        }
        items = []
        for sid, sdata in subjects.items():
            m_info = macro["by_subject"].get(sid, {})
            er = eff_by_sid.get(sid) or {}
            items.append(
                {
                    "subject_id": sid,
                    "model_canonical": sdata["model_canonical"],
                    "harness": sdata["harness"],
                    "harness_version": sdata["harness_version"],
                    "runner": sdata["runner"],
                    "checkpoint": er.get("ck"),
                    "thinking_mode": er.get("th"),
                    "has_gateway": bool(er.get("has_gw")),
                    "has_atif": bool(er.get("has_atif")),
                    "latest_at": er.get("latest_ts"),
                    "avg_duration_sec": round(float(er["avg_dur"]), 2) if er.get("avg_dur") is not None else None,
                    "tokens": {
                        "semantics": er.get("tok_sem") or "none",
                        "input": round(float(er["avg_in"])) if er.get("avg_in") is not None else None,
                        "cached": round(float(er["avg_cache"])) if er.get("avg_cache") is not None else None,
                        "output": round(float(er["avg_out"])) if er.get("avg_out") is not None else None,
                        "reasoning": round(float(er["avg_reason"])) if er.get("avg_reason") is not None else None,
                    },
                    "comparative_macro_avg": m_info.get("comparative_macro_avg"),
                    "comparative_bench_count": m_info.get("comparative_bench_count", 0),
                    "self_coverage_avg": m_info.get("self_coverage_avg"),
                    "self_bench_count": m_info.get("self_bench_count", 0),
                    "benchmarks": sdata["benchmarks"],
                }
            )
        items.sort(
            key=lambda x: (
                x["comparative_macro_avg"] is not None,
                x["comparative_macro_avg"] if x["comparative_macro_avg"] is not None else -1.0,
                x["self_coverage_avg"] if x["self_coverage_avg"] is not None else -1.0,
            ),
            reverse=True,
        )
        for rank_idx, it in enumerate(items, 1):
            it["rank"] = rank_idx

        # Collect 100% real Harbor/historical filter facet values (NO fabricated License/Country/Hardware/Price!)
        facet_harnesses = sorted({str(it["harness"]) for it in items if it.get("harness")})
        facet_runners = sorted({str(it["runner"]) for it in items if it.get("runner")})
        facet_protocols = sorted(
            {
                str(cell["scoring_protocol"])
                for it in items
                for cell in (it.get("benchmarks") or {}).values()
                if isinstance(cell, dict) and cell.get("scoring_protocol")
            }
        )

        total = len(items)
        offset = max(0, (page - 1) * page_size)
        track_disp = track_slug or "all"
        if track_slug and track_slug != "all":
            trow = self.db.fetchone("SELECT track_display_name FROM benchmarks WHERE track_slug = ? LIMIT 1", (track_slug,))
            if trow:
                track_disp = trow["track_display_name"]

        return {
            "methodology_version": METHODOLOGY_VERSION,
            "track_slug": track_slug or "all",
            "track_display_name": track_disp,
            "common_comparable_benchmarks": macro["common_comparable_benchmarks"],
            "best_by_task": list(best_by_key.values()),
            "available_filters": {
                "harnesses": facet_harnesses,
                "runners": facet_runners,
                "protocols": facet_protocols,
            },
            "items": items[offset : offset + page_size],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def get_efficiency_scatter(
        self,
        user: AuthUser,
        *,
        dataset_release_id: str,
        scoring_protocol: str = "harbor-native-reward-valid-v1",
        x_axis: str = "duration_p50_sec",
        task_set_policy: str = "fixed_y_common",
    ) -> dict[str, Any]:
        validate_safe_id(dataset_release_id, "dataset_release_id")
        rel = self.db.fetchone(
            """
            SELECT r.*, bm.display_name AS bm_display_name
            FROM dataset_releases r
            JOIN benchmarks bm ON bm.benchmark_slug = r.benchmark_slug
            WHERE r.dataset_release_id = ?
            """,
            (dataset_release_id,),
        )
        if rel is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"数据集版本不存在: {dataset_release_id}")

        proj_sql, params = _project_filter_sql(user, "project_id")
        batches = self.db.fetchall(
            f"""
            SELECT * FROM batches
            WHERE {proj_sql}
              AND dataset_release_id = ?
              AND scoring_protocol = ?
              AND is_running_placeholder = 0
            ORDER BY batch_name ASC
            """,
            [*params, dataset_release_id, scoring_protocol],
        )
        if not batches:
            return {
                "methodology_version": METHODOLOGY_VERSION,
                "benchmark_slug": rel["benchmark_slug"],
                "dataset_release_id": dataset_release_id,
                "raw_dataset_name": rel["bm_display_name"],
                "scoring_protocol": scoring_protocol,
                "task_set_policy": task_set_policy,
                "common_valid_tasks": 0,
                "x_axis": x_axis,
                "x_axis_coverage": {},
                "points": [],
                "series_connectors": [],
                "pareto_frontier_batch_ids": [],
            }

        batch_eps: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
        for b in batches:
            eps = self.db.fetchall(
                """
                SELECT task_id, task_checksum, status, reward, reward_valid,
                       duration_sec, input_tokens, output_tokens, tool_call_count, cost_usd, cost_source
                FROM episodes
                WHERE batch_id = ?
                """,
                (b["batch_id"],),
            )
            valid_map: dict[tuple[str, str], dict[str, Any]] = {}
            for e in eps:
                if e["reward_valid"] == 1 and finite(e["reward"]):
                    key = (str(e["task_id"]), str(e["task_checksum"] or ""))
                    valid_map[key] = e
            if valid_map:
                batch_eps[b["batch_id"]] = valid_map

        active_batches = [b for b in batches if b["batch_id"] in batch_eps]
        if not active_batches:
            common_keys: set[tuple[str, str]] = set()
        else:
            common_keys = set.intersection(*(set(batch_eps[b["batch_id"]].keys()) for b in active_batches))

        def _ep_x_val(ep: dict[str, Any], axis: str) -> float | None:
            if axis in ("duration_p50_sec", "duration_avg_sec"):
                return float(ep["duration_sec"]) if finite(ep.get("duration_sec")) else None
            if axis == "total_tokens_avg":
                if isinstance(ep.get("input_tokens"), int) and isinstance(ep.get("output_tokens"), int):
                    return float(ep["input_tokens"] + ep["output_tokens"])
                return None
            if axis == "tool_calls_avg":
                return float(ep["tool_call_count"]) if isinstance(ep.get("tool_call_count"), int) else None
            if axis == "recorded_cost_usd":
                return float(ep["cost_usd"]) if finite(ep.get("cost_usd")) else None
            return None

        y_common_keys = {
            k for k in common_keys if all(finite(batch_eps[b["batch_id"]][k].get("duration_sec")) for b in active_batches)
        }
        if not y_common_keys:
            y_common_keys = common_keys

        if task_set_policy == "recompute_xy_common":
            with_x = [b for b in active_batches if any(_ep_x_val(batch_eps[b["batch_id"]][k], x_axis) is not None for k in y_common_keys)]
            if with_x:
                eval_keys = {k for k in y_common_keys if all(_ep_x_val(batch_eps[b["batch_id"]][k], x_axis) is not None for b in with_x)}
            else:
                eval_keys = set()
        else:
            eval_keys = y_common_keys

        n_common = len(eval_keys)

        x_axis_coverage: dict[str, Any] = {}
        for ax in ("duration_p50_sec", "total_tokens_avg", "tool_calls_avg", "recorded_cost_usd"):
            any_rec = 0
            full_cov = 0
            partial_notes: list[str] = []
            for b in active_batches:
                vals = [_ep_x_val(batch_eps[b["batch_id"]][k], ax) for k in y_common_keys]
                non_null = sum(1 for v in vals if v is not None)
                if non_null > 0:
                    any_rec += 1
                    if non_null == len(y_common_keys) and len(y_common_keys) > 0:
                        full_cov += 1
                    else:
                        partial_notes.append(f"{non_null}/{len(y_common_keys)}")
            cov_entry: dict[str, Any] = {
                "total_batches": len(active_batches),
                "batches_with_any_records": any_rec,
                "plottable_full_coverage_points": full_cov,
            }
            if ax == "recorded_cost_usd" and partial_notes:
                cov_entry["partial_record_coverage"] = f"{len(partial_notes)} 组仅 {', '.join(partial_notes)} 题有原生已记录成本"
                cov_entry["note"] = f"{full_cov} 组覆盖全部 {len(y_common_keys)} 道共同题；已记录成本未做账单对账"
            x_axis_coverage[ax] = cov_entry

        points: list[dict[str, Any]] = []
        for b in active_batches:
            if n_common == 0:
                continue
            eps_sub = [batch_eps[b["batch_id"]][k] for k in eval_keys]
            scores_100 = [normalize_score_to_100(float(e["reward"]), rel["scale_type"]) for e in eps_sub]
            score_pct = round(math.fsum(scores_100) / n_common, 2)
            x_vals = [_ep_x_val(e, x_axis) for e in eps_sub]
            valid_x = [v for v in x_vals if v is not None]
            full_x_cov = len(valid_x) == n_common and n_common > 0
            if valid_x:
                if x_axis == "duration_p50_sec":
                    x_val = round(float(statistics.median(valid_x)), 2)
                else:
                    x_val = round(math.fsum(valid_x) / len(valid_x), 4)
            else:
                x_val = None

            points.append(
                {
                    "batch_id": b["batch_id"],
                    "batch_name": b["batch_name"],
                    "model_canonical": b["model_canonical"],
                    "harness": b["harness"],
                    "series_id": None,
                    "series_order": None,
                    "score_pct": score_pct,
                    "x_value": x_val if full_x_cov else None,
                    "partial_x_value": x_val if (not full_x_cov and x_val is not None) else None,
                    "x_coverage_tasks": len(valid_x),
                    "common_valid_n": n_common,
                    "full_coverage": f"{b['scored']}/{b['planned'] or b['recorded']}",
                    "eligible_for_pareto": full_x_cov,
                    "on_pareto": False,
                }
            )

        eligible = [p for p in points if p["eligible_for_pareto"] and p["x_value"] is not None]
        eligible.sort(key=lambda p: (p["x_value"], -p["score_pct"]))
        frontier_ids: list[str] = []
        best_y = -1e18
        for p in eligible:
            if p["score_pct"] > best_y:
                p["on_pareto"] = True
                frontier_ids.append(p["batch_id"])
                best_y = p["score_pct"]

        # Identify verified ordered family series ONLY (same model_canonical + harness across ordered checkpoints or thinking modes;
        # NEVER draw dashed connectors between unrelated points — v2.2.1 02-1 §3.1 & A-15!)
        _thinking_order = {"nonthinking": 0, "low": 1, "medium": 2, "med": 2, "high": 3, "thinking": 4, "think": 4}
        by_batch_id = {b["batch_id"]: b for b in active_batches}
        series_groups: dict[str, list[tuple[tuple[int, int, str], dict[str, Any]]]] = defaultdict(list)
        for p in points:
            b = by_batch_id.get(p["batch_id"])
            if not b or not p["eligible_for_pareto"] or p["x_value"] is None:
                continue
            mc = (b.get("model_canonical") or "").strip()
            har = (b.get("harness") or b.get("runner") or "").strip()
            ck = (b.get("checkpoint") or "").strip()
            th = (b.get("thinking_mode") or "").strip().lower()
            if not mc or not har:
                continue
            if ck:
                ck_num = int(re.sub(r"[^0-9]", "", ck) or "0") if ck != "base" else -1
                grp_key = f"{mc}|{har}|ck|{th or 'default'}"
                series_groups[grp_key].append(((0 if ck == "base" else 1, ck_num, ck), p))
            elif th in _thinking_order:
                grp_key = f"{mc}|{har}|thinking"
                series_groups[grp_key].append(((0, _thinking_order[th], th), p))

        series_connectors: list[dict[str, Any]] = []
        for grp_key, members in sorted(series_groups.items()):
            distinct_keys = {m[0] for m in members}
            if len(distinct_keys) < 2:
                continue
            members.sort(key=lambda m: m[0])
            ordered_ids = []
            for ord_idx, (_, pt) in enumerate(members):
                pt["series_id"] = grp_key
                pt["series_order"] = ord_idx
                ordered_ids.append(pt["batch_id"])
            series_connectors.append(
                {
                    "series_id": grp_key,
                    "label": grp_key.replace("|", " · "),
                    "batch_ids": ordered_ids,
                }
            )

        return {
            "methodology_version": METHODOLOGY_VERSION,
            "benchmark_slug": rel["benchmark_slug"],
            "dataset_release_id": dataset_release_id,
            "raw_dataset_name": rel["bm_display_name"],
            "scoring_protocol": scoring_protocol,
            "task_set_policy": task_set_policy,
            "common_valid_tasks": n_common,
            "x_axis": x_axis,
            "x_axis_coverage": x_axis_coverage,
            "points": points,
            "series_connectors": series_connectors,
            "pareto_frontier_batch_ids": frontier_ids,
        }

    # --- 5. Legacy Byte-Offset Sample Reader (< 10ms O(1) seek!) ---

    def list_legacy_dirs(self, user: AuthUser) -> dict[str, Any]:
        """List ONLY legacy benchmark directories authorized for `user` under `source_approval_registry.yaml` (S-02).
        Filters both directory visibility and per-file indexed counts by (project_id, owner_id) + holdout scope."""
        root = self.settings.legacy_root_dir
        dirs: list[dict[str, Any]] = []
        if root.is_dir():
            root_resolved = root.resolve()
            indexed_rows = self.db.fetchall("SELECT file_path, COUNT(*) AS cnt FROM legacy_sample_offsets GROUP BY file_path")
            cnt_by_dir_path: dict[str, int] = {}
            for r in indexed_rows:
                try:
                    fp = Path(r["file_path"]).resolve()
                    fp.relative_to(root_resolved)
                except (OSError, ValueError):
                    continue
                f_proj, f_owner, _, _ = self.approval_registry.lookup_full(fp)
                if not can_access_project(user, f_proj, owner_id=f_owner):
                    continue
                parent_key = str(fp.parent)
                cnt_by_dir_path[parent_key] = cnt_by_dir_path.get(parent_key, 0) + int(r["cnt"] or 0)

            for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                if not entry.is_dir():
                    continue
                try:
                    validate_safe_id(entry.name, "benchmark_dir")
                    legacy_dir = resolve_within_root(root, entry.name)
                except AppError:
                    continue
                if not legacy_dir.is_dir() or legacy_dir.parent != root_resolved:
                    continue
                if is_holdout_dataset_or_batch(benchmark_dir=entry.name) and not has_scope(user, "dataset:holdout_read"):
                    continue
                proj_id, owner_id, _, _ = self.approval_registry.lookup_full(legacy_dir)
                if not can_access_project(user, proj_id, owner_id=owner_id):
                    continue

                has_authorized_samples = False
                for cand in legacy_dir.iterdir():
                    if not cand.is_file() or cand.suffix not in (".json", ".jsonl"):
                        continue
                    try:
                        c_res = resolve_within_root(legacy_dir, cand.name)
                    except AppError:
                        continue
                    if c_res.parent != legacy_dir:
                        continue
                    fp_proj, fp_owner, _, _ = self.approval_registry.lookup_full(c_res)
                    if can_access_project(user, fp_proj, owner_id=fp_owner):
                        has_authorized_samples = True
                        break

                idx_cnt = cnt_by_dir_path.get(str(legacy_dir), 0)
                if has_authorized_samples or idx_cnt > 0:
                    dirs.append({"name": entry.name, "indexed_samples": idx_cnt})
        return {"items": dirs, "total": len(dirs), "methodology_version": METHODOLOGY_VERSION}

    def list_legacy_samples(
        self,
        user: AuthUser,
        benchmark_dir: str,
        *,
        model_file: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        validate_safe_id(benchmark_dir, "benchmark_dir")
        if is_holdout_dataset_or_batch(benchmark_dir=benchmark_dir):
            require_scope(user, "dataset:holdout_read")
        legacy_dir = resolve_within_root(self.settings.legacy_root_dir, benchmark_dir)
        if legacy_dir.parent != self.settings.legacy_root_dir.resolve():
            raise AppError(400, "INVALID_PATH_OR_ID", "非法历史目录路径")
        proj_id, owner_id, reg_status, reg_classes = self.approval_registry.lookup_full(legacy_dir)
        require_project_access(user, proj_id, owner_id=owner_id)
        can_media_refs = (
            reg_status != "pending"
            and "direct_vlm_qa" in reg_classes
            and has_scope(user, "direct_vlm_qa:read")
        )
        prefix = str(legacy_dir.resolve())
        if model_file:
            validate_safe_id(model_file, "model_file")
            target_file = resolve_within_root(legacy_dir, model_file)
            if target_file.parent != legacy_dir.resolve():
                raise AppError(400, "INVALID_PATH_OR_ID", "非法模型文件路径")
            f_proj, f_owner, f_status, f_classes = self.approval_registry.lookup_full(target_file)
            require_project_access(user, f_proj, owner_id=f_owner)
            can_media_refs = (
                f_status != "pending"
                and "direct_vlm_qa" in f_classes
                and has_scope(user, "direct_vlm_qa:read")
            )
            allowed_files = [str(target_file)]
        else:
            raw_files = self.db.fetchall(
                "SELECT DISTINCT file_path FROM legacy_sample_offsets WHERE file_path LIKE ?",
                (f"{prefix}/%",),
            )
            allowed_files = []
            for rf in raw_files:
                fp_str = str(rf["file_path"])
                try:
                    fp = Path(fp_str).resolve()
                except OSError:
                    continue
                if fp.parent != legacy_dir.resolve():
                    continue
                f_proj, f_owner, _, _ = self.approval_registry.lookup_full(fp)
                if can_access_project(user, f_proj, owner_id=f_owner):
                    allowed_files.append(fp_str)

        if not allowed_files:
            return {
                "benchmark_dir": benchmark_dir,
                "items": [],
                "total": 0,
                "page": page,
                "page_size": page_size,
                "methodology_version": METHODOLOGY_VERSION,
            }
        placeholders = ",".join("?" for _ in allowed_files)
        where_sql = f"file_path IN ({placeholders})"
        params: list[Any] = list(allowed_files)
        total = self.db.fetchone(f"SELECT COUNT(*) AS c FROM legacy_sample_offsets WHERE {where_sql}", params)["c"]  # type: ignore[index]
        offset = max(0, (page - 1) * page_size)
        rows = self.db.fetchall(
            f"""
            SELECT file_path, sample_id, status, reward, category, difficulty,
                   media_match_status, media_refs_json, meta_json
            FROM legacy_sample_offsets
            WHERE {where_sql}
            ORDER BY file_path ASC, sample_id ASC
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, offset],
        )
        def _normalize_media_refs(match_status: str, raw_refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
            out_refs: list[dict[str, Any]] = []
            for idx, m in enumerate(raw_refs):
                if not isinstance(m, dict):
                    continue
                item = dict(m)
                kind = item.get("kind")
                if match_status == "unique_hit" and kind in ("file", "tsv_inline"):
                    item["is_image"] = True
                    if kind == "tsv_inline" and not item.get("url") and item.get("source") and item.get("tsv_key"):
                        tsv_idx = int(item.get("tsv_idx", idx))
                        item["url"] = f"/api/v1/media/{item['source']}/tsv-inline/{item['tsv_key']}?idx={tsv_idx}"
                    elif kind == "file" and not item.get("url") and item.get("source") and item.get("rel_path"):
                        item["url"] = f"/api/v1/media/{item['source']}/{item['rel_path']}"
                else:
                    item["is_image"] = False
                out_refs.append(item)
            return out_refs

        items = []
        for r in rows:
            meta = json.loads(r.pop("meta_json") or "{}")
            refs = _normalize_media_refs(str(r["media_match_status"] or ""), json.loads(r.pop("media_refs_json") or "[]"))
            fpath = Path(r.pop("file_path"))
            _, _, f_status, f_classes = self.approval_registry.lookup_full(fpath)
            row_can_media = (
                can_media_refs
                and f_status != "pending"
                and "direct_vlm_qa" in f_classes
            )
            items.append(
                {
                    "model_file": fpath.name,
                    "sample_id": r["sample_id"],
                    "status": r["status"],
                    "reward": r["reward"],
                    "category": r["category"],
                    "difficulty": r["difficulty"],
                    "media_match_status": r["media_match_status"],
                    "media_refs": refs if row_can_media else [],
                    "meta": meta,
                }
            )
        return {
            "benchmark_dir": benchmark_dir,
            "items": items,
            "total": int(total),
            "page": page,
            "page_size": page_size,
            "methodology_version": METHODOLOGY_VERSION,
        }

    def get_legacy_sample_detail(
        self,
        user: AuthUser,
        benchmark_dir: str,
        model_file: str,
        sample_id: str,
    ) -> dict[str, Any]:
        validate_safe_id(benchmark_dir, "benchmark_dir")
        validate_safe_id(model_file, "model_file")
        validate_safe_task_id(sample_id)
        if is_holdout_dataset_or_batch(benchmark_dir=benchmark_dir):
            require_scope(user, "dataset:holdout_read")
        legacy_dir = resolve_within_root(self.settings.legacy_root_dir, benchmark_dir)
        if legacy_dir.parent != self.settings.legacy_root_dir.resolve():
            raise AppError(400, "INVALID_PATH_OR_ID", "非法历史目录路径")
        proj_id, owner_id, reg_status, reg_classes = self.approval_registry.lookup_full(legacy_dir)
        require_project_access(user, proj_id, owner_id=owner_id)
        require_scope(user, "direct_vlm_qa:read")

        target_file = resolve_within_root(legacy_dir, model_file)
        if target_file.parent != legacy_dir.resolve():
            raise AppError(400, "INVALID_PATH_OR_ID", "模型文件路径超出授权目录")
        f_proj, f_owner, f_status, f_classes = self.approval_registry.lookup_full(target_file)
        require_project_access(user, f_proj, owner_id=f_owner)
        if reg_status == "pending" or "direct_vlm_qa" not in reg_classes or f_status == "pending" or "direct_vlm_qa" not in f_classes:
            raise AppError(403, "SCOPE_FORBIDDEN", "来源未批准 direct_vlm_qa 证据类")

        row = self.db.fetchone(
            """
            SELECT start_offset, end_offset, status, reward, category, difficulty,
                   media_match_status, media_refs_json, meta_json
            FROM legacy_sample_offsets
            WHERE file_path = ? AND sample_id = ?
            """,
            (str(target_file), sample_id),
        )
        if row is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", "历史样本偏移记录不存在")
        raw_obj = read_legacy_sample_by_offset(target_file, int(row["start_offset"]), int(row["end_offset"]))
        strip_ans = not has_scope(user, "dataset:answer_read")
        cleaned = redact_json_recursive(raw_obj, strip_answers=strip_ans)
        raw_refs = json.loads(row["media_refs_json"] or "[]")
        norm_refs: list[dict[str, Any]] = []
        for idx, m in enumerate(raw_refs):
            if not isinstance(m, dict):
                continue
            item = dict(m)
            kind = item.get("kind")
            if row["media_match_status"] == "unique_hit" and kind in ("file", "tsv_inline"):
                item["is_image"] = True
                if kind == "tsv_inline" and not item.get("url") and item.get("source") and item.get("tsv_key"):
                    tsv_idx = int(item.get("tsv_idx", idx))
                    item["url"] = f"/api/v1/media/{item['source']}/tsv-inline/{item['tsv_key']}?idx={tsv_idx}"
                elif kind == "file" and not item.get("url") and item.get("source") and item.get("rel_path"):
                    item["url"] = f"/api/v1/media/{item['source']}/{item['rel_path']}"
            else:
                item["is_image"] = False
            norm_refs.append(item)
        return {
            "benchmark_dir": benchmark_dir,
            "model_file": model_file,
            "sample_id": sample_id,
            "status": row["status"],
            "reward": row["reward"],
            "media_match_status": row["media_match_status"],
            "media_refs": norm_refs,
            "sample": cleaned,
            "methodology_version": METHODOLOGY_VERSION,
        }

    # --- 6. Datasets Tree & Tasks ---

    def get_datasets_tree(self, user: AuthUser) -> dict[str, Any]:
        cfg = load_taxonomy_config(self.settings.taxonomy_file)
        tracks_meta = cfg.get("tracks", [])
        bms = self.db.fetchall("SELECT * FROM benchmarks ORDER BY track_slug, display_name")
        rels = self.db.fetchall("SELECT * FROM dataset_releases ORDER BY dataset_release_id")
        proj_sql, params = _project_filter_sql(user, "project_id")
        b_counts = self.db.fetchall(
            f"""
            SELECT dataset_release_id, COUNT(*) AS cnt
            FROM batches
            WHERE {proj_sql} AND dataset_release_id IS NOT NULL
            GROUP BY dataset_release_id
            """,
            params,
        )
        cnt_map = {r["dataset_release_id"]: int(r["cnt"]) for r in b_counts}

        rels_by_bm: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rels:
            r_copy = dict(r)
            r_copy["batch_count"] = cnt_map.get(r["dataset_release_id"], 0)
            rels_by_bm[r["benchmark_slug"]].append(r_copy)

        bms_by_track: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for bm in bms:
            bm_copy = dict(bm)
            bm_copy["metadata"] = json.loads(bm_copy.pop("metadata_json") or "{}")
            bm_copy["releases"] = rels_by_bm.get(bm["benchmark_slug"], [])
            bms_by_track[bm["track_slug"]].append(bm_copy)

        tree_tracks = []
        seen_tracks = set()
        for t in tracks_meta:
            ts = t["track_slug"]
            seen_tracks.add(ts)
            tree_tracks.append(
                {
                    "track_slug": ts,
                    "display_name": t["display_name"],
                    "zh_name": t.get("zh_name", t["display_name"]),
                    "goal_type": t["goal_type"],
                    "default_visible": bool(t.get("default_visible", True)),
                    "benchmarks": bms_by_track.get(ts, []),
                }
            )
        for ts, b_list in bms_by_track.items():
            if ts not in seen_tracks:
                tree_tracks.append(
                    {
                        "track_slug": ts,
                        "display_name": b_list[0]["track_display_name"],
                        "goal_type": b_list[0]["goal_type"],
                        "default_visible": True,
                        "benchmarks": b_list,
                    }
                )
        return {"methodology_version": METHODOLOGY_VERSION, "tracks": tree_tracks}

    def list_dataset_tasks(
        self,
        user: AuthUser,
        dataset_release_id: str,
        *,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        validate_safe_id(dataset_release_id, "dataset_release_id")
        rel = self.db.fetchone("SELECT * FROM dataset_releases WHERE dataset_release_id = ?", (dataset_release_id,))
        if rel is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", "数据集版本不存在")
        is_holdout_rel = is_holdout_dataset_or_batch(
            dataset_release_id=dataset_release_id,
            release_split=rel.get("split"),
            benchmark_slug=rel.get("benchmark_slug"),
        )
        can_read_holdout = has_scope(user, "dataset:holdout_read")
        total_all = int(
            self.db.fetchone(
                "SELECT COUNT(*) AS c FROM task_definitions WHERE dataset_release_id = ?", (dataset_release_id,)
            )["c"]  # type: ignore[index]
        )
        if is_holdout_rel and not can_read_holdout:
            return {
                "dataset_release_id": dataset_release_id,
                "redacted": True,
                "hidden_holdout_count": total_all,
                "total_including_holdout": total_all,
                "redacted_reason": "缺少 dataset:holdout_read 权限，仅展示聚合题数",
                "items": [],
                "total": total_all,
                "page": page,
                "page_size": page_size,
                "methodology_version": METHODOLOGY_VERSION,
            }

        where_sql = "dataset_release_id = ?"
        if not can_read_holdout:
            where_sql += " AND LOWER(COALESCE(split, '')) != 'holdout'"
        total_visible = int(
            self.db.fetchone(
                f"SELECT COUNT(*) AS c FROM task_definitions WHERE {where_sql}", (dataset_release_id,)
            )["c"]  # type: ignore[index]
        )
        hidden_holdout_count = max(0, total_all - total_visible)
        offset = max(0, (page - 1) * page_size)
        rows = self.db.fetchall(
            f"""
            SELECT dataset_release_id, task_id, task_dir, task_checksum, split, source,
                   environment, difficulty, category, language, tags_json, resources_json
            FROM task_definitions
            WHERE {where_sql}
            ORDER BY task_id ASC
            LIMIT ? OFFSET ?
            """,
            (dataset_release_id, page_size, offset),
        )
        for r in rows:
            r["tags"] = json.loads(r.pop("tags_json") or "[]")
            r["resources"] = json.loads(r.pop("resources_json") or "{}")
        return {
            "dataset_release_id": dataset_release_id,
            "redacted": hidden_holdout_count > 0,
            "hidden_holdout_count": hidden_holdout_count,
            "total_including_holdout": total_all,
            "redacted_reason": (
                f"缺少 dataset:holdout_read 权限，已隐藏 {hidden_holdout_count} 道留出集题目"
                if hidden_holdout_count > 0
                else None
            ),
            "items": rows,
            "total": total_visible,
            "page": page,
            "page_size": page_size,
            "methodology_version": METHODOLOGY_VERSION,
        }

    def _get_dataset_task_row(
        self, user: AuthUser, dataset_release_id: str, task_id: str
    ) -> tuple[dict[str, Any], bool]:
        validate_safe_id(dataset_release_id, "dataset_release_id")
        validate_safe_task_id(task_id)
        rel = self.db.fetchone("SELECT * FROM dataset_releases WHERE dataset_release_id = ?", (dataset_release_id,))
        if rel is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", "数据集版本不存在")
        is_rel_holdout = is_holdout_dataset_or_batch(
            dataset_release_id=dataset_release_id,
            release_split=rel.get("split"),
            benchmark_slug=rel.get("benchmark_slug"),
        )
        if is_rel_holdout:
            require_scope(user, "dataset:holdout_read")
        row = self.db.fetchone(
            "SELECT * FROM task_definitions WHERE dataset_release_id = ? AND task_id = ?",
            (dataset_release_id, task_id),
        )
        if row is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", "题目不存在")
        is_task_holdout = is_rel_holdout or str(row.get("split") or "").strip().lower() == "holdout"
        if is_task_holdout:
            require_scope(user, "dataset:holdout_read")
        return row, is_task_holdout

    def _get_dataset_bundle_content(
        self, user: AuthUser, dataset_release_id: str, task_id: str, *, is_holdout: bool
    ) -> tuple[dict[str, Any] | None, frozenset[str]]:
        row = self.db.fetchone(
            """
            SELECT archive_path, instruction, attachments_json FROM dataset_bundle_task_content
            WHERE dataset_release_id = ? AND task_id = ?
            """,
            (dataset_release_id, task_id),
        )
        if row is None:
            return None, frozenset()
        source = Path(row["archive_path"])
        proj_id, owner_id, approval, classes = self.approval_registry.lookup_full(source)
        if is_holdout or approval != "approved_public":
            require_project_access(user, proj_id, owner_id=owner_id)
        return row, classes if approval != "pending" else frozenset()

    def get_dataset_task_detail(self, user: AuthUser, dataset_release_id: str, task_id: str) -> dict[str, Any]:
        row, is_task_holdout = self._get_dataset_task_row(user, dataset_release_id, task_id)
        needed_class = "holdout_task_body" if is_task_holdout else "public_task_metadata"
        instruction_text: str | None = None
        instruction_status = "unavailable"
        attachments: list[str] = []
        if row.get("instruction_ref"):
            p = Path(row["instruction_ref"])
            proj_id, owner_id, reg_status, reg_classes = self.approval_registry.lookup_full(p)
            require_project_access(user, proj_id, owner_id=owner_id)
            if reg_status == "pending" or needed_class not in reg_classes:
                instruction_status = "restricted"
            else:
                try:
                    instruction_text = redact_text(p.read_text(encoding="utf-8", errors="replace")).strip()
                except OSError:
                    pass
        else:
            bundle, classes = self._get_dataset_bundle_content(
                user, dataset_release_id, task_id, is_holdout=is_task_holdout
            )
            if bundle:
                if needed_class in classes:
                    instruction_text = bundle["instruction"]
                    if not is_task_holdout and "public_task_attachments" in classes:
                        attachments = sorted(json.loads(bundle["attachments_json"]))
                else:
                    instruction_status = "restricted"
        return {
            "dataset_release_id": dataset_release_id,
            "task_id": task_id,
            "task_dir": row["task_dir"],
            "task_checksum": row["task_checksum"],
            "split": row["split"],
            "source": row["source"],
            "difficulty": row["difficulty"],
            "category": row["category"],
            "language": row["language"],
            "tags": json.loads(row["tags_json"] or "[]"),
            "resources": json.loads(row["resources_json"] or "{}"),
            "instruction": instruction_text,
            "instruction_status": "ready" if instruction_text else instruction_status,
            "attachments": attachments,
            "has_answer_access": has_scope(user, "dataset:answer_read"),
            "methodology_version": METHODOLOGY_VERSION,
        }

    def get_dataset_task_attachment(
        self, user: AuthUser, dataset_release_id: str, task_id: str, attachment_name: str
    ) -> bytes:
        validate_safe_id(attachment_name, "attachment_name")
        _, is_task_holdout = self._get_dataset_task_row(user, dataset_release_id, task_id)
        if is_task_holdout:
            raise AppError(403, "SCOPE_FORBIDDEN", "题目附件不可读取")
        bundle, classes = self._get_dataset_bundle_content(user, dataset_release_id, task_id, is_holdout=False)
        if bundle is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", "题目附件不存在")
        if not {"public_task_metadata", "public_task_attachments"} <= classes:
            raise AppError(403, "SCOPE_FORBIDDEN", "题目附件不可读取")
        attachment = json.loads(bundle["attachments_json"]).get(attachment_name)
        if attachment is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", "题目附件不存在")
        try:
            with tarfile.open(bundle["archive_path"], "r:") as archive:
                member = archive.getmember(attachment["member"])
                if not member.isfile() or member.size > MAX_TASK_ATTACHMENT_BYTES:
                    raise AppError(404, "RESOURCE_NOT_FOUND", "题目附件不可读取")
                with archive.extractfile(member) as stream:
                    data = stream.read()
        except (OSError, tarfile.TarError, KeyError):
            raise AppError(404, "RESOURCE_NOT_FOUND", "题目附件不可读取") from None
        if _sha256_bytes(data) != attachment["sha256"]:
            raise AppError(409, "INTEGRITY_MISMATCH", "题目附件已更新，等待索引刷新")
        return data
