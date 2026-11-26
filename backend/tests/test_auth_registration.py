from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from app.api.errors import AppError
from app.main import create_app
from app.security.auth import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, verify_password


@pytest.fixture()
def registration_app(isolated_settings):
    isolated_settings.config_dir = Path(__file__).resolve().parents[2] / "config"
    return create_app(isolated_settings, run_startup_scan=False)


def test_registration_starts_viewer_session_and_can_log_in_again(registration_app):
    with TestClient(registration_app) as client:
        password = " 测试口令-123 "
        credentials = {"username": "  新评测用户 ", "password": password}
        result = client.post("/api/v1/auth/register", json=credentials, headers={"Origin": "http://testserver"})
        assert result.status_code == 201
        payload = result.json()
        user = payload["user"]
        assert user["username"] == user["display_name"] == "新评测用户"
        assert user["role"] == "viewer"
        assert user["projects"] == ["default"]
        assert set(user["scopes"]) == {"trace:read", "gateway:read", "verifier_log:read", "direct_vlm_qa:read"}
        assert result.headers["cache-control"] == "no-store"
        cookies = result.headers.get_list("set-cookie")
        assert any(cookie.startswith(SESSION_COOKIE_NAME + "=") and "HttpOnly" in cookie for cookie in cookies)
        assert client.cookies[CSRF_COOKIE_NAME] == payload["csrf_token"]
        assert client.get("/api/v1/auth/me").json()["user_id"] == user["user_id"]

        csrf = {"X-CSRF-Token": payload["csrf_token"]}
        assert client.post("/api/v1/admin/reindex", headers=csrf).status_code == 403
        assert client.post("/api/v1/auth/logout", headers=csrf).status_code == 200
        assert client.get("/api/v1/auth/me").status_code == 401
        assert client.post("/api/v1/auth/login", json={**credentials, "password": password.strip()}).status_code == 401
        logged_in = client.post("/api/v1/auth/login", json=credentials)
        assert logged_in.status_code == 200 and logged_in.json()["user"] == user

    db = registration_app.state.ctx.db
    saved = db.fetchone("SELECT password_hash, is_active FROM users WHERE user_id = ?", (user["user_id"],))
    assert saved["password_hash"].startswith("$argon2id$")
    assert verify_password(saved["password_hash"], password) and saved["is_active"] == 1


@pytest.mark.parametrize("credentials", [
    {"username": " ", "password": "successful-length-123"},
    {"username": "invalid", "password": "secret"},
    {"username": "invalid", "password": "successful-length-123", "role": "admin"},
    {"username": "invalid", "password": "successful-length-123", "projects": ["private"]},
    {"username": "invalid", "password": "successful-length-123", "scopes": ["dataset:holdout_read"]},
])
def test_registration_rejects_invalid_inputs_without_returning_password(registration_app, credentials):
    with TestClient(registration_app) as client:
        result = client.post("/api/v1/auth/register", json=credentials)
    assert result.status_code == 422 and result.json()["error"]["code"] == "INVALID_REGISTRATION"
    assert credentials["password"] not in result.text
    assert "set-cookie" not in result.headers
    assert registration_app.state.ctx.db.fetchone("SELECT COUNT(*) AS n FROM users")["n"] == 0


def test_registration_checks_origin_and_database_write_guard(registration_app, monkeypatch):
    credentials = {"username": "protected", "password": "test-secret-123"}
    db = registration_app.state.ctx.db
    with TestClient(registration_app) as client:
        foreign = client.post("/api/v1/auth/register", json=credentials, headers={"Origin": "https://foreign.invalid"})
        assert foreign.status_code == 403 and foreign.json()["error"]["code"] == "CSRF_INVALID"

        def reject_write():
            raise AppError(503, "LOCAL_DISK_LOW", "test write guard")

        monkeypatch.setattr(db, "write_guard", reject_write)
        guarded = client.post("/api/v1/auth/register", json=credentials)
    assert guarded.status_code == 503 and guarded.json()["error"]["code"] == "LOCAL_DISK_LOW"
    assert "set-cookie" not in foreign.headers and "set-cookie" not in guarded.headers
    assert db.fetchone("SELECT COUNT(*) AS n FROM users")["n"] == 0


def test_concurrent_registration_creates_one_account(registration_app):
    start = Barrier(2, timeout=5)

    def submit(index):
        with TestClient(registration_app) as client:
            start.wait()
            return client.post("/api/v1/auth/register", json={
                "username": "race" if index == 0 else "  race ",
                "password": f"test-pass-{index}",
            })

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, range(2)))
    assert sorted(response.status_code for response in outcomes) == [201, 409]
    conflict = next(response for response in outcomes if response.status_code == 409)
    assert conflict.json()["error"]["code"] == "USERNAME_TAKEN" and "set-cookie" not in conflict.headers
    assert registration_app.state.ctx.db.fetchone("SELECT COUNT(*) AS n FROM users WHERE username = 'race'")["n"] == 1
