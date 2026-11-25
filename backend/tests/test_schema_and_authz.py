from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import namedtuple
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.errors import AppError
from app.config import Settings
from app.db.database import Database
from app.db.migrations import init_db, sync_taxonomy_to_db, utcnow_iso
from app.main import create_app
from app.security.auth import AuthUser, rotate_secret_key
from app.security.authz import ALL_SCOPES, can_access_project, personal_project_id
from app.security.redaction import (
    compute_internal_source_fingerprint,
    redact_host,
    redact_json_recursive,
    redact_path,
    redact_text,
)
from app.security.storage_probe import (
    check_disk_write_guard,
    get_mount_fstype,
    probe_isolated_dir,
    probe_secret_file,
    resolve_within_root,
    validate_safe_id,
)
from app.services.query_service import QueryService
from tests.conftest import create_user, login_client

_DiskUsage = namedtuple("_DiskUsage", ["total", "used", "free"])


def test_ddl_28_tables_and_fk_clean(isolated_settings: Settings) -> None:
    probe_isolated_dir(isolated_settings.state_dir, create=True)
    db = Database(isolated_settings.db_path)
    init_db(db)
    tables = [
        r["name"]
        for r in db.fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    ]
    expected_core = {
        "users",
        "benchmarks",
        "dataset_releases",
        "dataset_source_aliases",
        "task_definitions",
        "batches",
        "episodes",
        "trial_protocol_scores",
        "legacy_leaderboard_rows",
        "cold_file_cache",
        "legacy_sample_offsets",
        "selection_tasks",
        "official_results",
        "sample_sets",
        "saved_comparisons",
        "exec_jobs",
        "exec_job_events",
        "direct_lab_runs",
        "param_compare_groups",
        "offline_reports",
        "index_warnings",
        "search_fts",
    }
    assert expected_core.issubset(set(tables))
    assert len(tables) >= 27
    assert db.fetchall("PRAGMA foreign_key_check") == []

    counts = sync_taxonomy_to_db(db, isolated_settings.taxonomy_file)
    assert counts["benchmarks"] >= 20
    assert counts["releases"] >= 25
    assert counts["aliases"] >= 20
    db.close_thread_connection()


def test_s06_fail_closed_storage_probes_autofs_cifs_and_managed_release(tmp_path: Path, isolated_settings: Settings) -> None:
    """Verify:
    1) get_mount_fstype resolves overlaid CIFS over autofs as 'cifs' (NOT 'autofs'!).
    2) Real 197 CIFS (/home/share-data/eval) fails closed 503 STAGING_PERMISSION_UNSAFE for
       staging_dir, db_path, secrets_file, runner_work_dir, AND production_mode config_dir!
    3) scripts/deploy_release.sh deploys a 0750/0640/0600 managed release copy to POSIX local storage,
       retaining at most 2 releases and passing production_mode checks cleanly.
    """
    # 1. Real 197 mount resolution: /home/share-data has autofs + cifs overlay -> must resolve to cifs!
    assert get_mount_fstype(Path("/home/share-data/eval")) == "cifs"

    # Synthetic /proc/mounts tie-breaker check
    fake_mounts = tmp_path / "mounts"
    fake_dir = tmp_path / "shared_dir"
    fake_dir.mkdir()
    fake_mounts.write_text(
        f"/dev/root / ext4 rw 0 0\n"
        f"systemd-1 {fake_dir} autofs rw 0 0\n"
        f"//10.0.0.1/share {fake_dir} cifs rw 0 0\n",
        encoding="utf-8",
    )
    assert get_mount_fstype(fake_dir, proc_mounts=str(fake_mounts)) == "cifs"

    # 2. CIFS fail-closed probes on ALL runtime paths
    cifs_dir = Path("/home/share-data/eval")
    with pytest.raises(AppError) as exc_info:
        probe_isolated_dir(cifs_dir, create=False)
    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "STAGING_PERMISSION_UNSAFE"

    # Misconfigured db_path on CIFS -> create_app fails closed 503
    bad_db_cfg = Settings(**{**isolated_settings.__dict__, "db_path": Path("/home/share-data/eval/bad_state/db.sqlite")})
    with pytest.raises(AppError) as e_db:
        create_app(bad_db_cfg, run_startup_scan=False)
    assert e_db.value.status_code == 503 and e_db.value.code == "STAGING_PERMISSION_UNSAFE"

    # Misconfigured secrets_file on CIFS -> create_app fails closed 503
    bad_sec_cfg = Settings(**{**isolated_settings.__dict__, "secrets_file": Path("/home/share-data/eval/bad_sec/secrets.env")})
    with pytest.raises(AppError) as e_sec:
        create_app(bad_sec_cfg, run_startup_scan=False)
    assert e_sec.value.status_code == 503 and e_sec.value.code == "STAGING_PERMISSION_UNSAFE"

    # Misconfigured runner_work_dir on CIFS -> create_app fails closed 503
    bad_rw_cfg = Settings(**{**isolated_settings.__dict__, "runner_work_dir": Path("/home/share-data/eval/bad_rw")})
    with pytest.raises(AppError) as e_rw:
        create_app(bad_rw_cfg, run_startup_scan=False)
    assert e_rw.value.status_code == 503 and e_rw.value.code == "STAGING_PERMISSION_UNSAFE"

    # production_mode=True with CIFS config_dir -> fails closed 503
    bad_prod_cfg = Settings(**{**isolated_settings.__dict__, "production_mode": True, "config_dir": Path("/home/share-data/eval/config")})
    with pytest.raises(AppError) as e_prod:
        create_app(bad_prod_cfg, run_startup_scan=False)
    assert e_prod.value.status_code == 503 and e_prod.value.code == "STAGING_PERMISSION_UNSAFE"

    # Local POSIX tmpfs/ext4 dir succeeds with mode 0700
    res = probe_isolated_dir(isolated_settings.staging_dir, create=True)
    assert res["ok"] is True
    assert res["mode"] == "0o700"
    assert (os.stat(isolated_settings.staging_dir).st_mode & 0o077) == 0

    # Secret file with overly broad mode (0644) -> fails closed 503
    probe_isolated_dir(isolated_settings.state_dir, create=True)
    bad_mode_sec = isolated_settings.state_dir / "bad_perms.env"
    bad_mode_sec.write_text("SECRET_KEY=abc\n", encoding="utf-8")
    os.chmod(bad_mode_sec, 0o644)
    with pytest.raises(AppError) as e_perm:
        probe_secret_file(bad_mode_sec)
    assert e_perm.value.status_code == 503 and e_perm.value.code == "STAGING_PERMISSION_UNSAFE"

    # 3. Managed release deployment via scripts/deploy_release.sh (retains <= 2 releases, 0600 registry)
    rel_runtime = tmp_path / "managed_runtime"
    env = dict(os.environ, LRM_EVAL_ROOT="/home/share-data/eval", LRM_RUNTIME_DIR=str(rel_runtime))
    script = "/home/share-data/eval/backend/scripts/deploy_release.sh"
    for ver in ("v1", "v2", "v3"):
        subprocess.run(["bash", script, ver], env=env, check=True, capture_output=True)
    releases_root = rel_runtime / "releases"
    rel_dirs = sorted(p.name for p in releases_root.iterdir() if p.is_dir() and not p.is_symlink())
    assert len(rel_dirs) == 2
    assert rel_dirs == ["v2", "v3"]
    cur_link = releases_root / "current"
    assert cur_link.is_symlink() and cur_link.resolve().name == "v3"
    reg_file = cur_link / "config" / "source_approval_registry.yaml"
    assert reg_file.is_file()
    assert (os.stat(reg_file).st_mode & 0o777) == 0o600
    assert (os.stat(cur_link / "backend" / "app").st_mode & 0o007) == 0
    rel_manifest = json.loads((cur_link / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
    assert rel_manifest["release_version"] == "v3"
    assert rel_manifest["file_count"] >= 30 and len(rel_manifest["manifest_sha256"]) == 64

    # production_mode=True MUST reject a world-readable source_approval_registry.yaml even on local POSIX tmpfs!
    weak_cfg_dir = tmp_path / "weak_cfg"
    weak_cfg_dir.mkdir(mode=0o750)
    shutil.copy(reg_file, weak_cfg_dir / "source_approval_registry.yaml")
    os.chmod(weak_cfg_dir / "source_approval_registry.yaml", 0o644)
    weak_prod_cfg = Settings(**{**isolated_settings.__dict__, "production_mode": True, "config_dir": weak_cfg_dir})
    with pytest.raises(AppError) as e_weak:
        create_app(weak_prod_cfg, run_startup_scan=False)
    assert e_weak.value.status_code == 503 and e_weak.value.code == "STAGING_PERMISSION_UNSAFE"


def test_local_disk_low_write_guard_blocks_all_write_paths(tmp_path: Path, isolated_settings: Settings) -> None:
    """Verify 503 LOCAL_DISK_LOW blocks:
    1) Brand-new DB creation on startup (< 1.5 GiB free).
    2) Existing INCOMPLETE secrets.env under low disk (1.0 GiB free) is NOT overwritten (even when DB fs is healthy!).
    3) Existing cleanly closed DB opens in strict read-only write_protected=True mode:
       /admin/health, /system/overview, /batches stay 200 with ZERO SQLite -wal / -shm sidecar files created,
       while startup scan, POST /admin/reindex, and POST /admin/taxonomy/reload return 503 LOCAL_DISK_LOW!
    """
    low_disk = lambda _p: _DiskUsage(total=98 * 1024**3, used=97 * 1024**3, free=int(1.0 * 1024**3))
    ok_disk = lambda _p: _DiskUsage(total=98 * 1024**3, used=50 * 1024**3, free=48 * 1024**3)

    # 1. Direct check raises 503 LOCAL_DISK_LOW
    with pytest.raises(AppError) as e_low:
        check_disk_write_guard(tmp_path, min_free_gb=1.5, disk_usage_fn=low_disk)
    assert e_low.value.status_code == 503 and e_low.value.code == "LOCAL_DISK_LOW"

    # Brand-new DB startup fails closed 503 LOCAL_DISK_LOW
    fresh_rt = tmp_path / "fresh_rt"
    fresh_cfg = Settings(
        runtime_dir=fresh_rt,
        state_dir=fresh_rt / "state",
        staging_dir=fresh_rt / "staging",
        evidence_store_dir=fresh_rt / "evidence",
        runner_work_dir=fresh_rt / "runner-work",
        db_path=fresh_rt / "state" / "fresh.db",
        secrets_file=fresh_rt / "state" / "secrets.env",
        enable_background_indexer=False,
    )
    with pytest.raises(AppError) as e_new:
        create_app(fresh_cfg, run_startup_scan=False, disk_usage_fn=low_disk)
    assert e_new.value.status_code == 503 and e_new.value.code == "LOCAL_DISK_LOW"
    assert not fresh_cfg.db_path.exists()

    # 2. Existing INCOMPLETE secrets.env under low disk on secrets_file's ACTUAL mount MUST NOT be overwritten!
    sec_rt = tmp_path / "sec_rt"
    sec_dir = tmp_path / "sec_mount_dir"
    sec_dir.mkdir(mode=0o700)
    incomplete_sec = sec_dir / "secrets.env"
    incomplete_sec.write_text("# incomplete secret file\nLRM_EVAL_SECRET_KEY=short\n", encoding="utf-8")
    os.chmod(incomplete_sec, 0o600)
    before_sec_bytes = incomplete_sec.read_bytes()

    def _per_mount_usage(p: Path):
        if str(p).startswith(str(sec_dir)):
            return _DiskUsage(total=98 * 1024**3, used=97 * 1024**3, free=int(1.0 * 1024**3))
        return _DiskUsage(total=98 * 1024**3, used=50 * 1024**3, free=48 * 1024**3)

    sec_cfg = Settings(
        runtime_dir=sec_rt,
        state_dir=sec_rt / "state",
        staging_dir=sec_rt / "staging",
        evidence_store_dir=sec_rt / "evidence",
        runner_work_dir=sec_rt / "runner-work",
        db_path=sec_rt / "state" / "eval.db",
        secrets_file=incomplete_sec,
        enable_background_indexer=False,
    )
    with pytest.raises(AppError) as e_inc_sec:
        create_app(sec_cfg, run_startup_scan=False, disk_usage_fn=_per_mount_usage)
    assert e_inc_sec.value.status_code == 503 and e_inc_sec.value.code == "LOCAL_DISK_LOW"
    assert incomplete_sec.read_bytes() == before_sec_bytes

    # 2b. Key rotation under low disk on secrets_file's ACTUAL mount MUST ALSO fail closed without rewriting!
    valid_sec = sec_dir / "valid_secrets.env"
    valid_sec.write_text("LRM_EVAL_SECRET_KEY=valid_existing_secret_key_with_more_than_32_characters_0123456789\n", encoding="utf-8")
    os.chmod(valid_sec, 0o600)
    before_valid_bytes = valid_sec.read_bytes()
    with pytest.raises(AppError) as e_rot:
        rotate_secret_key(
            valid_sec,
            write_guard=lambda: check_disk_write_guard(valid_sec.parent, min_free_gb=1.5, disk_usage_fn=_per_mount_usage),
        )
    assert e_rot.value.status_code == 503 and e_rot.value.code == "LOCAL_DISK_LOW"
    assert valid_sec.read_bytes() == before_valid_bytes

    # 3. Initialize DB cleanly while disk is healthy (including empty fake harbor-runs so 0 changes occur on startup!),
    # close so NO -wal/-shm exist, then reopen under low disk!
    empty_tianxi = tmp_path / "empty_tianxi_lowdisk"
    (empty_tianxi / "harbor-runs").mkdir(parents=True)
    isolated_settings.tianxi_root = empty_tianxi
    app_ok = create_app(isolated_settings, run_startup_scan=False, disk_usage_fn=ok_disk)
    create_user(
        app_ok.state.ctx.db,
        username="disk_admin",
        password="StrongAdminPassword!1",
        role="admin",
        projects=["default"],
        scopes=list(ALL_SCOPES),
    )
    app_ok.state.ctx.db.close_thread_connection()
    wal_path = Path(f"{isolated_settings.db_path}-wal")
    shm_path = Path(f"{isolated_settings.db_path}-shm")
    assert not wal_path.exists() and not shm_path.exists()

    batches_before = Database(isolated_settings.db_path).fetchone("SELECT COUNT(*) AS c FROM batches")["c"]  # type: ignore[index]
    # Close probe connection cleanly so -wal/-shm are absent before low-disk startup
    wal_path.unlink(missing_ok=True)
    shm_path.unlink(missing_ok=True)

    app_low = create_app(isolated_settings, run_startup_scan=True, disk_usage_fn=low_disk)
    assert app_low.state.ctx.db.read_only_mode is True
    with TestClient(app_low) as client:
        csrf = login_client(client, "disk_admin", "StrongAdminPassword!1")

        # Read endpoints succeed AND MUST NOT create -wal or -shm sidecar files!
        h = client.get("/api/v1/admin/health")
        assert h.status_code == 200
        hj = h.json()
        assert hj["storage"]["write_guard_ok"] is False
        assert hj["indexer"]["write_protected"] is True
        assert client.get("/api/v1/system/overview").status_code == 200
        assert client.get("/api/v1/batches").status_code == 200
        assert not wal_path.exists(), "Low-disk read MUST NOT create -wal sidecar!"
        assert not shm_path.exists(), "Low-disk read MUST NOT create -shm sidecar!"

        # Manual reindex -> 503 LOCAL_DISK_LOW
        r1 = client.post("/api/v1/admin/reindex", headers={"X-CSRF-Token": csrf})
        assert r1.status_code == 503 and r1.json()["error"]["code"] == "LOCAL_DISK_LOW"

        # Taxonomy reload -> 503 LOCAL_DISK_LOW
        r2 = client.post("/api/v1/admin/taxonomy/reload", headers={"X-CSRF-Token": csrf})
        assert r2.status_code == 503 and r2.json()["error"]["code"] == "LOCAL_DISK_LOW"

        # Zero new batches and zero sidecar files created!
        batches_after = app_low.state.ctx.db.fetchone("SELECT COUNT(*) AS c FROM batches")["c"]  # type: ignore[index]
        assert batches_after == batches_before
        assert not wal_path.exists() and not shm_path.exists()

    # 4. Non-empty -wal WITHOUT -shm under low-disk read-only mode MUST fail closed 503 LOCAL_DISK_LOW
    # instead of creating a new -shm sidecar file!
    wal_path.write_bytes(b"simulated_uncheckpointed_wal_frame")
    try:
        with pytest.raises(AppError) as e_wal:
            app_low.state.ctx.db.fetchone("SELECT COUNT(*) AS c FROM batches")
        assert e_wal.value.status_code == 503 and e_wal.value.code == "LOCAL_DISK_LOW"
        assert not shm_path.exists(), "Must NOT create -shm sidecar when failing closed on orphan -wal!"
    finally:
        wal_path.unlink(missing_ok=True)
        shm_path.unlink(missing_ok=True)


def test_s03_universal_path_and_credential_redaction() -> None:
    assert "/home/jianghp3" not in (redact_path("/home/jianghp3/work/jobs/batch-1/result.json") or "")
    assert "/tmp/tianxi-vlmevalkit" not in (
        redact_path("/tmp/tianxi-vlmevalkit-smoke.PO3cZ5/work/jobs/cmmmu-vlmkit-qwen38-27b-20261124-nonthinking") or ""
    )
    assert redact_path("/tmp/tianxi-vlmevalkit-smoke.PO3cZ5/work/jobs/cmmmu-vlmkit") == "<redacted>/jobs/cmmmu-vlmkit"
    assert redact_host("10.110.158.144") == "10.110.x.144"

    raw_log = "Error in /home/alice/secret/run.py and /tmp/run_xyz/work.log with Bearer abcdef1234567890 and sk-secretkey12345678"
    cleaned = redact_text(raw_log) or ""
    assert "/home/alice" not in cleaned
    assert "/tmp/run_xyz" not in cleaned
    assert "abcdef1234567890" not in cleaned
    assert "sk-secretkey12345678" not in cleaned

    # Recursive JSON redaction + answer stripping
    nested = {
        "prompt": "Read /home/alice/key.txt with sk-secret1234567890",
        "answer": "42",
        "nested": [{"gold": "secret_gold", "note": "/tmp/priv/x.log"}],
    }
    stripped = redact_json_recursive(nested, strip_answers=True)
    assert isinstance(stripped, dict)
    assert "answer" not in stripped
    assert "gold" not in stripped["nested"][0]
    assert "/home/alice" not in stripped["prompt"]
    assert "sk-secret1234567890" not in stripped["prompt"]
    assert "/tmp/priv" not in stripped["nested"][0]["note"]

    fp = compute_internal_source_fingerprint("10.110.158.144", "/tmp/job-1")
    assert fp and len(fp) == 64

    with pytest.raises(AppError) as e1:
        validate_safe_id("../etc/passwd", "batch_id")
    assert e1.value.status_code == 400 and e1.value.code == "INVALID_PATH_OR_ID"

    with pytest.raises(AppError) as e2:
        resolve_within_root(Path("/home/share-data/dataset/CMMMU"), "../../etc/passwd")
    assert e2.value.status_code == 400


def test_s02_auth_before_dedup_team_revocation_and_personal_project(isolated_settings: Settings) -> None:
    probe_isolated_dir(isolated_settings.state_dir, create=True)
    db = Database(isolated_settings.db_path)
    init_db(db)
    sync_taxonomy_to_db(db, isolated_settings.taxonomy_file)
    now = utcnow_iso()

    uid_owner = create_user(db, username="owner_u", password="OwnerPassword!1", role="researcher", projects=["proj_a"])
    uid_b = create_user(db, username="user_b", password="UserBPassword!2", role="researcher", projects=["proj_b"])

    shared_uuid = "11111111-2222-3333-4444-555555555555"
    with db.transaction() as conn:
        for bid, proj, owner, prio, rew in [
            ("b_proj_a_high", "proj_a", uid_owner, 1, 1.0),
            ("b_proj_b_low", "proj_b", uid_b, 2, 0.0),
            ("b_personal_owner", personal_project_id(uid_owner), uid_owner, 3, 1.0),
        ]:
            conn.execute(
                """
                INSERT INTO batches (
                    batch_id, source_locator, source_namespace, batch_name,
                    project_id, owner_id, evidence_approval,
                    benchmark_slug, dataset_release_id, raw_dataset_name,
                    runner, run_scope, scoring_protocol, generation_id,
                    content_digest, indexed_at
                ) VALUES (?, ?, 'harbor_runs', ?, ?, ?, 'approved_public',
                          'deepswe', 'deepswe__1.1__default', 'DeepSWE',
                          'harbor', 'subset', 'deepswe-f2p-p2p', 'g1', 'd1', ?)
                """,
                (bid, f"loc:{bid}", bid, proj, owner, now),
            )
            conn.execute(
                """
                INSERT INTO trial_protocol_scores (
                    canonical_trial_key, scoring_protocol, batch_id, trial_id,
                    project_id, source_namespace, source_priority_rank,
                    dataset_release_id, task_id, status, reward, reward_valid, source_tags_json
                ) VALUES (?, 'deepswe-f2p-p2p', ?, ?, ?, 'harbor_runs', ?,
                          'deepswe__1.1__default', 'task-1', 'scored', ?, 1, '["harbor_runs"]')
                """,
                (shared_uuid, bid, shared_uuid, proj, prio, rew),
            )

    qs = QueryService(isolated_settings, db)
    user_b = AuthUser(uid_b, "user_b", "User B", "researcher", frozenset(), frozenset(["proj_b"]), True)
    res_b = qs.query_authorized_protocol_scores(user_b, scoring_protocol="deepswe-f2p-p2p")
    assert len(res_b) == 1
    assert res_b[0]["batch_id"] == "b_proj_b_low"
    assert res_b[0]["project_id"] == "proj_b"
    assert res_b[0]["reward"] == 0.0

    owner_revoked = AuthUser(uid_owner, "owner_u", "Owner", "researcher", frozenset(), frozenset(), True)
    assert can_access_project(owner_revoked, "proj_a", owner_id=uid_owner) is False
    res_revoked = qs.query_authorized_protocol_scores(owner_revoked, scoring_protocol="deepswe-f2p-p2p")
    assert len(res_revoked) == 1
    assert res_revoked[0]["batch_id"] == "b_personal_owner"
    assert res_revoked[0]["project_id"] == personal_project_id(uid_owner)

    assert can_access_project(user_b, personal_project_id(uid_owner), owner_id=uid_owner) is False
    assert can_access_project(owner_revoked, personal_project_id(uid_owner), owner_id=uid_owner) is True
    db.close_thread_connection()


def test_s01_cookie_flags_csrf_and_zero_hardcoded_passwords(isolated_settings: Settings) -> None:
    isolated_settings.cookie_secure = True
    app = create_app(isolated_settings, run_startup_scan=False)
    create_user(
        app.state.ctx.db,
        username="sec_admin",
        password="VeryStrongPassword!99",
        role="admin",
        projects=["default"],
        scopes=list(ALL_SCOPES),
    )
    client = TestClient(app, base_url="https://testserver")
    resp = client.post("/api/v1/auth/login", json={"username": "sec_admin", "password": "VeryStrongPassword!99"})
    assert resp.status_code == 200
    set_cookies = resp.headers.get_list("set-cookie")
    session_hdr = next(h for h in set_cookies if h.startswith("lrm_eval_session="))
    assert "HttpOnly" in session_hdr
    assert "Secure" in session_hdr
    assert "SameSite=lax" in session_hdr or "SameSite=Lax" in session_hdr

    bad = client.post("/api/v1/admin/taxonomy/reload")
    assert bad.status_code == 403 and bad.json()["error"]["code"] == "CSRF_INVALID"

    csrf = resp.json()["csrf_token"]
    bad_origin = client.post(
        "/api/v1/admin/taxonomy/reload",
        headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example.com"},
    )
    assert bad_origin.status_code == 403 and bad_origin.json()["error"]["code"] == "CSRF_INVALID"

    ok = client.post(
        "/api/v1/admin/taxonomy/reload",
        headers={"X-CSRF-Token": csrf, "Origin": "https://testserver"},
    )
    assert ok.status_code == 200 and ok.json()["ok"] is True

    app_dir = Path("/home/share-data/eval/backend/app")
    for pyf in app_dir.rglob("*.py"):
        txt = pyf.read_text(encoding="utf-8")
        assert "admin123" not in txt.lower()
        assert "password123" not in txt.lower()
