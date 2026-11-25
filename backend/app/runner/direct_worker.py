#!/usr/bin/env python3
"""Isolated Direct runner worker invoked as a separate child process by the External Runner Daemon.

Strict invariants:
- Imports ONLY the verified `managed_framework/direct.py` (`load_tasks`, `model_messages`, `score`).
- Executes REAL HTTP POST `<gateway_base_url>/chat/completions` against the server-side Gateway Profile
  using `OPENAI_API_KEY` injected strictly via sanitized child env (NEVER CLI argv).
- Writes authentic Harbor-format `result.json` into `0700` `<work_dir>/jobs/<batch_name>/<trial_id>/result.json`
  and restricted evidence into `0700` `<evidence_dir>/<trial_id>/evidence.json` (0600).
- Emits authentic `trial_finished` progress records EXCLUSIVELY over the dedicated private OS pipe
  `LRM_TRUSTED_EVENT_FD`. Child stdout/stderr is strictly redirected to `0600 runner_stdio.log` and NEVER parsed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def _load_direct_module(direct_py: Path) -> Any:
    spec = importlib.util.spec_from_file_location("managed_direct", str(direct_py))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load managed direct.py: {direct_py}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: direct_worker.py <config_json_path>", file=sys.stderr)
        return 2

    cfg_path = Path(sys.argv[1])
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    event_fd = int(os.environ["LRM_TRUSTED_EVENT_FD"])
    event_fh = os.fdopen(event_fd, "w", buffering=1, encoding="utf-8")

    cancelled = False

    def _on_term(_signum: int, _frame: Any) -> None:
        nonlocal cancelled
        cancelled = True

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    direct_py = Path(cfg["direct_py"])
    dataset_path = Path(cfg["dataset_path"])
    work_dir = Path(cfg["work_dir"])
    evidence_dir = Path(cfg["evidence_dir"])
    batch_name = str(cfg["batch_name"])
    model = str(cfg["model_route"])
    base_url = str(cfg["gateway_base_url"]).rstrip("/")
    selected_tasks: list[str] = list(cfg["tasks"])
    attempts = int(cfg.get("attempts", 1))
    timeout_sec = float(cfg.get("timeout_sec", 60.0))
    scorer_path = Path(cfg["scorer_path"]) if cfg.get("scorer_path") else None
    api_key = os.environ.get("OPENAI_API_KEY", "")

    if not api_key:
        print("ERROR: OPENAI_API_KEY is missing from isolated worker env", file=sys.stderr, flush=True)
        event_fh.close()
        return 3

    direct_mod = _load_direct_module(direct_py)
    cases = direct_mod.load_tasks(dataset_path, selected_tasks)
    for case in cases:
        direct_mod.model_messages(case, dataset_path.parent)
        if scorer_path is None and "answer" not in case:
            raise ValueError(f"Missing exact-match answer for {case['id']}")

    job_dir = work_dir / "jobs" / batch_name
    job_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(job_dir, 0o700)

    endpoint = f"{base_url}/chat/completions"
    print(f"[direct-worker] starting batch={batch_name} cases={len(cases)} attempts={attempts} endpoint={base_url}", flush=True)

    any_error = False
    for case in cases:
        if cancelled:
            break
        task_id = str(case["id"])
        task_checksum = hashlib.sha256(json.dumps(case, sort_keys=True).encode("utf-8")).hexdigest()
        messages = direct_mod.model_messages(case, dataset_path.parent)

        for att in range(1, attempts + 1):
            if cancelled:
                break
            trial_id = str(uuid4())
            t_dir = job_dir / trial_id
            t_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(t_dir, 0o700)

            started = _utcnow()
            t0 = time.monotonic()
            body: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": False,
                "temperature": 0,
            }
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            error_type: str | None = None
            error_detail: str | None = None
            reward: float | None = None
            prediction: str | None = None
            actual_model: str | None = None
            usage: dict[str, Any] | None = None
            http_status: int | None = None

            try:
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                    http_status = int(getattr(resp, "status", 200))
                    payload = json.load(resp)
                prediction = payload["choices"][0]["message"]["content"]
                if not isinstance(prediction, str):
                    raise ValueError("Expected a text completion from gateway")
                actual_model = payload.get("model")
                usage = payload.get("usage")
                # Log raw completion snippet to stdout (strictly redirected to 0600 runner_stdio.log;
                # NEVER drives progress — proves I-05 spoof immunity even when model output contains fake SSE JSON!)
                print(f"[direct-worker] task={task_id} attempt={att} completion={prediction[:300]}", flush=True)
                reward = float(direct_mod.score(scorer_path, case, prediction, timeout_sec))
            except urllib.error.HTTPError as exc:
                http_status = int(exc.code)
                error_type = f"HTTPError{exc.code}"
                error_detail = f"Gateway HTTP {exc.code}"
                any_error = True
            except (urllib.error.URLError, TimeoutError, ValueError, IndexError, KeyError) as exc:
                error_type = (
                    "TimeoutError"
                    if isinstance(getattr(exc, "reason", exc), TimeoutError)
                    else type(exc).__name__
                )
                error_detail = str(exc)
                any_error = True
            except Exception as exc:
                error_type = type(exc).__name__
                error_detail = str(exc)
                any_error = True

            elapsed = round(max(0.001, time.monotonic() - t0), 4)
            finished = _utcnow()
            status = "scored" if (error_type is None and reward is not None) else "failed"

            result_doc = {
                "id": trial_id,
                "task_name": task_id,
                "task_checksum": task_checksum,
                "started_at": started,
                "finished_at": finished,
                "agent_info": {"name": "direct", "version": "native-direct-v1", "model_info": {"name": actual_model or model}},
                "verifier_result": {"rewards": {"reward": reward}} if reward is not None else None,
                "exception_info": {"exception_type": error_type, "exception_message": error_detail} if error_type else None,
                "direct_result": {
                    "prediction": prediction,
                    "reported_model": actual_model,
                    "usage": usage,
                    "http_status": http_status,
                },
                "usage": {
                    "duration_seconds": elapsed,
                    "input_tokens": (usage or {}).get("prompt_tokens") if isinstance(usage, dict) else None,
                    "output_tokens": (usage or {}).get("completion_tokens") if isinstance(usage, dict) else None,
                },
            }
            res_file = t_dir / "result.json"
            res_file.write_text(json.dumps(result_doc, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
            os.chmod(res_file, 0o600)

            # Restricted trial evidence written STRICTLY to 0700 evidence_store (NEVER shared CIFS!)
            ev_tdir = evidence_dir / trial_id
            ev_tdir.mkdir(parents=True, exist_ok=True)
            os.chmod(ev_tdir, 0o700)
            restricted_ev = {
                "trial_id": trial_id,
                "task_id": task_id,
                "task_checksum": task_checksum,
                "storage_tier": "isolated_0700_evidence_store",
                "prompt": case.get("prompt"),
                "answer": case.get("answer"),
                "response": prediction,
                "reported_model": actual_model or model,
                "http_status": http_status,
                "verifier_log": (
                    f"direct.score completed task={task_id} status={status} reward={reward}"
                    if error_type is None
                    else f"direct trial error={error_type}: {error_detail}"
                ),
                "usage": result_doc["usage"],
            }
            ev_file = ev_tdir / "evidence.json"
            ev_file.write_text(json.dumps(restricted_ev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.chmod(ev_file, 0o600)

            # Emit authentic progress record ONLY over dedicated private trusted pipe!
            event_fh.write(
                json.dumps(
                    {
                        "event_type": "trial_finished",
                        "trial_id": trial_id,
                        "task_id": task_id,
                        "task_checksum": task_checksum,
                        "attempt": att,
                        "status": status,
                        "reward": reward if status == "scored" else None,
                        "exception_type": error_type,
                        "duration_sec": elapsed,
                        "started_at": started,
                        "finished_at": finished,
                        "usage": result_doc["usage"],
                        "result_path": str(res_file),
                        "evidence_path": str(ev_file),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            event_fh.flush()

    event_fh.close()
    if cancelled:
        return 143
    return 0 if not any_error else 0  # Individual trial failures are recorded in result.json & published by Finalizer!


if __name__ == "__main__":
    raise SystemExit(main())
