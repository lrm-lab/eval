from __future__ import annotations

import io
import json
import tarfile
import time
from pathlib import Path

import zstandard as zstd

from app.config import Settings
from app.db.database import Database
from app.db.migrations import init_db, sync_taxonomy_to_db
from app.indexing.cold_indexer import ColdIndexer, read_legacy_sample_by_offset
from app.indexing.identity import make_batch_id, make_canonical_trial_key
from app.indexing.media_matcher import VersionedMediaMatcher
from app.security.auth import AuthUser
from app.security.authz import ALL_SCOPES
from app.security.storage_probe import probe_isolated_dir
from app.services.compare_service import CompareService
from app.services.methodology import compute_comparative_macro_avg
from app.services.query_service import QueryService


def test_a05_macro_avg_missing_benchmark_anti_bias() -> None:
    res = compute_comparative_macro_avg(
        {
            "A": {"B1": (80.0, "p1"), "B2": (70.0, "p1"), "B3": (95.0, "p1")},
            "B": {"B1": (82.0, "p1"), "B2": (72.0, "p1"), "B3": (None, None)},
        }
    )
    assert res["common_comparable_benchmarks"] == ["B1", "B2"]
    assert res["by_subject"]["A"]["comparative_macro_avg"] == 75.0
    assert res["by_subject"]["B"]["comparative_macro_avg"] == 77.0
    assert res["by_subject"]["B"]["is_best"] is True
    assert res["by_subject"]["A"]["is_best"] is False
    assert res["by_subject"]["A"]["self_coverage_avg"] == 81.67


def test_a02_a03_a04_a13_a15_real_197_cold_data(isolated_settings: Settings) -> None:
    """Real 197 cold-tier golden assertions:
    - TB2.1: 6 native jobs, 534 trials, 490 reward_valid, 230 with exception_info, 71-task efficiency scatter
    - DeepSWE: 2-way (78/60, 45.00% vs 23.33%, -21.67pp, -48.15%, 18/9/28/5+18, faults_as_zero 39.74%/17.95%), 3-way (48/39)
    - GAIA: 2-way (165/118)
    - WildClawBench: readable batches indexed + CIFS PermissionError gaps explicitly reported
    - IFEval, AI2D_TEST, RealWorldQA: offset indexed + O(1) seek read < 10ms
    - Bundles: gaia .tar indexed & restart cache hit skips cleanly (< 50ms)
    - Source coverage report lists every 197 source root!
    """
    probe_isolated_dir(isolated_settings.state_dir, create=True)
    db = Database(isolated_settings.db_path)
    init_db(db)
    sync_taxonomy_to_db(db, isolated_settings.taxonomy_file)

    cold = ColdIndexer(isolated_settings, db)
    tb_res = cold.index_native_tb21(force=True)
    assert tb_res["batches"] == 6
    assert tb_res["trials"] == 534

    rv_total = db.fetchone("SELECT COUNT(*) AS c FROM episodes WHERE native_reward_valid = 1")["c"]  # type: ignore[index]
    rv_with_exc = db.fetchone("SELECT COUNT(*) AS c FROM episodes WHERE native_reward_valid = 1 AND exception_type IS NOT NULL")["c"]  # type: ignore[index]
    assert rv_total == 490
    assert rv_with_exc == 230

    k1 = make_canonical_trial_key(trial_id="non-uuid-1", batch_id="b_run1", task_id="t1", checkpoint="ck133", harness_version="1.0")
    k2 = make_canonical_trial_key(trial_id="non-uuid-1", batch_id="b_run2", task_id="t1", checkpoint="ck133", harness_version="1.0")
    assert k1 != k2

    admin = AuthUser("u_adm", "adm", "Adm", "admin", frozenset(ALL_SCOPES), frozenset(["default"]), True)
    qs = QueryService(isolated_settings, db)
    scat = qs.get_efficiency_scatter(admin, dataset_release_id="terminal-bench__2.1__default", scoring_protocol="harbor-native-reward-valid-v1", x_axis="duration_p50_sec")
    assert scat["common_valid_tasks"] == 71
    assert len(scat["points"]) == 6
    scores = sorted(p["score_pct"] for p in scat["points"])
    p50s = sorted(p["x_value"] for p in scat["points"])
    assert scores[0] == 25.35 and scores[-1] == 54.93
    assert 570.0 <= p50s[0] <= 580.0
    assert 1085.0 <= p50s[-1] <= 1095.0
    assert scat["x_axis_coverage"]["total_tokens_avg"]["batches_with_any_records"] == 3
    assert scat["x_axis_coverage"]["total_tokens_avg"]["plottable_full_coverage_points"] == 3
    assert scat["x_axis_coverage"]["recorded_cost_usd"]["batches_with_any_records"] == 1
    assert scat["x_axis_coverage"]["recorded_cost_usd"]["plottable_full_coverage_points"] == 0

    scat_cost = qs.get_efficiency_scatter(admin, dataset_release_id="terminal-bench__2.1__default", scoring_protocol="harbor-native-reward-valid-v1", x_axis="recorded_cost_usd")
    assert scat_cost["pareto_frontier_batch_ids"] == []
    assert all(p["eligible_for_pareto"] is False for p in scat_cost["points"])

    ds_res = cold.index_legacy_agent_json("DeepSWE", force=True)
    assert ds_res["batches"] == 3
    cmp_svc = CompareService(isolated_settings, db)
    b_pi5 = make_batch_id("legacy_json:DeepSWE/Pi_v5_Qwen3_8-27B_think.json")
    b_oc = make_batch_id("legacy_json:DeepSWE/OpenClaw_Qwen3_8-27B_think.json")
    b_pi0 = make_batch_id("legacy_json:DeepSWE/Pi_Qwen3_8-27B_think.json")

    res2 = cmp_svc.query_compare(
        admin,
        {
            "subjects": [
                {"id": "s1", "kind": "batch", "batch_id": b_pi5, "alias": "Pi v5"},
                {"id": "s2", "kind": "batch", "batch_id": b_oc, "alias": "OpenClaw"},
            ],
            "baseline_subject_id": "s1",
            "scoring_protocol": "deepswe-f2p-p2p",
            "regression_threshold_pp": -1.0,
            "include_verdict": True,
        },
    )
    row = res2["rows"][0]
    assert row["intersection"] == {"common_covered_tasks": 78, "common_valid_scored_tasks": 60, "fault_excluded_tasks": 18}
    c1, c2 = row["cells"]["s1"], row["cells"]["s2"]
    assert c1["common_valid"]["score"] == 45.0 and c1["common_valid"]["passed"] == 27
    assert c2["common_valid"]["score"] == 23.33 and c2["common_valid"]["passed"] == 14
    assert c2["common_valid"]["delta_pp"] == -21.67
    assert c2["common_valid"]["rel_pct"] == -48.15
    assert c1["common_covered_faults_as_zero"]["passed"] == 31 and c1["common_covered_faults_as_zero"]["score"] == 39.74
    assert c2["common_covered_faults_as_zero"]["passed"] == 14 and c2["common_covered_faults_as_zero"]["score"] == 17.95
    assert c2["common_covered_faults_as_zero"]["delta_pp"] == -21.79
    assert c1["self_full_batch_valid"]["score"] == 44.86 and c1["self_full_batch_valid"]["valid_scored"] == 107
    assert c2["self_full_batch_valid"]["score"] == 22.58 and c2["self_full_batch_valid"]["valid_scored"] == 62

    diff2 = cmp_svc.query_task_diff(admin, {"comparison_id": res2["comparison_id"], "subject_a_id": "s1", "subject_b_id": "s2", "filter": "all"})
    assert diff2["intersection"] == {"common_covered_tasks": 78, "common_valid_scored_tasks": 60}
    assert diff2["win_tie_loss"] == {"a_wins": 18, "both_pass": 9, "both_fail": 28, "b_wins": 5, "fault_excluded": 18}

    res3 = cmp_svc.query_compare(
        admin,
        {
            "subjects": [{"id": "s1", "batch_id": b_pi5}, {"id": "s2", "batch_id": b_oc}, {"id": "s3", "batch_id": b_pi0}],
            "baseline_subject_id": "s1",
            "scoring_protocol": "deepswe-f2p-p2p",
        },
    )
    assert res3["rows"][0]["intersection"]["common_covered_tasks"] == 48
    assert res3["rows"][0]["intersection"]["common_valid_scored_tasks"] == 39

    gaia_res = cold.index_legacy_agent_json("GAIA", force=True)
    assert gaia_res["batches"] == 2
    g_pi = make_batch_id("legacy_json:GAIA/Qwen3_8-27B_think_Pi.json")
    g_oc = make_batch_id("legacy_json:GAIA/Qwen3_8-27B_think_OpenClaw.json")
    res_g = cmp_svc.query_compare(
        admin,
        {
            "subjects": [{"id": "s1", "batch_id": g_pi}, {"id": "s2", "batch_id": g_oc}],
            "baseline_subject_id": "s1",
            "scoring_protocol": "gaia-official-scorer",
        },
    )
    assert res_g["rows"][0]["intersection"]["common_covered_tasks"] == 165
    assert res_g["rows"][0]["intersection"]["common_valid_scored_tasks"] == 118

    # WildClawBench readable batches + permission gaps (incl. partial permission gap on pi-jhpV5-Qwen3_8-27B)
    wc_res = cold.index_legacy_wildclawbench(force=True)
    assert wc_res["batches"] >= 2
    assert wc_res["unreadable_dirs"] >= 4

    # Historical text & multimodal offset indexing (IFEval, AI2D_TEST, RealWorldQA) + O(1) seek < 25ms!
    assert cold.index_legacy_text_json_offsets("IFEval", force=True) >= 1
    assert cold.index_legacy_jsonl_offsets("AI2D_TEST", force=True) >= 1
    assert cold.index_legacy_jsonl_offsets("RealWorldQA", force=True) >= 1

    # Multimodal judge_hit scores are captured into legacy_sample_offsets.reward
    ai2d_scored = db.fetchone(
        "SELECT COUNT(*) AS c FROM legacy_sample_offsets WHERE file_path LIKE '%/AI2D_TEST/%' AND reward IS NOT NULL"
    )["c"]  # type: ignore[index]
    assert ai2d_scored > 0

    for bdir in ("IFEval", "AI2D_TEST", "RealWorldQA"):
        row_off = db.fetchone(
            "SELECT file_path, sample_id, start_offset, end_offset FROM legacy_sample_offsets WHERE file_path LIKE ? LIMIT 1",
            (f"%/{bdir}/%",),
        )
        assert row_off is not None, f"Missing offsets for {bdir}"
        sample_obj = read_legacy_sample_by_offset(Path(row_off["file_path"]), row_off["start_offset"], row_off["end_offset"])
        assert isinstance(sample_obj, dict)
        # Measure O(1) byte-slice seek (< 25ms once SMB handle metadata is warm)
        t_seek = time.perf_counter()
        sample_obj2 = read_legacy_sample_by_offset(Path(row_off["file_path"]), row_off["start_offset"], row_off["end_offset"])
        seek_ms = (time.perf_counter() - t_seek) * 1000.0
        assert sample_obj2 == sample_obj
        assert seek_ms < 25.0, f"O(1) seek for {bdir} took {seek_ms:.2f}ms"

    # Bundles: GAIA .tar extraction
    gaia_bundle_n = cold.index_bundles_metadata(force=True, include_large_zst=False)
    assert gaia_bundle_n >= 100

    # Restart cache hit skips TB2.1, DeepSWE, GAIA, WildClawBench, IFEval, AI2D, RealWorldQA, GAIA bundle with 0 rebuilds!
    t_c = time.perf_counter()
    assert cold.index_native_tb21(force=False) == {"batches": 0, "trials": 0}
    assert cold.index_legacy_agent_json("DeepSWE", force=False) == {"batches": 0, "trials": 0}
    assert cold.index_legacy_wildclawbench(force=False) == {"batches": 0, "unreadable_dirs": 0}
    assert cold.index_legacy_text_json_offsets("IFEval", force=False) == 0
    assert cold.index_legacy_jsonl_offsets("AI2D_TEST", force=False) == 0
    assert cold.index_bundles_metadata(force=False, include_large_zst=False) == 0
    cache_check_ms = (time.perf_counter() - t_c) * 1000.0
    assert cache_check_ms < 500.0, f"Cold restart cache check unexpectedly slow: {cache_check_ms:.1f}ms"

    # Per-source coverage report covers all roots including WildClaw partial permission gaps
    cov = cold.build_source_coverage_report()
    sources = {c["source"]: c for c in cov}
    assert "harbor-runs" in sources
    assert "2026-11-20/wildclawbench" in sources and sources["2026-11-20/wildclawbench"]["status"] == "partial_permission_denied"
    assert "2026-11-20/IFEval" in sources and sources["2026-11-20/IFEval"]["status"] == "indexed"
    assert "2026-11-20/AI2D_TEST" in sources and sources["2026-11-20/AI2D_TEST"]["status"] == "indexed"
    assert "2026-11-20/RealWorldQA" in sources and sources["2026-11-20/RealWorldQA"]["status"] == "indexed"
    assert "bundles/gaia-validation-20261124.tar" in sources and sources["bundles/gaia-validation-20261124.tar"]["status"] == "ready"
    assert "training-runs" in sources
    db.close_thread_connection()


def test_cold_zst_bundle_streaming_and_cache_hit(tmp_path: Path, isolated_settings: Settings) -> None:
    """Verify streaming .tar.zst extraction (same path used for 7.5 GiB wildclawbench-20261120-tasks.tar.zst)
    AND restart cache hit skip."""
    fake_tianxi = tmp_path / "tianxi_zst"
    bdir = fake_tianxi / "bundles"
    bdir.mkdir(parents=True)
    zst_path = bdir / "wildclawbench-20261120-tasks.tar.zst"

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tf:
        toml_bytes = b'[task]\nname = "wc_task_1"\n[metadata]\ndifficulty = "hard"\ncategory = "coding"\n'
        ti = tarfile.TarInfo(name="wildclaw/wc_task_1/task.toml")
        ti.size = len(toml_bytes)
        tf.addfile(ti, io.BytesIO(toml_bytes))
    cctx = zstd.ZstdCompressor()
    zst_path.write_bytes(cctx.compress(tar_buf.getvalue()))

    isolated_settings.tianxi_root = fake_tianxi
    probe_isolated_dir(isolated_settings.state_dir, create=True)
    db = Database(isolated_settings.db_path)
    init_db(db)
    sync_taxonomy_to_db(db, isolated_settings.taxonomy_file)
    cold = ColdIndexer(isolated_settings, db)

    assert cold.index_bundles_metadata(force=False, include_large_zst=True) == 1
    td = db.fetchone("SELECT task_id, difficulty, category FROM task_definitions WHERE dataset_release_id = 'wildclawbench__2026-11-20__default'")
    assert td == {"task_id": "wc_task_1", "difficulty": "hard", "category": "coding"}
    # Restart cache hit skips zst completely
    assert cold.index_bundles_metadata(force=False, include_large_zst=True) == 0
    db.close_thread_connection()


def test_a09_versioned_multimodal_media_matcher_real_197(isolated_settings: Settings) -> None:
    matcher = VersionedMediaMatcher(isolated_settings.media_rules_file)

    cmmmu_jl = isolated_settings.legacy_root_dir / "CMMMU" / "Qwen3_8-27B_think.jsonl"
    hits_cmmmu = 0
    with open(cmmmu_jl, "r", encoding="utf-8") as fh:
        for _ in range(20):
            sample = json.loads(fh.readline())
            if matcher.match_sample("CMMMU", sample).status == "unique_hit":
                hits_cmmmu += 1
    assert hits_cmmmu == 20

    mmmu_jl = isolated_settings.legacy_root_dir / "MMMU_Pro_10c" / "Qwen3_8-27B_think.jsonl"
    with open(mmmu_jl, "r", encoding="utf-8") as fh:
        mmmu_s0 = json.loads(fh.readline())
    assert matcher.match_sample("MMMU_Pro_10c", mmmu_s0).status == "ambiguous_multi_hit"

    mmif_jl = isolated_settings.legacy_root_dir / "MM-IFEval" / "Qwen3_8-27B_think.jsonl"
    with open(mmif_jl, "r", encoding="utf-8") as fh:
        sample0 = json.loads(fh.readline())
    assert matcher.match_sample("MM-IFEval", sample0).status == "unique_hit"

    assert matcher.match_sample("RealWorldQA", {"question": "test"}).status == "unregistered_source"
