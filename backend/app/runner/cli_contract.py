"""Single-source-of-truth native CLI contract for managed `framework/run.py` (S-04 / I-05).

Guarantees that `--plan` previews (`ExecutionService`) and live execution dispatches
(`app.runner.daemon`) construct 100% identical, `run.py`-compatible arguments:
- NEVER passes unsupported flags (`--evidence-store` or `--mode`).
- Passes restricted evidence store path strictly via `LRM_EVIDENCE_STORE_DIR` in child env
  (natively consumed by `publish_v2.ensure_private_evidence_store`).
- Resolves Harbor relative `../datasets/...` paths and VLMEvalKit `mode='infer'` overrides
  into a `0600` job-scoped TOML overlay `<effective_work_dir>/.resolved_harbor.toml` so
  native `run.py` handles Harbor, Harbor Gateway, and VLMEvalKit (`--all` + `infer`) cleanly
  without ever mutating shared CIFS configs.
- Preserves identical `work_dir`, `output`, `config`, and locked task arguments across
  Harbor native `--resume` invocations.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tomllib
from pathlib import Path
from typing import Any

from app.config import Settings


def _toml_val(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        escaped = (
            v.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_val(x) for x in v) + "]"
    if isinstance(v, dict):
        inner = ", ".join(f"{_toml_key(k)} = {_toml_val(val)}" for k, val in v.items())
        return "{ " + inner + " }"
    raise TypeError(f"Unsupported TOML value type: {type(v).__name__}")


def _toml_key(k: str) -> str:
    if k and all(ch.isalnum() or ch in ("_", "-") for ch in k):
        return k
    return _toml_val(k)


def _dump_toml_doc(doc: dict[str, Any]) -> str:
    lines: list[str] = []

    def _emit_table(prefix: list[str], tbl: dict[str, Any]) -> None:
        scalars: list[tuple[str, Any]] = []
        subtables: list[tuple[str, dict[str, Any]]] = []
        for k, v in tbl.items():
            if isinstance(v, dict):
                subtables.append((str(k), v))
            elif v is not None:
                scalars.append((str(k), v))
        if prefix and (scalars or not subtables):
            header = ".".join(_toml_key(p) for p in prefix)
            lines.append(f"[{header}]")
        for k, v in scalars:
            lines.append(f"{_toml_key(k)} = {_toml_val(v)}")
        if prefix and scalars:
            lines.append("")
        for k, sub in subtables:
            _emit_table([*prefix, k], sub)

    _emit_table([], doc)
    return "\n".join(lines).rstrip() + "\n"


def _write_private(path: Path, data: bytes) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
        file.write(data)


def _harbor_input_digest(spec: dict[str, Any], overlay: Path) -> bytes:
    # The native batch lock checks the resulting Harbor command and dataset. Also
    # bind the source overlay and the ordered Web selection before dispatch.
    inputs = [str(overlay), hashlib.sha256(overlay.read_bytes()).hexdigest(),
              spec["suite_id"], spec["batch_name"], spec.get("selection_mode"),
              spec.get("tasks"), spec.get("planned_tasks")]
    return hashlib.sha256(json.dumps(inputs).encode()).hexdigest().encode()


def materialize_managed_run_config(
    settings: Settings,
    spec: dict[str, Any],
    effective_work_dir: Path,
    *,
    harbor_agent_ca_bundle: str | None = None,
) -> Path:
    """Write a deterministic `0600` job-scoped TOML under `effective_work_dir/.resolved_harbor.toml`
    derived strictly from the managed `harbor.toml` (for Harbor / Harbor Gateway) or
    `datasets/multimodal/harbor.toml` (for VLMEvalKit):
    1. Resolves non-bundle relative `path` entries against `settings.framework_dir` (e.g.
       `../datasets/tianxi-synth-v1/train` -> `<tianxi_root>/datasets/tianxi-synth-v1/train`)
       so running managed `run.py` from local ext4 releases finds real datasets reliably.
    2. Applies requested `vlmevalkit_mode` (`infer` vs `all`) inside `[datasets.<suite>.vlmevalkit]`
       so native `run.py --all --config <resolved>` honors `infer` on default-`all` datasets
       like `cmmmu-val` without passing any unsupported `--mode` CLI flag."""
    kind = str(spec["kind"])
    suite_id = str(spec["suite_id"])
    out_toml = effective_work_dir / ".resolved_harbor.toml"
    lock = effective_work_dir / ".harbor-input.sha256"

    if spec.get("resume_mode") == "native":
        if (kind != "harbor" or any(p.is_symlink() or not p.is_file()
                                   or stat.S_IMODE(p.stat().st_mode) != 0o600 for p in (out_toml, lock))
                or lock.read_bytes() != _harbor_input_digest(spec, out_toml)):
            raise ValueError("Original Harbor config or locked task selection is missing or changed")
        return out_toml

    effective_work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(effective_work_dir, 0o700)
    if kind == "direct":
        cases = Path(spec["dataset_path"]).resolve(strict=True)
        suite_tbl = {"runner": "direct", "name": suite_id, "path": str(cases)}
        if spec.get("scorer_path"):
            suite_tbl["scorer"] = str(Path(spec["scorer_path"]).resolve(strict=True))
        resolved_doc = {"datasets": {suite_id: suite_tbl}}
    elif kind == "vlmevalkit":
        raw_cfg_path = spec.get("config_path") or spec.get("_config_path")
        src_toml = Path(str(raw_cfg_path)) if raw_cfg_path else (settings.datasets_dir / "multimodal" / "harbor.toml")
        if not src_toml.is_file():
            raise RuntimeError(f"VLMEvalKit harbor.toml missing: {src_toml}")
        cfg = tomllib.loads(src_toml.read_text(encoding="utf-8"))
        datasets = cfg.get("datasets") or {}
        if suite_id not in datasets or not isinstance(datasets[suite_id], dict):
            raise RuntimeError(f"VLMEvalKit suite `{suite_id}` missing in {src_toml}")
        suite_tbl = dict(datasets[suite_id])
        vlm_tbl = dict(suite_tbl.get("vlmevalkit") or {})
        req_mode = str(spec.get("vlmevalkit_mode") or "").strip().lower()
        if req_mode in ("all", "infer"):
            # Never loosen an infer-only dataset (e.g. mm-ifeval) to all
            if str(vlm_tbl.get("mode") or "all").lower() == "infer":
                vlm_tbl["mode"] = "infer"
            else:
                vlm_tbl["mode"] = req_mode
        suite_tbl["vlmevalkit"] = vlm_tbl
        # Ensure dataset path is absolute
        raw_path = str(suite_tbl.get("path") or "")
        if raw_path and not Path(raw_path).is_absolute():
            suite_tbl["path"] = str((src_toml.resolve().parent / raw_path).resolve())
        resolved_doc = {"datasets": {suite_id: suite_tbl}}
    else:
        managed_toml = settings.managed_framework_dir / "harbor.toml"
        fw_toml = settings.framework_dir / "harbor.toml"
        src_toml = (
            managed_toml
            if (settings.production_mode and managed_toml.is_file())
            else (managed_toml if managed_toml.is_file() else fw_toml)
        )
        if not src_toml.is_file():
            raise RuntimeError(f"Managed harbor.toml missing: {src_toml}")
        cfg = tomllib.loads(src_toml.read_text(encoding="utf-8"))
        datasets = cfg.get("datasets") or {}
        if suite_id not in datasets or not isinstance(datasets[suite_id], dict):
            raise RuntimeError(f"Harbor suite `{suite_id}` missing in {src_toml}")
        suite_tbl = dict(datasets[suite_id])
        path_root = str(suite_tbl.get("path_root") or ("bundle" if "handoff_profile" in suite_tbl else "config"))
        raw_path = str(suite_tbl.get("path") or "")
        if path_root != "bundle" and raw_path and not Path(raw_path).is_absolute():
            suite_tbl["path"] = str((settings.framework_dir / raw_path).resolve())
        resolved_doc = {}
        for top_key in ("bundle", "claude_code_version", "opencode_version"):
            if top_key in cfg and cfg[top_key] is not None:
                resolved_doc[top_key] = cfg[top_key]
        if kind == "harbor" and harbor_agent_ca_bundle:
            # Keep the operator's trust store in the frozen native inputs for resume.
            resolved_doc["agent_ca_bundle"] = str(Path(harbor_agent_ca_bundle).resolve(strict=True))
        resolved_doc["datasets"] = {suite_id: suite_tbl}

    _write_private(out_toml, _dump_toml_doc(resolved_doc).encode())
    if kind == "harbor":
        _write_private(lock, _harbor_input_digest(spec, out_toml))
    return out_toml


def build_managed_run_py_command(
    *,
    python_exe: str,
    run_py_path: str,
    spec: dict[str, Any],
    config_path: str | None,
    base_url: str | None,
    work_dir: str,
    output_dir: str,
    plan_only: bool = False,
    harbor_agent_ca_bundle: str | None = None,
) -> list[str]:
    """Construct the exact `managed_framework/run.py` command line for Harbor, Harbor Gateway,
    VLMEvalKit, and Direct. Strictly conforms to `run.py` argparse:
    - ZERO `--evidence-store` (passed via `LRM_EVIDENCE_STORE_DIR` env instead)
    - ZERO `--mode` (VLMEvalKit mode is encoded in `--task` or resolved TOML config)
    - Direct NEVER emits `--all` (uses `--task <id>` per `run.py:862`)."""
    kind = str(spec["kind"])
    suite_id = str(spec["suite_id"])
    cmd: list[str] = [python_exe, run_py_path, suite_id]

    if kind == "direct" and config_path is None:
        config_path = str(Path(work_dir) / ".resolved_harbor.toml")
    if config_path:
        cmd.extend(["--config", config_path])

    selection_mode = str(spec.get("selection_mode") or "all")
    tasks = [str(t) for t in (spec.get("tasks") or [])]
    if kind == "direct":
        # run.py:862 explicitly forbids --all for direct suites ("Select direct cases with --task")
        direct_tasks = tasks or [str(t) for t in (spec.get("planned_tasks") or [])]
        for t in direct_tasks:
            cmd.extend(["--task", t])
    elif selection_mode == "all":
        cmd.append("--all")
    else:
        for t in tasks:
            cmd.extend(["--task", t])

    if kind in ("harbor", "harbor_gateway"):
        if spec.get("agent"):
            cmd.extend(["--agent", str(spec["agent"])])
        if spec.get("harness_version"):
            cmd.extend(["--agent-version", str(spec["harness_version"])])
        if kind == "harbor_gateway":
            cmd.append("--gateway-capture")
        elif harbor_agent_ca_bundle:
            cmd.extend(["--agent-ca-bundle", harbor_agent_ca_bundle])
        cmd.extend(
            [
                "--attempts",
                str(int(spec.get("attempts") or 1)),
                "--pause-after",
                str(int(spec.get("pause_after") if spec.get("pause_after") is not None else 3)),
            ]
        )
        if spec.get("resume_mode") == "native" and kind == "harbor" and not plan_only:
            cmd.append("--resume")
    elif kind == "direct":
        cmd.extend(["--attempts", str(int(spec.get("attempts") or 1))])
    # Note: kind == "vlmevalkit" intentionally omits --agent, --agent-version, --attempts, --mode!

    cmd.extend(["--model", str(spec["model_route"])])
    if base_url:
        cmd.extend(["--base-url", str(base_url)])
    cmd.extend(
        [
            "--batch",
            str(spec["batch_name"]),
            "--concurrent",
            str(int(spec.get("concurrent") or 1)),
            "--work",
            work_dir,
            "--output",
            output_dir,
        ]
    )
    if plan_only:
        cmd.append("--plan")
    return cmd
