"""Verify OpenEnv receipts in the Runner's private, sealed staging batch."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from app.api.errors import AppError
from app.security.storage_probe import validate_safe_id


def _mismatch() -> AppError:
    return AppError(409, "GENERATION_MISMATCH", "网关证据与已发布试验不一致，请刷新后重试")


def _read(root: Path, path: Path, digest: dict[str, Any] | None = None) -> bytes:
    try:
        if path.resolve(strict=True) != path.absolute() or not path.is_relative_to(root) or not path.is_file():
            raise _mismatch()
        raw = path.read_bytes()
    except OSError as exc:
        raise _mismatch() from exc
    if digest is not None and (
        len(raw) != digest.get("size") or hashlib.sha256(raw).hexdigest() != digest.get("sha256")
    ):
        raise _mismatch()
    return raw


def _object(raw: bytes) -> dict[str, Any]:
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise _mismatch() from exc
    if not isinstance(doc, dict):
        raise _mismatch()
    return doc


def read_gateway_receipts(work: Path, batch_name: str) -> dict[str, dict[str, Any]]:
    validate_safe_id(batch_name, "batch_name")
    staging = work / "raw-output" / batch_name
    manifest = _object(_read(work, staging / "publish-manifest.json"))
    generation = manifest.get("generation_id")
    files = manifest.get("top_level_files")
    if (
        manifest.get("schema_version") != 2 or manifest.get("batch") != batch_name
        or not isinstance(generation, str) or not isinstance(files, dict)
        or not isinstance(files.get("trial-digests.jsonl"), dict)
    ):
        raise _mismatch()
    validate_safe_id(generation, "generation_id")
    digests = _read(work, staging / "trial-digests.jsonl", files["trial-digests.jsonl"])
    receipts = {}
    for raw in digests.splitlines():
        row = _object(raw)
        trial_id = row.get("trial_id")
        if not isinstance(trial_id, str) or trial_id in receipts or row.get("generation_id") != generation:
            raise _mismatch()
        receipts[trial_id] = row
    return receipts


def gateway_receipt(
    work: Path, batch_name: str, trial_id: str, receipts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    validate_safe_id(trial_id, "trial_id")
    receipt = _object(_read(work, work / "jobs" / batch_name / trial_id / "result.json"))
    try:
        native_id = str(UUID(receipt["id"]))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise _mismatch() from exc
    ref = receipts.get(native_id, {}).get("gateway")
    if not isinstance(ref, dict):
        raise _mismatch()
    generation = receipts[native_id]["generation_id"]
    if (
        ref.get("generation_id") != generation or ref.get("storage_tier") != "evidence_store_0700"
        or ref.get("rel_path") != f"{batch_name}/generations/{generation}/trials/{native_id}/model-gateway.json"
    ):
        raise _mismatch()
    return {
        "native_trial_id": native_id, "rel_path": ref["rel_path"],
        "sha256": ref.get("sha256"), "size": ref.get("size"),
    }


def read_gateway_capture(
    evidence: Path, batch_name: str, ref: dict[str, Any],
) -> dict[str, Any]:
    path = ref.get("rel_path")
    if not isinstance(path, str):
        raise _mismatch()
    parts = Path(path).parts
    if (
        len(parts) != 6 or parts[:2] != (batch_name, "generations") or parts[3] != "trials"
        or parts[4] != ref.get("native_trial_id") or parts[5] != "model-gateway.json"
        or any(part in ("..", ".") for part in parts)
    ):
        raise _mismatch()
    capture = _object(_read(evidence, evidence / path, ref))
    turns = capture.get("turns")
    calls = capture.get("model_calls")
    if (
        capture.get("schema_version") != 1 or capture.get("trial_id") != ref.get("native_trial_id")
        or capture.get("purpose") != "eval" or capture.get("validated") is not True
        or not isinstance(turns, list) or not turns
        or type(calls) is not int or calls < len(turns)
        or any(not isinstance(turn, dict) or not isinstance(turn.get("request_messages"), list) for turn in turns)
    ):
        raise _mismatch()
    return capture
