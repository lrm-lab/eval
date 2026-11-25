from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from fastapi.testclient import TestClient

from app.config import Settings
from app.db.migrations import utcnow_iso
from app.main import create_app
from app.security.authz import ALL_SCOPES
from app.services.telemetry_service import (
    MAX_BACKUP_BYTES,
    STREAM_SAMPLES_FILENAME,
    OpenAIStreamMeter,
    append_stream_sample,
    backup_status,
    daemon_log_rotation_status,
    job_stdio_status,
    load_job_stream_telemetry,
    measure_sse_lines,
    normalize_cgroup_telemetry,
    read_cgroup_v2_telemetry,
    runner_work_stdio_usage,
    summarize_stream_samples,
)
from tests.conftest import create_user, login_client


def _chunk(content: str | None = None, *, finish: str | None = None, usage: dict | None = None, key: str = "content") -> str:
    obj: dict = {"id": "c1", "object": "chat.completion.chunk", "choices": []}
    if content is not None or finish is not None:
        obj["choices"] = [{"index": 0, "delta": ({key: content} if content is not None else {}), "finish_reason": finish}]
    if usage is not None:
        obj["usage"] = usage
    return "data: " + json.dumps(obj)


# ----------------------------------------------------------------------------
# Streaming TTFT / TPS
# ----------------------------------------------------------------------------
def test_stream_meter_ttft_tps_with_and_without_usage() -> None:
    lines = [
        (0.10, ": keep-alive"),
        (0.15, _chunk("", key="content")),  # role-only / empty delta must not count as first token
        (0.25, _chunk("Hel")),
        (0.35, _chunk("lo")),
        (0.45, _chunk(" world")),
        (1.25, _chunk(None, finish="stop")),
        (1.25, _chunk(usage={"prompt_tokens": 5, "completion_tokens": 9})),
        (1.30, "data: [DONE]"),
        (1.40, _chunk("ignored after DONE")),
    ]
    res = measure_sse_lines(lines, started_at=0.0)
    assert res["source"] == "openai_sse_stream"
    assert res["ttft_ms"] == 250.0
    assert res["output_tokens"] == 9
    assert res["tps"] == round(8 / 0.2, 2)
    assert res["finish_reason"] == "stop"

    no_usage = measure_sse_lines([(l[0], l[1]) for l in lines if "completion_tokens" not in l[1]], started_at=0.0)
    assert no_usage["ttft_ms"] == 250.0
    assert no_usage["output_tokens"] is None and no_usage["tps"] is None

    reasoning = measure_sse_lines([(0.5, _chunk("think", key="reasoning_content")), (0.6, "data: [DONE]")], started_at=0.0)
    assert reasoning["ttft_ms"] == 500.0
    assert reasoning["tps"] is None  # single token: no decode window, never fabricated


def test_stream_meter_honest_unavailable() -> None:
    non_stream = OpenAIStreamMeter(started_at=0.0)
    non_stream.feed_text(json.dumps({"choices": [{"message": {"content": "hi"}}]}))
    r1 = non_stream.result()
    assert r1["source"] == "unavailable" and r1["ttft_ms"] is None and r1["tps"] is None

    empty = OpenAIStreamMeter(started_at=0.0)
    empty.feed_line('data: {"choices": 42}', at=1.0)
    empty.feed_text("data: not-json\n\n" + _chunk(None, finish="stop") + "\n\ndata: [DONE]\n", at=1.0)
    r2 = empty.result()
    assert r2["source"] == "unavailable" and r2["ttft_ms"] is None and r2["tps"] is None


def test_stream_meter_against_real_sse_http_server() -> None:
    class _H(BaseHTTPRequestHandler):
        def log_message(self, *_a) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            time.sleep(0.12)
            for tok in ("a", "b", "c", "d"):
                self.wfile.write((_chunk(tok) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(0.03)
            self.wfile.write((_chunk(usage={"completion_tokens": 4}) + "\n\ndata: [DONE]\n\n").encode())
            self.wfile.flush()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        req = Request(
            f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions",
            data=json.dumps({"model": "m", "stream": True, "messages": []}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        meter = OpenAIStreamMeter()
        with urlopen(req, timeout=10) as resp:
            for raw in resp:
                meter.feed_line(raw)
        res = meter.result()
    finally:
        srv.shutdown()
        srv.server_close()
    assert res["source"] == "openai_sse_stream"
    assert res["ttft_ms"] >= 100.0
    assert res["output_tokens"] == 4
    assert res["tps"] is not None and 10 < res["tps"] < 1000


def test_stream_samples_persist_and_summarize(tmp_path: Path) -> None:
    job = tmp_path / "job1"
    job.mkdir()
    empty = load_job_stream_telemetry(job)
    assert empty["source"] == "unavailable" and empty["ttft_ms"] is None and empty["sample_count"] == 0

    assert append_stream_sample(job, {"source": "unavailable", "ttft_ms": None, "tps": None}) is False
    for ttft, tps in ((100.0, 40.0), (300.0, None), (200.0, 60.0)):
        assert append_stream_sample(job, {"source": "openai_sse_stream", "ttft_ms": ttft, "tps": tps})
    with (job / STREAM_SAMPLES_FILENAME).open("a") as fh:
        fh.write("garbage\n" + json.dumps({"ttft_ms": -5}) + "\n" + json.dumps({"ttft_ms": float("nan")}) + "\n")
    assert ((job / STREAM_SAMPLES_FILENAME).stat().st_mode & 0o777) == 0o600

    s = load_job_stream_telemetry(job)
    assert s["source"] == "openai_sse_stream"
    assert s["sample_count"] == 3 and s["tps_sample_count"] == 2
    assert s["ttft_ms"] == 200.0 and s["ttft_p95_ms"] == 300.0
    assert s["tps"] == 50.0

    assert summarize_stream_samples([{"ttft_ms": True}])["source"] == "unavailable"

    link_job = tmp_path / "job_link"
    link_job.mkdir()
    os.symlink(job / STREAM_SAMPLES_FILENAME, link_job / STREAM_SAMPLES_FILENAME)
    assert load_job_stream_telemetry(link_job)["source"] == "unavailable"


# ----------------------------------------------------------------------------
# cgroup v2
# ----------------------------------------------------------------------------
def _fake_cgroup(tmp: Path, rel: str, files: dict[str, str]) -> tuple[Path, Path]:
    proc = tmp / "proc_self_cgroup"
    proc.write_text(f"0::/{rel}\n")
    root = tmp / "cg"
    d = root / rel if rel else root
    d.mkdir(parents=True, exist_ok=True)
    for k, v in files.items():
        (d / k).write_text(v)
    return proc, root


def test_cgroup_container_scope_reads_metrics(tmp_path: Path) -> None:
    proc, root = _fake_cgroup(
        tmp_path,
        "system.slice/docker-abc123abc123.scope",
        {
            "cpu.stat": "usage_usec 1234567\nuser_usec 1000\n",
            "memory.peak": "52428800\n",
            "memory.current": "41943040\n",
            "memory.max": "max\n",
            "pids.current": "7\n",
        },
    )
    t = read_cgroup_v2_telemetry(proc, root, tmp_path / "no-dockerenv")
    assert t["source"] == "cgroup_v2" and t["scope"] == "container"
    assert t["cpu_usage_usec"] == 1234567 and t["memory_peak_bytes"] == 52428800
    assert t["memory_current_bytes"] == 41943040 and t["memory_max_bytes"] is None and t["pids_current"] == 7


def test_cgroup_namespace_root_container(tmp_path: Path) -> None:
    proc, root = _fake_cgroup(tmp_path, "", {"memory.max": "786432000\n", "memory.current": "1000\n", "cpu.stat": "usage_usec 5\n"})
    marker = tmp_path / "dockerenv"
    podman_marker = tmp_path / "containerenv"
    assert read_cgroup_v2_telemetry(proc, root, marker, podman_marker)["source"] == "unavailable"
    marker.touch()
    t = read_cgroup_v2_telemetry(proc, root, marker, podman_marker)
    assert t["source"] == "cgroup_v2" and t["scope"] == "container_namespace_root" and t["memory_max_bytes"] == 786432000
    (root / "memory.max").unlink()
    assert read_cgroup_v2_telemetry(proc, root, marker, podman_marker)["source"] == "unavailable"


def test_cgroup_non_isolated_scopes_unavailable(tmp_path: Path) -> None:
    for i, rel in enumerate(("user.slice/user-1000.slice/session-3.scope", "system.slice/docker.service", "system.slice/containerd.service", "")):
        sub = tmp_path / f"c{i}"
        sub.mkdir()
        files = {"cpu.stat": "usage_usec 99\n", "memory.peak": "100\n"}
        proc, root = _fake_cgroup(sub, rel, files)
        t = read_cgroup_v2_telemetry(proc, root, sub / "no-dockerenv", sub / "no-containerenv")
        assert t["source"] == "unavailable", rel
        assert t["cpu_usage_usec"] is None and t["memory_peak_bytes"] is None
        assert "隔离的 cgroup v2" in t["note"]
    proc = tmp_path / "v1"
    proc.write_text("12:memory:/docker/abc\n")
    assert read_cgroup_v2_telemetry(proc, tmp_path, tmp_path / "x")["source"] == "unavailable"
    proc.write_text("0::/../../etc\n")
    assert read_cgroup_v2_telemetry(proc, tmp_path, tmp_path / "x")["source"] == "unavailable"
    assert read_cgroup_v2_telemetry(tmp_path / "missing", tmp_path, tmp_path / "x")["source"] == "unavailable"


def test_normalize_cgroup_telemetry() -> None:
    ok = normalize_cgroup_telemetry({"source": "cgroup_v2", "scope": "container", "cpu_usage_usec": 10, "memory_peak_bytes": -1, "pids_current": True})
    assert ok["source"] == "cgroup_v2" and ok["cpu_usage_usec"] == 10 and ok["memory_peak_bytes"] is None and ok["pids_current"] is None
    fake = normalize_cgroup_telemetry({"source": "cgroup_v2", "cpu_usage_usec": None, "memory_max_bytes": 5})
    assert fake["source"] == "unavailable" and fake["memory_max_bytes"] is None
    assert normalize_cgroup_telemetry({"source": "cgroup_v2", "scope": "host_service", "cpu_usage_usec": 10})["cpu_usage_usec"] is None
    assert normalize_cgroup_telemetry({"source": "cgroup_v2", "cpu_usage_usec": 10})["source"] == "unavailable"
    assert normalize_cgroup_telemetry(None)["source"] == "unavailable"


# ----------------------------------------------------------------------------
# Ops status: stdio caps, daemon log rotation, single backup
# ----------------------------------------------------------------------------
def test_job_stdio_and_runner_work_usage(tmp_path: Path) -> None:
    job = tmp_path / "rw" / "job_a"
    job.mkdir(parents=True)
    assert job_stdio_status(job)["present"] is False
    (job / "runner_stdio.log").write_bytes(b"x" * 1024)
    st = job_stdio_status(job)
    assert st["present"] and st["total_bytes"] == 1024 and not st["rotated"]
    assert st["per_job_cap_bytes"] == 2 * 1024 * 1024
    (job / "runner_stdio.log").write_bytes(b"x" * (1024 * 1024))
    (job / "runner_stdio.log.1").write_bytes(b"y" * (1024 * 1024))
    st2 = job_stdio_status(job)
    assert st2["rotated"] and st2["segments"] == 2
    os.symlink(job / "runner_stdio.log", tmp_path / "rw" / "evil")
    u = runner_work_stdio_usage(tmp_path / "rw")
    assert u["total_bytes"] == 2 * 1024 * 1024 and u["jobs_with_stdio"] == 1 and u["within_cap"]
    assert runner_work_stdio_usage(tmp_path / "missing")["total_bytes"] == 0
    assert runner_work_stdio_usage(job / "runner_stdio.log")["total_bytes"] is None


def test_daemon_log_rotation_status(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "backend.log").write_bytes(b"a" * 100)
    (logs / "backend.log.1").write_bytes(b"a" * 50)
    (logs / "backend.log.9").write_bytes(b"a" * 999)  # outside rotation window: ignored
    st = daemon_log_rotation_status(logs)
    assert st["cap_bytes"] == 10 * 15 * 1024 * 1024
    be = next(c for c in st["components"] if c["component"] == "backend")
    assert be["current_bytes"] == 150 and be["files_present"] == 2 and be["max_files"] == 4
    assert st["total_bytes"] == 150 and st["within_cap"]


def test_backup_status_single_zst(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert backup_status(state)["status"] == "missing"
    bdir = state / "backups"
    bdir.mkdir(parents=True)
    (bdir / ".eval_platform-x.db.zst.tmp").write_bytes(b"t")
    b1 = bdir / "eval_platform-20261125T000000Z.db.zst"
    b1.write_bytes(b"z" * 1000)
    os.utime(b1, (1_800_000_000, 1_800_000_000))
    st = backup_status(state, now=1_800_000_000 + 7200)
    assert st["status"] == "ok" and st["retained_count"] == 1 and st["latest_size_bytes"] == 1000 and st["age_hours"] == 2.0
    (bdir / "eval_platform-20261124T000000Z.db.zst").write_bytes(b"z")
    assert backup_status(state)["status"] == "multiple"
    (bdir / "eval_platform-20261124T000000Z.db.zst").unlink()
    with b1.open("r+b") as fh:
        fh.truncate(MAX_BACKUP_BYTES + 1)
    assert backup_status(state)["status"] == "oversize"
    blocked_state = tmp_path / "not_a_dir"
    blocked_state.write_text("fixture")
    assert backup_status(blocked_state)["status"] == "unavailable"


# ----------------------------------------------------------------------------
# HTTP API
# ----------------------------------------------------------------------------
def test_execution_telemetry_api_authz_and_honest_payload(isolated_settings: Settings) -> None:
    app = create_app(isolated_settings, run_startup_scan=False)
    ctx = app.state.ctx
    adm = create_user(ctx.db, username="tel_adm", password="StrongTelAdm!11", role="admin", projects=["default"], scopes=list(ALL_SCOPES))
    create_user(ctx.db, username="tel_res", password="StrongTelRes!22", role="researcher", projects=["proj_b"], scopes=[])

    jid = "job_tel_01"
    jdir = isolated_settings.runner_work_dir / jid  # type: ignore[operator]
    jdir.mkdir(parents=True)
    os.chmod(jdir, 0o700)
    (jdir / "runner_stdio.log").write_bytes(b"line\n" * 10)
    now = utcnow_iso()
    with ctx.db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO exec_jobs (
                job_id, kind, project_id, owner_id, batch_name, batch_id, status,
                params_json, command_preview, runner_id, exit_code, last_event_seq,
                log_ref, notify_json, created_at, finished_at
            ) VALUES (?, 'direct', 'default', ?, 'tel-batch', NULL, 'finished', '{}', 'cmd', 'r1', 0, 0, ?, '{}', ?, ?)
            """,
            (jid, adm, f"runner_work:{jid}/runner_stdio.log", now, now),
        )

    anon = TestClient(app)
    assert anon.get("/api/v1/execution/telemetry").status_code == 401
    assert anon.get(f"/api/v1/execution/jobs/{jid}").status_code == 401

    res = TestClient(app)
    login_client(res, "tel_res", "StrongTelRes!22")
    r = res.get("/api/v1/execution/telemetry")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ops"] is None
    assert body["stdio_policy"]["per_job_cap_bytes"] == 2 * 1024 * 1024
    assert body["web_cgroup"]["source"] in ("cgroup_v2", "unavailable")
    if body["web_cgroup"]["source"] == "unavailable":
        assert body["web_cgroup"]["cpu_usage_usec"] is None and body["web_cgroup"]["memory_peak_bytes"] is None
    assert res.get(f"/api/v1/execution/jobs/{jid}").status_code in (403, 404)
    assert res.get("/api/v1/execution/jobs/..%2Fetc").status_code in (400, 404, 422)

    adm_c = TestClient(app)
    login_client(adm_c, "tel_adm", "StrongTelAdm!11")
    ops = adm_c.get("/api/v1/execution/telemetry").json()["ops"]
    assert ops["backup"]["status"] == "missing" and ops["backup"]["latest_name"] is None
    assert ops["daemon_logs"]["cap_bytes"] == 150 * 1024 * 1024
    assert ops["job_stdio_usage"]["total_bytes"] == 50

    jt = adm_c.get(f"/api/v1/execution/jobs/{jid}")
    assert jt.status_code == 200, jt.text
    jb = jt.json()["telemetry"]
    assert jb["streaming_ttft_tps"] == {**jb["streaming_ttft_tps"], "source": "unavailable", "ttft_ms": None, "tps": None}
    assert jb["stdio"]["total_bytes"] == 50 and jb["stdio"]["rotated"] is False

    append_stream_sample(jdir, {"source": "openai_sse_stream", "ttft_ms": 321.5, "tps": 42.0})
    (jdir / "runner_stdio.log").write_bytes(b"line\n" * 201)
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO exec_job_events (job_id, seq_id, event_type, payload_json, created_at) VALUES (?, 1, 'job_started', ?, ?)",
            (jid, json.dumps({"cgroup_telemetry": {"source": "cgroup_v2", "scope": "container", "cpu_usage_usec": 8}}), now),
        )
    job = adm_c.get(f"/api/v1/execution/jobs/{jid}").json()
    jb2 = job["telemetry"]
    assert jb2["cgroup"]["cpu_usage_usec"] == 8
    assert job["stdio_log_truncated"] and len(job["redacted_stdio_logs"]) == 200
    assert jb2["streaming_ttft_tps"]["source"] == "openai_sse_stream"
    assert jb2["streaming_ttft_tps"]["ttft_ms"] == 321.5 and jb2["streaming_ttft_tps"]["tps"] == 42.0

    assert adm_c.get("/api/v1/execution/jobs/job_missing_zz").status_code == 404
    ctx.db.close_thread_connection()
