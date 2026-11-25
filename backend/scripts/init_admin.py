#!/usr/bin/env python3
"""CLI admin user initializer (argon2id, zero hardcoded default passwords — S-01)."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.db.database import Database
from app.db.migrations import init_db, utcnow_iso
from app.security.auth import hash_password
from app.security.authz import ALL_SCOPES
from app.security.storage_probe import check_disk_write_guard, probe_isolated_dir, probe_secret_file


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize or update an LRM Eval user with argon2id.")
    parser.add_argument("--username", required=True)
    parser.add_argument("--display-name", default="Administrator")
    parser.add_argument("--role", choices=("viewer", "researcher", "admin"), default="admin")
    parser.add_argument("--projects", default="default", help="Comma-separated authorized project IDs")
    parser.add_argument("--scopes", default="all", help="'all' or comma-separated scopes")
    parser.add_argument("--password-env", default="LRM_ADMIN_PASSWORD")
    args = parser.parse_args()

    password = os.environ.get(args.password_env)
    if not password:
        if sys.stdin.isatty():
            password = getpass.getpass(f"Password for {args.username} (>= 8 chars): ")
        else:
            print(f"Error: set {args.password_env} or run interactively.", file=sys.stderr)
            return 2

    if len(password) < 8:
        print("Error: password must be at least 8 characters.", file=sys.stderr)
        return 2

    cfg = Settings()
    assert cfg.state_dir is not None and cfg.db_path is not None and cfg.secrets_file is not None

    def _guard_for_path(target_dir: Path) -> None:
        check_disk_write_guard(
            target_dir,
            min_free_gb=cfg.root_disk_min_free_gb,
            runtime_dir=cfg.runtime_dir,
            local_budget_mb=cfg.local_budget_mb,
        )

    probe_isolated_dir(cfg.state_dir, create=True, write_guard=lambda: _guard_for_path(cfg.state_dir))
    probe_isolated_dir(cfg.db_path.parent, create=True, write_guard=lambda: _guard_for_path(cfg.db_path.parent))
    probe_secret_file(cfg.secrets_file, create_parent=True, write_guard=lambda: _guard_for_path(cfg.secrets_file.parent))

    def _guard() -> None:
        _guard_for_path(cfg.db_path.parent)

    _guard()
    db = Database(cfg.db_path, write_guard=_guard)
    init_db(db)

    scopes = list(ALL_SCOPES) if args.scopes.strip().lower() == "all" else [s.strip() for s in args.scopes.split(",") if s.strip()]
    projects = [p.strip() for p in args.projects.split(",") if p.strip()]
    pw_hash = hash_password(password)
    now = utcnow_iso()

    with db.transaction() as conn:
        existing = conn.execute("SELECT user_id FROM users WHERE username = ?", (args.username,)).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE users
                SET display_name = ?, password_hash = ?, role = ?, scopes_json = ?,
                    projects_json = ?, is_active = 1, updated_at = ?
                WHERE username = ?
                """,
                (args.display_name, pw_hash, args.role, json.dumps(scopes), json.dumps(projects), now, args.username),
            )
            uid = existing["user_id"]
            action = "updated"
        else:
            uid = f"u_{secrets.token_hex(8)}"
            conn.execute(
                """
                INSERT INTO users (
                    user_id, username, display_name, password_hash, role,
                    scopes_json, projects_json, is_active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (uid, args.username, args.display_name, pw_hash, args.role, json.dumps(scopes), json.dumps(projects), now, now),
            )
            action = "created"

    print(f"User {args.username} ({uid}) {action} with role={args.role}, projects={projects}, scopes={len(scopes)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
