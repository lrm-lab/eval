from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.db.database import Database

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db(db: Database) -> None:
    ddl = SCHEMA_FILE.read_text(encoding="utf-8")
    db.executescript(ddl)


def load_taxonomy_config(taxonomy_file: Path) -> dict[str, Any]:
    if not taxonomy_file.is_file():
        return {"tracks": [], "benchmarks": []}
    return yaml.safe_load(taxonomy_file.read_text(encoding="utf-8")) or {"tracks": [], "benchmarks": []}


def sync_taxonomy_to_db(db: Database, taxonomy_file: Path) -> dict[str, int]:
    """Idempotently sync config/taxonomy.yaml into benchmarks, dataset_releases, and dataset_source_aliases."""
    cfg = load_taxonomy_config(taxonomy_file)
    tracks_by_slug = {t["track_slug"]: t for t in cfg.get("tracks", [])}
    now = utcnow_iso()
    bench_count = 0
    release_count = 0
    alias_count = 0

    with db.transaction() as conn:
        for bm in cfg.get("benchmarks", []):
            slug = bm["benchmark_slug"]
            track_slug = bm["track_slug"]
            track_meta = tracks_by_slug.get(track_slug, {})
            track_display = track_meta.get("display_name", track_slug)
            goal_type = bm.get("goal_type") or track_meta.get("goal_type", "extended_baseline")
            meta = {
                "core_14": bool(bm.get("core_14", False)),
                "track_order": track_meta.get("order", 99),
                "track_zh_name": track_meta.get("zh_name", track_display),
                "default_visible": bool(track_meta.get("default_visible", True)),
            }
            conn.execute(
                """
                INSERT INTO benchmarks (
                    benchmark_slug, display_name, track_slug, track_display_name,
                    goal_type, capability, modality, language, status, metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(benchmark_slug) DO UPDATE SET
                    display_name = excluded.display_name,
                    track_slug = excluded.track_slug,
                    track_display_name = excluded.track_display_name,
                    goal_type = excluded.goal_type,
                    capability = excluded.capability,
                    modality = excluded.modality,
                    language = excluded.language,
                    status = excluded.status,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    slug,
                    bm["display_name"],
                    track_slug,
                    track_display,
                    goal_type,
                    bm.get("capability", ""),
                    bm.get("modality", "agent"),
                    bm.get("language", "en"),
                    bm.get("status", "integrated"),
                    json.dumps(meta, ensure_ascii=False),
                    now,
                ),
            )
            bench_count += 1

            for rel in bm.get("releases", []):
                rel_id = rel["dataset_release_id"]
                conn.execute(
                    """
                    INSERT INTO dataset_releases (
                        dataset_release_id, benchmark_slug, dataset_version, split,
                        runner, known_task_count, metric_name, default_protocol,
                        scale_type, media_rule_ver, source_approval, metadata_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dataset_release_id) DO UPDATE SET
                        benchmark_slug = excluded.benchmark_slug,
                        dataset_version = excluded.dataset_version,
                        split = excluded.split,
                        runner = excluded.runner,
                        known_task_count = excluded.known_task_count,
                        metric_name = excluded.metric_name,
                        default_protocol = excluded.default_protocol,
                        scale_type = excluded.scale_type,
                        media_rule_ver = excluded.media_rule_ver,
                        source_approval = excluded.source_approval,
                        metadata_json = excluded.metadata_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        rel_id,
                        slug,
                        str(rel.get("dataset_version", "default")),
                        str(rel.get("split", "default")),
                        str(rel.get("runner", "harbor")),
                        rel.get("known_task_count"),
                        str(rel.get("metric_name", "reward")),
                        str(rel.get("default_protocol", "unknown")),
                        str(rel.get("scale_type", "ratio_0_1")),
                        rel.get("media_rule_ver"),
                        str(rel.get("source_approval", "pending")),
                        json.dumps(rel.get("metadata", {}), ensure_ascii=False),
                        now,
                    ),
                )
                release_count += 1

                for al in rel.get("aliases", []):
                    raw_name = al["raw_dataset_name"]
                    ns = str(al.get("source_namespace", "*"))
                    runner = str(al.get("runner", "*"))
                    alias_id = "al_" + hashlib.sha256(f"{raw_name}|{ns}|{runner}".encode("utf-8")).hexdigest()[:16]
                    conn.execute(
                        """
                        INSERT INTO dataset_source_aliases (
                            alias_id, raw_dataset_name, source_namespace, runner,
                            dataset_release_id, inferred_run_scope, verified_by, verified_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(raw_dataset_name, source_namespace, runner) DO UPDATE SET
                            dataset_release_id = excluded.dataset_release_id,
                            inferred_run_scope = excluded.inferred_run_scope,
                            verified_by = excluded.verified_by,
                            verified_at = excluded.verified_at
                        """,
                        (
                            alias_id,
                            raw_name,
                            ns,
                            runner,
                            rel_id,
                            al.get("inferred_run_scope"),
                            "taxonomy.yaml",
                            now,
                        ),
                    )
                    alias_count += 1

    return {"benchmarks": bench_count, "releases": release_count, "aliases": alias_count}
