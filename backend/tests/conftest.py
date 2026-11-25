from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db.database import Database
from app.db.migrations import init_db, sync_taxonomy_to_db, utcnow_iso
from app.main import create_app
from app.security.auth import hash_password
from app.security.authz import ALL_SCOPES


def create_user(
    db: Database,
    *,
    username: str,
    password: str,
    role: str = "viewer",
    projects: list[str] | None = None,
    scopes: list[str] | None = None,
    is_active: int = 1,
) -> str:
    uid = f"u_{secrets.token_hex(6)}"
    now = utcnow_iso()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO users (
                user_id, username, display_name, password_hash, role,
                scopes_json, projects_json, is_active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uid,
                username,
                username,
                hash_password(password),
                role,
                json.dumps(scopes if scopes is not None else []),
                json.dumps(projects if projects is not None else ["default"]),
                is_active,
                now,
                now,
            ),
        )
    return uid


def login_client(client: TestClient, username: str, password: str) -> str:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["csrf_token"]


@pytest.fixture()
def isolated_settings(tmp_path: Path) -> Settings:
    runtime = tmp_path / "runtime"
    state = runtime / "state"
    staging = runtime / "uploads-staging"
    evidence = runtime / "evidence-store"
    runner_work = runtime / "runner-work"
    return Settings(
        eval_root=Path("/home/share-data/eval"),
        tianxi_root=Path("/home/share-data/tianxi_benches"),
        dataset_root=Path("/home/share-data/dataset"),
        config_dir=Path("/home/share-data/eval/config"),
        runtime_dir=runtime,
        state_dir=state,
        staging_dir=staging,
        evidence_store_dir=evidence,
        runner_work_dir=runner_work,
        db_path=state / "eval_platform.db",
        secrets_file=state / "secrets.env",
        cookie_secure=False,
        enable_background_indexer=False,
    )


@pytest.fixture()
def m0_app(isolated_settings: Settings):
    app = create_app(isolated_settings, run_startup_scan=False)
    ctx = app.state.ctx
    create_user(
        ctx.db,
        username="admin_test",
        password="StrongAdminPassword!1",
        role="admin",
        projects=["default", "proj_a", "proj_b"],
        scopes=list(ALL_SCOPES),
    )
    create_user(
        ctx.db,
        username="researcher_b",
        password="StrongResearcherPass!2",
        role="researcher",
        projects=["proj_b"],
        scopes=["trace:read"],
    )
    create_user(
        ctx.db,
        username="viewer_noscope",
        password="StrongViewerPassword!3",
        role="viewer",
        projects=["default"],
        scopes=[],
    )
    return app
