from __future__ import annotations

import json
import re
import shlex
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.api.errors import AppError
from app.config import Settings
from app.db.database import Database
from app.security.auth import AuthUser
from app.security.authz import has_scope, sql_project_filter
from app.security.redaction import redact_path, redact_text
from app.security.storage_probe import validate_safe_id

CKPT_DIR_RE = re.compile(r"^checkpoint-(\d+)$")
SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class TrainingService:
    """M3 Training & Multi-Expert OPD Monitor (v2.2.1 · 02-7 §3, 04-2 §3, E-02).

    Strict invariants:
    1. Dynamically scans `settings.training_runs_dir` so newly published runs appear with ZERO restart.
    2. When `training-runs/` is empty (real 197 production state), returns an honest empty state +
       prerequisites + `train.py --backend opd --plan` CLI generator. NEVER injects fixture data
       into production DB or production API responses!
    3. Parses real `framework/training_opd.py` output schema:
       - `routing.json`: list of `{dataset, task, environment, source, expert, model}` (or `{"tasks": [...]}`)
       - `checkpoint-0000/opd.json`: `{"student": "<student_model>"}`
       - `checkpoint-XXXX/opd.json`: `{"step": int, "rollout_checkpoint": str, "rollouts": [{dataset, task, expert, reward, tokens, sampled_reverse_kl}]}`
    4. Before/After holdout comparison is strictly guarded: ONLY linked when BOTH base and checkpoint
       batches have explicit `checkpoint` identity, SAME `-holdout` `dataset_release_id`, SAME `scoring_protocol`,
       authorized `project_id`, and caller holds `dataset:holdout_read`!
    """

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db

    def _list_registered_train_datasets(self) -> list[dict[str, Any]]:
        managed_toml = self.settings.managed_framework_dir / "harbor.toml"
        if self.settings.production_mode and managed_toml.is_file():
            toml_path = managed_toml
        elif (self.settings.framework_dir / "harbor.toml").is_file():
            toml_path = self.settings.framework_dir / "harbor.toml"
        else:
            toml_path = managed_toml
        out: list[dict[str, Any]] = []
        if not toml_path.is_file():
            return out
        try:
            cfg = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            for alias, meta in (cfg.get("datasets") or {}).items():
                if not isinstance(meta, dict):
                    continue
                if meta.get("runner", "harbor") != "harbor":
                    continue
                # training_opd.py:97 forbids -holdout datasets for OPD training
                if str(alias).endswith("-holdout") or str(meta.get("split", "train")) != "train":
                    continue
                out.append(
                    {
                        "dataset_alias": str(alias),
                        "display_name": str(meta.get("name") or alias),
                        "split": "train",
                    }
                )
        except Exception:
            pass
        return out

    def _parse_routing_file(self, routing_path: Path, *, allow_holdout: bool = True) -> list[dict[str, Any]]:
        if routing_path.is_symlink() or not routing_path.is_file():
            return []
        try:
            raw = json.loads(routing_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if isinstance(raw, dict) and isinstance(raw.get("tasks"), list):
            rows = raw["tasks"]
        elif isinstance(raw, list):
            rows = raw
        else:
            return []
        cleaned: list[dict[str, Any]] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            ds_s = redact_text(str(r.get("dataset") or "unknown")) or "unknown"
            tk_s = redact_text(str(r.get("task") or "unknown")) or "unknown"
            if not allow_holdout and ("holdout" in ds_s.lower() or "holdout" in tk_s.lower()):
                continue
            cleaned.append(
                {
                    "dataset": ds_s,
                    "task": tk_s,
                    "environment": redact_text(str(r.get("environment") or "default")) or "default",
                    "source": redact_text(str(r.get("source") or "")) or "",
                    "expert": redact_text(str(r.get("expert") or "default")) or "default",
                    "model": redact_text(str(r.get("model") or "unknown")) or "unknown",
                    "priority": r.get("priority"),
                    "matched_selectors": r.get("matched_selectors"),
                }
            )
        return cleaned

    def _build_routing_topology(self, routing_rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Build Sankey nodes/links: Dataset -> Environment/Source -> Expert -> Expert Model."""
        nodes_seen: dict[str, dict[str, str]] = {}
        link_counts: dict[tuple[str, str], int] = defaultdict(int)
        experts_meta: dict[str, dict[str, Any]] = {}

        def _add_node(nid: str, label: str, stage: str) -> None:
            if nid not in nodes_seen:
                nodes_seen[nid] = {"name": nid, "label": label, "stage": stage}

        for r in routing_rows:
            ds_id = f"ds:{r['dataset']}"
            env_label = f"{r['environment']}" + (f" / {r['source']}" if r["source"] else "")
            env_id = f"env:{env_label}"
            exp_id = f"exp:{r['expert']}"
            mod_id = f"model:{r['model']}"

            _add_node(ds_id, r["dataset"], "dataset")
            _add_node(env_id, env_label, "environment_source")
            _add_node(exp_id, f"Expert: {r['expert']}", "expert")
            _add_node(mod_id, r["model"], "expert_model")

            link_counts[(ds_id, env_id)] += 1
            link_counts[(env_id, exp_id)] += 1
            link_counts[(exp_id, mod_id)] += 1

            em = experts_meta.setdefault(
                r["expert"],
                {
                    "expert": r["expert"],
                    "model": r["model"],
                    "task_count": 0,
                    "datasets": set(),
                    "environments": set(),
                    "sources": set(),
                },
            )
            em["task_count"] += 1
            em["datasets"].add(r["dataset"])
            em["environments"].add(r["environment"])
            if r["source"]:
                em["sources"].add(r["source"])

        experts_summary = [
            {
                "expert": k,
                "model": v["model"],
                "task_count": v["task_count"],
                "datasets": sorted(v["datasets"]),
                "environments": sorted(v["environments"]),
                "sources": sorted(v["sources"]),
            }
            for k, v in sorted(experts_meta.items())
        ]
        return {
            "total_routed_tasks": len(routing_rows),
            "nodes": list(nodes_seen.values()),
            "links": [{"source": s, "target": t, "value": v} for (s, t), v in sorted(link_counts.items())],
            "experts": experts_summary,
        }

    def _parse_run_dir(self, run_dir: Path, *, allow_holdout: bool = True) -> dict[str, Any]:
        run_id = run_dir.name
        routing_rows = self._parse_routing_file(run_dir / "routing.json", allow_holdout=allow_holdout)
        topology = self._build_routing_topology(routing_rows)

        ckpts: list[tuple[int, Path]] = []
        for child in run_dir.iterdir():
            if child.is_symlink() or not child.is_dir():
                continue
            m = CKPT_DIR_RE.fullmatch(child.name)
            opd_f = child / "opd.json"
            if m and not opd_f.is_symlink() and opd_f.is_file():
                ckpts.append((int(m.group(1)), child))
        ckpts.sort(key=lambda x: x[0])

        student_model: str | None = None
        step_series: list[dict[str, Any]] = []
        expert_series: dict[str, list[dict[str, Any]]] = defaultdict(list)
        checkpoint_names: list[str] = []

        for step_num, cdir in ckpts:
            checkpoint_names.append(cdir.name)
            try:
                doc = json.loads((cdir / "opd.json").read_text(encoding="utf-8"))
            except Exception:
                continue
            if step_num == 0 and isinstance(doc.get("student"), str):
                student_model = redact_text(doc["student"])
                continue

            rollouts = doc.get("rollouts") if isinstance(doc.get("rollouts"), list) else []
            step_val = int(doc.get("step", step_num))
            rollout_ckpt = str(doc.get("rollout_checkpoint") or f"checkpoint-{max(0, step_val - 1):04d}")

            kls: list[float] = []
            rewards: list[float] = []
            total_tokens = 0
            by_exp: dict[str, dict[str, Any]] = defaultdict(lambda: {"kls": [], "rewards": [], "tokens": 0, "count": 0})
            clean_rollouts: list[dict[str, Any]] = []

            for ro in rollouts:
                if not isinstance(ro, dict):
                    continue
                ro_ds = redact_text(str(ro.get("dataset") or "")) or ""
                ro_tk = redact_text(str(ro.get("task") or "")) or ""
                if not allow_holdout and ("holdout" in ro_ds.lower() or "holdout" in ro_tk.lower()):
                    continue
                exp = redact_text(str(ro.get("expert") or "default")) or "default"
                kl = float(ro["sampled_reverse_kl"]) if isinstance(ro.get("sampled_reverse_kl"), (int, float)) else None
                rw = float(ro["reward"]) if isinstance(ro.get("reward"), (int, float)) else None
                toks = int(ro.get("tokens") or 0)
                total_tokens += toks
                if kl is not None:
                    kls.append(kl)
                    by_exp[exp]["kls"].append(kl)
                if rw is not None:
                    rewards.append(rw)
                    by_exp[exp]["rewards"].append(rw)
                by_exp[exp]["tokens"] += toks
                by_exp[exp]["count"] += 1
                clean_rollouts.append(
                    {
                        "dataset": ro_ds,
                        "task": ro_tk,
                        "expert": exp,
                        "reward": rw,
                        "tokens": toks,
                        "sampled_reverse_kl": kl,
                    }
                )

            exp_breakdown: dict[str, Any] = {}
            for exp, agg in sorted(by_exp.items()):
                mean_kl = round(sum(agg["kls"]) / len(agg["kls"]), 6) if agg["kls"] else None
                mean_rw = round(sum(agg["rewards"]) / len(agg["rewards"]), 6) if agg["rewards"] else None
                exp_breakdown[exp] = {
                    "mean_reverse_kl": mean_kl,
                    "mean_reward": mean_rw,
                    "tokens": agg["tokens"],
                    "rollouts": agg["count"],
                }
                expert_series[exp].append(
                    {
                        "step": step_val,
                        "checkpoint": cdir.name,
                        "mean_reverse_kl": mean_kl,
                        "mean_reward": mean_rw,
                        "tokens": agg["tokens"],
                    }
                )

            step_series.append(
                {
                    "step": step_val,
                    "checkpoint": cdir.name,
                    "rollout_checkpoint": rollout_ckpt,
                    "mean_reverse_kl": round(sum(kls) / len(kls), 6) if kls else None,
                    "mean_reward": round(sum(rewards) / len(rewards), 6) if rewards else None,
                    "total_tokens": total_tokens,
                    "rollout_count": len(clean_rollouts),
                    "by_expert": exp_breakdown,
                    "rollouts": clean_rollouts,
                }
            )

        latest = step_series[-1] if step_series else None
        return {
            "run_id": run_id,
            "source_path_redacted": redact_path(str(run_dir)),
            "student_model": student_model,
            "checkpoint_count": len(ckpts),
            "checkpoints": checkpoint_names,
            "steps_completed": latest["step"] if latest else 0,
            "latest_mean_reverse_kl": latest["mean_reverse_kl"] if latest else None,
            "latest_mean_reward": latest["mean_reward"] if latest else None,
            "total_routed_tasks": topology["total_routed_tasks"],
            "expert_count": len(topology["experts"]),
            "routing_topology": topology,
            "step_series": step_series,
            "expert_series": dict(expert_series),
        }

    def _compute_holdout_comparisons(
        self,
        user: AuthUser,
        *,
        student_model: str | None,
        checkpoints: list[str],
    ) -> dict[str, Any]:
        """02-7 §3.1 strict guardrail:
        ONLY produce Base -> Checkpoint holdout comparison when:
        1. User holds `dataset:holdout_read` scope (S-02).
        2. Base and Checkpoint batches both have explicit `checkpoint` identity.
        3. Both evaluated on the EXACT same `-holdout` `dataset_release_id` AND `scoring_protocol`.
        """
        if not has_scope(user, "dataset:holdout_read"):
            return {
                "status": "redacted_missing_scope",
                "reason": "缺少 dataset:holdout_read 权限，已隐藏训练前后留出集对比明细",
                "items": [],
            }
        if not checkpoints:
            return {
                "status": "unavailable",
                "reason": "当前训练运行尚无已落盘 checkpoint，无法生成留出集前后对照",
                "items": [],
            }

        where_proj, proj_params = sql_project_filter(user, "b.project_id")
        rows = self.db.fetchall(
            f"""
            SELECT b.batch_id, b.batch_name, b.dataset_release_id, b.scoring_protocol,
                   b.model_canonical, b.checkpoint, b.valid_mean, b.scored, b.planned
            FROM batches b
            LEFT JOIN dataset_releases r ON r.dataset_release_id = b.dataset_release_id
            WHERE {where_proj}
              AND b.checkpoint IS NOT NULL AND TRIM(b.checkpoint) != ''
              AND b.valid_mean IS NOT NULL
              AND (
                LOWER(COALESCE(r.split, '')) = 'holdout'
                OR LOWER(COALESCE(b.dataset_release_id, '')) LIKE '%holdout%'
                OR LOWER(COALESCE(b.raw_dataset_name, '')) LIKE '%holdout%'
              )
            ORDER BY b.finished_at DESC, b.batch_id ASC
            """,
            tuple(proj_params),
        )
        if not rows:
            return {
                "status": "unavailable",
                "reason": "尚未发现带明确 checkpoint 身份的留出集（-holdout）评测批次（须同留出题集 + 同评分协议方可生成前后增益卡）",
                "items": [],
            }

        # Group by (dataset_release_id, scoring_protocol) to strictly enforce comparability!
        by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            rel_id = str(r["dataset_release_id"] or "")
            proto = str(r["scoring_protocol"] or "")
            if rel_id and proto:
                by_key[(rel_id, proto)].append(r)

        ckpt_set = set(checkpoints)
        comparisons: list[dict[str, Any]] = []
        for (rel_id, proto), group in sorted(by_key.items()):
            base_candidates = [
                r
                for r in group
                if r["checkpoint"] in ("checkpoint-0000", "base", student_model)
            ]
            step_candidates = [
                r
                for r in group
                if r["checkpoint"] in ckpt_set and r["checkpoint"] != "checkpoint-0000"
            ]
            if not base_candidates or not step_candidates:
                continue
            base = base_candidates[0]
            for cand in step_candidates:
                b_pct = round(float(base["valid_mean"]) * 100.0, 2)
                c_pct = round(float(cand["valid_mean"]) * 100.0, 2)
                delta_pp = round((float(cand["valid_mean"]) - float(base["valid_mean"])) * 100.0, 2)
                comparisons.append(
                    {
                        "dataset_release_id": rel_id,
                        "scoring_protocol": proto,
                        "base_checkpoint": base["checkpoint"],
                        "base_batch_id": base["batch_id"],
                        "base_score_pct": b_pct,
                        "candidate_checkpoint": cand["checkpoint"],
                        "candidate_batch_id": cand["batch_id"],
                        "candidate_score_pct": c_pct,
                        "delta_pp": delta_pp,
                        "status": "improved" if delta_pp > 0 else ("unchanged" if delta_pp == 0 else "regressed"),
                    }
                )

        if not comparisons:
            return {
                "status": "unavailable",
                "reason": "未找到同时满足「明确 checkpoint 身份 + 同留出题集 + 同评分协议」的 Base 与 Checkpoint 成对批次",
                "items": [],
            }
        return {"status": "available", "reason": None, "items": comparisons}

    def list_runs(self, user: AuthUser) -> dict[str, Any]:
        root = self.settings.training_runs_dir
        allow_ho = has_scope(user, "dataset:holdout_read")
        runs: list[dict[str, Any]] = []
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_symlink() or not child.is_dir() or child.name.startswith("."):
                    continue
                if (child / "routing.json").is_file() or any(
                    CKPT_DIR_RE.fullmatch(sub.name) for sub in child.iterdir() if not sub.is_symlink() and sub.is_dir()
                ):
                    parsed = self._parse_run_dir(child, allow_holdout=allow_ho)
                    runs.append(
                        {
                            "run_id": parsed["run_id"],
                            "source_path_redacted": parsed["source_path_redacted"],
                            "student_model": parsed["student_model"],
                            "checkpoint_count": parsed["checkpoint_count"],
                            "steps_completed": parsed["steps_completed"],
                            "latest_mean_reverse_kl": parsed["latest_mean_reverse_kl"],
                            "latest_mean_reward": parsed["latest_mean_reward"],
                            "total_routed_tasks": parsed["total_routed_tasks"],
                            "expert_count": parsed["expert_count"],
                        }
                    )

        return {
            "methodology_version": self.settings.methodology_version,
            "training_runs_dir_redacted": redact_path(str(root)),
            "is_empty_production_state": len(runs) == 0,
            "items": runs,
            "total": len(runs),
            "registered_train_datasets": self._list_registered_train_datasets(),
            "empty_state_guide": {
                "title": "当前 training-runs/ 目录为空（0 次真实 OPD 训练产物）",
                "honesty_notice": "平台严禁将隔离测试夹具注入生产数据库或混入线上榜单；下方提供基于真实 framework/train.py 与 framework/training_opd.py 的预检命令生成器。",
                "prerequisites": [
                    "1. 在带 GPU 的训练机上基于 1 个已通过 report.py --check-training-captures 的 harbor-runs/ 采集批次，运行 1 次最小步数（如 1–2 steps）的 training_opd.py；",
                    "2. 将产出的 routing.json 与至少 1 个 checkpoint-XXXX/opd.json 发布到 <TIANXI_SHARED>/training-runs/<run_id>/；",
                    "3. 197 平台自动发现该目录并即时渲染多专家路由拓扑图与步级双轴趋势曲线（无需重启服务）。",
                ],
                "gpu_notice": "197 (LRM-02) 无本地训练 GPU，本页表单仅生成 --plan 预检与训练机可执行 CLI 命令。",
            },
        }

    def get_run_detail(self, user: AuthUser, run_id: str) -> dict[str, Any]:
        validate_safe_id(run_id, "run_id")
        if not SAFE_RUN_ID_RE.fullmatch(run_id):
            raise AppError(400, "INVALID_PATH_OR_ID", f"非法 run_id: {run_id}")
        root = self.settings.training_runs_dir.resolve()
        raw_target = root / run_id
        if raw_target.is_symlink():
            raise AppError(400, "INVALID_PATH_OR_ID", "拒绝符号链接训练运行目录")
        target = raw_target.resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise AppError(400, "INVALID_PATH_OR_ID", "非法训练目录路径") from exc
        if not target.is_dir():
            raise AppError(404, "RESOURCE_NOT_FOUND", f"训练运行不存在: {run_id}")

        parsed = self._parse_run_dir(target, allow_holdout=has_scope(user, "dataset:holdout_read"))
        parsed["methodology_version"] = self.settings.methodology_version
        parsed["holdout_comparison"] = self._compute_holdout_comparisons(
            user,
            student_model=parsed["student_model"],
            checkpoints=parsed["checkpoints"],
        )
        return parsed

    def plan_training_cli(self, user: AuthUser, payload: dict[str, Any]) -> dict[str, Any]:
        backend = str(payload.get("backend") or "opd").strip().lower()
        if backend not in ("opd", "trl"):
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "backend 仅支持 opd 或 trl")

        datasets = payload.get("datasets") or ["tianxi-synth-v1-train"]
        if isinstance(datasets, str):
            datasets = [datasets]
        if not isinstance(datasets, list) or not datasets:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "至少选择一个训练集 dataset")

        clean_ds: list[str] = []
        for ds in datasets:
            s = str(ds).strip()
            if not re.fullmatch(r"[A-Za-z0-9._-]+", s):
                raise AppError(422, "SCHEMA_VALIDATION_FAILED", f"非法数据集名: {s}")
            if s.endswith("-holdout") or "holdout" in s.lower():
                raise AppError(
                    422,
                    "SCHEMA_VALIDATION_FAILED",
                    f"依据 training_opd.py:97 约束，严禁将留出集 `{s}` 用于 OPD 训练！",
                )
            clean_ds.append(s)

        student = str(payload.get("student") or "Qwen3_8-27B").strip()
        if not re.fullmatch(r"[A-Za-z0-9._:/-]+", student):
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "student 模型名格式非法")
        run_name = str(payload.get("run_name") or "opd-run-01").strip()
        if not SAFE_RUN_ID_RE.fullmatch(run_name):
            raise AppError(400, "INVALID_PATH_OR_ID", "run_name 格式非法")
        steps = int(payload.get("steps") or 2)
        if steps < 1 or steps > 10000:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "steps 必须在 1..10000 之间")
        rollouts_per_step = payload.get("rollouts_per_step")
        seed = int(payload.get("seed") or 42)

        if backend == "opd":
            plan_cmd = ["python3", "framework/train.py", "--backend", "opd", "--plan", "--"]
            run_cmd = ["python3", "framework/train.py", "--backend", "opd", "--"]
            for ds in clean_ds:
                plan_cmd.extend(["--dataset", ds])
                run_cmd.extend(["--dataset", ds])
            run_cmd.extend(
                [
                    "--student",
                    student,
                    "--output",
                    f"<TIANXI_SHARED>/training-runs/{run_name}",
                    "--steps",
                    str(steps),
                    "--seed",
                    str(seed),
                ]
            )
            if rollouts_per_step is not None and int(rollouts_per_step) >= 1:
                run_cmd.extend(["--rollouts-per-step", str(int(rollouts_per_step))])
        else:
            plan_cmd = [
                "python3",
                "framework/train.py",
                "--backend",
                "trl",
                "--trl-root",
                "<GPU_HOST_TRL_ROOT>",
                "--plan",
                "--",
                "--model_name_or_path",
                student,
            ]
            run_cmd = [p for p in plan_cmd if p != "--plan"]

        return {
            "ok": True,
            "backend": backend,
            "gpu_host_required": True,
            "plan_command": shlex.join(plan_cmd),
            "train_command": shlex.join(run_cmd),
            "expected_artifacts": [
                f"training-runs/{run_name}/routing.json",
                f"training-runs/{run_name}/checkpoint-0000/opd.json",
                f"training-runs/{run_name}/checkpoint-0001/opd.json",
            ],
        }
