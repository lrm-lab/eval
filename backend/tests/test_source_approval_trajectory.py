from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest

from app.api.errors import AppError
from app.config import Settings
from app.db.database import Database
from app.indexing.hot_indexer import SourceApprovalRegistry
from app.security.auth import AuthUser
from app.services.query_service import QueryService


@pytest.mark.parametrize("parallel_lookup", [False, True])
def test_source_approval_reloads_and_resolves_live_directory_aliases(tmp_path, parallel_lookup):
    registry_file = tmp_path / "approvals.json"
    source = tmp_path / "source"
    source.mkdir()
    first = source / "run-first"
    second = source / "run-second"
    neighbor = source / "run-second-copy"
    for batch in (first, second, neighbor):
        batch.mkdir()
    aliases = tmp_path / "aliases"
    aliases.mkdir()
    alias = aliases / "run"
    alias.symlink_to(first, target_is_directory=True)

    def rule(path, project, mode="dir", status="approved_restricted"):
        return {
            "source_root": str(path),
            "match_mode": mode,
            "project_id": project,
            "approval_status": status,
            "approved_evidence_classes": ["atif_trajectory"],
        }

    rules = [
        rule(alias, "alias"),
        rule(second, "same-length-later"),
        rule(source / "run-first" / "private", "private", status="pending"),
        rule(source / "run-first-copy", "prefix", mode="prefix"),
        # Distinct rules exercise the same path as the full production registry.
        *[rule(source / f"unused-{index}", "other") for index in range(16)],
    ]
    registry_file.write_text(json.dumps({"sources": rules}))
    registry = SourceApprovalRegistry(registry_file, parallel_lookup=parallel_lookup)
    assert registry.lookup_full(first / "traj.json")[0] == "alias"
    assert registry.lookup_full(first / "private" / "traj.json")[0] == "private"
    assert registry.lookup_full(source / "run-first-copy-2")[0] == "prefix"
    assert registry.lookup_full(neighbor / "traj.json")[2] == "pending"

    alias.unlink()
    alias.symlink_to(second, target_is_directory=True)
    assert registry.lookup_full(first / "traj.json")[2] == "pending"
    assert registry.lookup_full(second / "traj.json")[0] == "alias"
    assert registry.is_evidence_class_approved(second, "atif_trajectory", "approved_restricted")
    assert not registry.is_evidence_class_approved(second, "atif_trajectory", "revoked")

    registry_file.write_text(json.dumps({"sources": [rule(alias, "alias", status="revoked")]}))
    registry.reload()
    assert not registry.is_evidence_class_approved(second, "atif_trajectory", "approved_restricted")


@pytest.fixture()
def atif_source(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    settings = Settings(tianxi_root=tmp_path / "source", config_dir=config, runtime_dir=tmp_path / "runtime")
    root = settings.harbor_runs_dir / "run"
    root.mkdir(parents=True)
    settings.source_approval_file.write_text(json.dumps({
        "sources": [{
            "source_root": str(root),
            "approval_status": "approved_restricted",
            "approved_evidence_classes": ["atif_trajectory"],
        }, *[{"source_root": str(root.parent / f"other-{index}")} for index in range(16)]],
    }))
    service = QueryService(settings, Database(settings.db_path))
    trajectory = root / "trajectory.json"
    raw = json.dumps({"steps": [
        None,
        {"message": "unpaged"},
        {"tool_calls": ["invalid"]},
        {"tool_calls": [{"tool_call_id": "a", "function_name": "lookup"}]},
        {"tool_calls": [{"tool_call_id": "b", "function_name": "lookup"}],
         "observation": {"results": [{"source_call_id": "b", "content": "ok"}]}},
    ]}).encode()
    trajectory.write_bytes(raw)
    batch = {
        "runner": "harbor",
        "source_namespace": "harbor_runs",
        "source_locator": "harbor_runs:run",
        "evidence_approval": "approved_restricted",
        "generation_id": "gen_current",
    }
    refs = {"trajectory": {"rel_path": trajectory.name, "sha256": hashlib.sha256(raw).hexdigest()}}
    episode = {
        "generation_id": "gen_current",
        "trajectory_status": "has_tool_calls",
        "token_semantics": None,
        "evidence_refs_json": json.dumps(refs),
    }
    monkeypatch.setattr(service, "_load_batch_and_episode", lambda *args: (batch, episode))
    user = AuthUser("user", "user", "user", "researcher", frozenset({"trace:read"}), frozenset({"default"}), True)
    return service, user, batch, episode, trajectory, raw


def test_atif_paged_tools_keep_original_positions_and_verify_complete_file(tmp_path, atif_source):
    service, user, batch, episode, trajectory, raw = atif_source
    full = service.read_trial_trajectory(user, "b", "t", step_limit=1)
    assert full["total_steps"] == 4 and [step["step_id"] for step in full["steps"]] == [2]
    tools = service.read_trial_trajectory(user, "b", "t", only_tools=True, step_offset=1, step_limit=1)
    assert tools["total_steps"] == 2 and [step["step_id"] for step in tools["steps"]] == [5]
    assert tools["steps"][0]["paired_tools"][0]["observation"] == {"content": "ok", "is_error": False}

    episode["generation_id"] = "gen_stale"
    with pytest.raises(AppError) as mismatched:
        service.read_trial_trajectory(user, "b", "t")
    assert mismatched.value.status_code == 409
    episode["generation_id"] = batch["generation_id"]

    outside = tmp_path / "outside.json"
    outside.write_bytes(raw)
    trajectory.unlink()
    trajectory.symlink_to(outside)
    with pytest.raises(AppError) as escaped:
        service.read_trial_trajectory(user, "b", "t")
    assert escaped.value.code == "INVALID_PATH_OR_ID"
    trajectory.unlink()
    trajectory.write_bytes(raw.replace(b"unpaged", b"changed"))
    with pytest.raises(AppError) as tampered:
        service.read_trial_trajectory(user, "b", "t", only_tools=True, step_offset=1, step_limit=1)
    assert tampered.value.code == "GENERATION_MISMATCH"


@pytest.mark.parametrize("pause_at", ["target_path", "computed_result"])
def test_atif_reload_rejects_in_flight_authorization(atif_source, monkeypatch, pause_at):
    service, user, _, _, trajectory, _ = atif_source
    registry = service.approval_registry
    waiting = Event()
    resume = Event()
    verify = Mock(wraps=service._verify_lazy_file)
    monkeypatch.setattr(service, "_verify_lazy_file", verify)

    def pause_once():
        if not waiting.is_set():
            waiting.set()
            assert resume.wait(5)

    if pause_at == "target_path":
        original_resolve = Path.resolve

        def pause_before_resolution(path, *args, **kwargs):
            if path == trajectory.parent:
                pause_once()
            return original_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", pause_before_resolution)
    else:
        original_lookup = registry._lookup_resolved

        def pause_before_adoption(*args):
            result = original_lookup(*args)
            pause_once()
            return result

        monkeypatch.setattr(registry, "_lookup_resolved", pause_before_adoption)

    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(service.read_trial_trajectory, user, "b", "t")
        try:
            assert waiting.wait(5)
            registry.config_file.write_text('{"sources": []}')
            registry.reload()
        finally:
            resume.set()
        with pytest.raises(AppError) as in_flight:
            request.result(timeout=5)
        assert in_flight.value.status_code == 403
    with pytest.raises(AppError) as next_request:
        service.read_trial_trajectory(user, "b", "t")
    assert next_request.value.status_code == 403
    verify.assert_not_called()


def test_atif_continuous_approval_reload_fails_closed(atif_source, monkeypatch):
    service, user, *_ = atif_source
    registry = service.approval_registry
    original_lookup = registry._lookup_resolved
    verify = Mock(wraps=service._verify_lazy_file)
    monkeypatch.setattr(service, "_verify_lazy_file", verify)

    def replace_rules(*args):
        result = original_lookup(*args)
        registry.reload()
        return result

    lookup = Mock(side_effect=replace_rules)
    monkeypatch.setattr(registry, "_lookup_resolved", lookup)
    with pytest.raises(AppError) as unstable:
        service.read_trial_trajectory(user, "b", "t")
    assert (unstable.value.status_code, unstable.value.code) == (503, "SOURCE_UNAVAILABLE")
    assert lookup.call_count == 3
    verify.assert_not_called()
