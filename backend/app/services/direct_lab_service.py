from __future__ import annotations

import base64
import hashlib
import json
import math
import mimetypes
import os
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import yaml

from app.api.errors import AppError
from app.config import Settings
from app.db.database import Database
from app.db.migrations import utcnow_iso
from app.security.auth import AuthUser
from app.security.authz import (
    has_scope,
    require_project_access,
    require_role,
    sql_project_filter,
)
from app.security.gateways import GatewayProfileRegistry
from app.security.managed_exec import build_sanitized_subprocess_env, resolve_verified_managed_script
from app.security.redaction import redact_json_recursive, redact_text
from app.security.storage_probe import probe_isolated_dir, resolve_within_root, validate_safe_id
from app.services.methodology import METHODOLOGY_VERSION, compute_unrounded_delta, finite

SUPPORTED_IMAGE_MIMES: frozenset[str] = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
FORBIDDEN_NON_NATIVE_CASE_KEYS: frozenset[str] = frozenset({"messages", "expected", "expected_output", "chat_messages"})
EXTENDED_ADAPTER_PARAMS: tuple[str, ...] = ("system_prompt", "thinking", "temperature", "max_tokens", "chat_template_kwargs")

HttpPostJsonFn = Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]]


def _default_http_post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout_sec: float) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("Gateway response must be a JSON object")
        return parsed


def _detect_image_magic_mime(data: bytes) -> str | None:
    if len(data) >= 8 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 3 and data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 6 and (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _parse_numeric(text: str) -> float | None:
    s = text.strip().replace(",", "")
    if not s:
        return None
    try:
        v = float(s)
        return v if math.isfinite(v) else None
    except ValueError:
        return None


def _run_builtin_scorer(rule: str, case: dict[str, Any], prediction: str) -> float:
    ans = str(case.get("answer", "")).strip()
    pred = prediction.strip()
    if rule == "exact_match":
        return float(pred == ans)
    if rule == "case_insensitive_exact":
        return float(pred.lower() == ans.lower())
    if rule == "numeric_tolerance_1e3":
        vp = _parse_numeric(pred)
        va = _parse_numeric(ans)
        if vp is None or va is None:
            return 0.0
        return float(abs(vp - va) <= 1e-3)
    if rule == "substring_contains":
        return float(bool(ans) and ans.lower() in pred.lower())
    raise AppError(400, "INVALID_PATH_OR_ID", f"未登记的评分器规则: {rule}")


class DirectLabService:
    """M2 Direct Native Validation Workbench (v2.2.1 · 02-6 §2, E-02, S-04, S-06).
    - Strictly enforces native `direct.py:33-37` schema (`id`, string `prompt`, optional relative `images`,
      conditional mandatory `answer`, rejecting non-native `messages`/`expected`).
    - Server-side allowlisted scorers ONLY (`config/direct_scorers.yaml`); ZERO arbitrary user code execution.
    - Server-side Gateway Profiles ONLY (`0600 gateways.env` / env); ZERO raw API key submission or storage.
    - Fail-Closed `0700` staging directory for uploaded cases and trial receipts.
    - Immutable `cases_sha256` + `scorer_sha256` lock for same-dataset A/B comparison, verified via `report.py`."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        gateway_profiles: GatewayProfileRegistry | None = None,
        http_post_fn: HttpPostJsonFn | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.gateway_profiles = gateway_profiles or GatewayProfileRegistry(settings)
        self._http_post_fn = http_post_fn or _default_http_post_json
        self._scorers: list[dict[str, Any]] = []
        self.reload_scorers()

    def reload_scorers(self) -> None:
        p = self.settings.direct_scorers_file
        if p.is_file():
            cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            ver = str(cfg.get("version") or "v1")
            out: list[dict[str, Any]] = []
            for item in cfg.get("scorers") or []:
                if not isinstance(item, dict) or not item.get("scorer_id"):
                    continue
                sid = str(item["scorer_id"]).strip()
                rule = str(item.get("builtin_rule") or sid)
                sig_payload = json.dumps(
                    {"version": ver, "scorer_id": sid, "builtin_rule": rule, "requires_answer": bool(item.get("requires_answer", True))},
                    sort_keys=True,
                ).encode("utf-8")
                out.append(
                    {
                        "scorer_id": sid,
                        "display_name": str(item.get("display_name") or sid),
                        "description": str(item.get("description") or ""),
                        "requires_answer": bool(item.get("requires_answer", True)),
                        "builtin_rule": rule,
                        "scorer_sha256": hashlib.sha256(sig_payload).hexdigest(),
                    }
                )
            self._scorers = out
        else:
            sig = hashlib.sha256(b'{"builtin_rule":"exact_match","requires_answer":true,"scorer_id":"exact_match","version":"v1"}').hexdigest()
            self._scorers = [
                {
                    "scorer_id": "exact_match",
                    "display_name": "Native Exact Match (direct.py:58-59)",
                    "description": "原生 direct.py 精确字符串匹配",
                    "requires_answer": True,
                    "builtin_rule": "exact_match",
                    "scorer_sha256": sig,
                }
            ]

    def _get_scorer(self, scorer_id: str) -> dict[str, Any]:
        sid = (scorer_id or "exact_match").strip()
        for sc in self._scorers:
            if sc["scorer_id"] == sid:
                return sc
        raise AppError(
            422,
            "SCHEMA_VALIDATION_FAILED",
            f"仅允许使用服务端预审白名单评分器，拒绝未登记评分器或自定义脚本: {sid}",
            {"fields": [{"field": "scorer_id", "error": f"unregistered_scorer:{sid}"}]},
        )

    def get_capabilities(self, user: AuthUser) -> dict[str, Any]:
        self.gateway_profiles.reload()
        self.reload_scorers()
        return {
            "methodology_version": METHODOLOGY_VERSION,
            "active_adapter_variant": "native_direct_v1",
            "extended_adapter_status": {
                "variant": "extended_adapter_pending",
                "enabled": False,
                "pending_params": list(EXTENDED_ADAPTER_PARAMS),
                "disclosure": (
                    "上游 framework/direct.py 暂未提供运行期 system_prompt / thinking / temperature 参数；"
                    "当前工作台严格执行原生 direct.py:33-37 契约（native_direct_v1），扩展参数已按 v2.2.1 02-6 §2.2 标记为待适配项。"
                ),
            },
            "native_case_schema": {
                "required": ["id", "prompt"],
                "conditional_required": ["answer (when exact_match or requires_answer=true)"],
                "optional": ["images (relative paths: png/jpeg/webp/gif)"],
                "forbidden_non_native": sorted(FORBIDDEN_NON_NATIVE_CASE_KEYS),
            },
            "scorers": [
                {
                    "scorer_id": s["scorer_id"],
                    "display_name": s["display_name"],
                    "description": s["description"],
                    "requires_answer": s["requires_answer"],
                    "scorer_sha256": s["scorer_sha256"],
                }
                for s in self._scorers
            ],
            "gateway_profiles": self.gateway_profiles.list_public_profiles(kind="model_gateway"),
        }

    def decode_images_b64(self, raw_images_b64: Any) -> dict[str, bytes] | None:
        if raw_images_b64 is None:
            return None
        if not isinstance(raw_images_b64, dict):
            raise AppError(
                422,
                "SCHEMA_VALIDATION_FAILED",
                "images_b64 必须为相对路径到 base64 的映射",
                {"fields": [{"field": "images_b64", "error": "images_b64_must_be_object"}]},
            )
        if not raw_images_b64:
            return {}
        images_bytes: dict[str, bytes] = {}
        total_img_bytes = 0
        for k, v in raw_images_b64.items():
            if not isinstance(k, str) or not k.strip():
                raise AppError(422, "SCHEMA_VALIDATION_FAILED", "images_b64 键名不能为空")
            b64_str = str(v or "")
            if b64_str.startswith("data:") and "," in b64_str:
                b64_str = b64_str.split(",", 1)[1]
            try:
                decoded = base64.b64decode(b64_str, validate=True)
            except Exception as exc:
                raise AppError(
                    422,
                    "SCHEMA_VALIDATION_FAILED",
                    f"图像 {k} 的 base64 编码无效",
                    {"fields": [{"field": f"images_b64.{k}", "error": "invalid_base64"}]},
                ) from exc
            total_img_bytes += len(decoded)
            if total_img_bytes > self.settings.max_upload_bytes:
                raise AppError(413, "PAYLOAD_TOO_LARGE", "上传图像总大小超过 50 MB 上限")
            images_bytes[k] = decoded
        return images_bytes

    def validate_cases_jsonl(
        self,
        cases_jsonl: str,
        *,
        scorer_id: str = "exact_match",
        images_map: dict[str, bytes] | None = None,
        assets_root: Path | None = None,
    ) -> dict[str, Any]:
        """Strict native direct.py:33-37 validator with exact field-level errors.
        Binds actual SHA-256 bytes of every referenced image into `cases_sha256` (E-02)
        and rejects any missing or invalid-magic referenced image at validate & run time!"""
        sc = self._get_scorer(scorer_id)
        if not isinstance(cases_jsonl, str) or not cases_jsonl.strip():
            raise AppError(
                422,
                "SCHEMA_VALIDATION_FAILED",
                "cases.jsonl 不能为空",
                {"fields": [{"field": "cases_jsonl", "error": "empty_cases_jsonl"}]},
            )

        raw_bytes = cases_jsonl.encode("utf-8")
        if len(raw_bytes) > self.settings.max_upload_bytes:
            raise AppError(413, "PAYLOAD_TOO_LARGE", "cases.jsonl 超过 50 MB 上限")

        field_errors: list[dict[str, Any]] = []
        cases: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        image_count = 0
        referenced_image_digests: dict[str, str] = {}

        for line_no, raw_line in enumerate(cases_jsonl.splitlines(), start=1):
            if not raw_line.strip():
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                field_errors.append({"line": line_no, "field": "$", "error": f"invalid_json: {exc.msg}"})
                continue
            if not isinstance(obj, dict):
                field_errors.append({"line": line_no, "field": "$", "error": "case_must_be_json_object"})
                continue

            # Reject non-native alternative keys explicitly (02-6 §2.1)
            for bad_k in sorted(FORBIDDEN_NON_NATIVE_CASE_KEYS & set(obj.keys())):
                field_errors.append(
                    {
                        "line": line_no,
                        "field": bad_k,
                        "error": f"non_native_field_forbidden: direct.py 仅接受原生 id/prompt/images/answer，禁止使用 {bad_k}",
                    }
                )

            # Required unique non-empty string `id`
            cid = obj.get("id")
            if not isinstance(cid, str) or not cid.strip():
                field_errors.append({"line": line_no, "field": "id", "error": "missing_or_non_string_id"})
            elif cid in seen_ids:
                field_errors.append({"line": line_no, "field": "id", "error": f"duplicate_id:{cid}"})
            else:
                seen_ids.add(cid)

            # Required non-empty string `prompt` (direct.py:34-36)
            prompt = obj.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                field_errors.append({"line": line_no, "field": "prompt", "error": "missing_or_non_string_prompt"})

            # Conditional mandatory `answer` (direct.py:58-59)
            if sc["requires_answer"]:
                if "answer" not in obj or obj["answer"] is None or (isinstance(obj["answer"], str) and not obj["answer"].strip()):
                    field_errors.append(
                        {
                            "line": line_no,
                            "field": "answer",
                            "error": f"answer_required_for_scorer:{sc['scorer_id']}",
                        }
                    )

            # Optional `images`: relative path array with strict MIME + traversal + actual bytes binding!
            images = obj.get("images", [])
            if images is None:
                images = []
            if not isinstance(images, list):
                field_errors.append({"line": line_no, "field": "images", "error": "images_must_be_list_of_relative_paths"})
            else:
                for idx, img_name in enumerate(images):
                    if not isinstance(img_name, str) or not img_name.strip():
                        field_errors.append({"line": line_no, "field": f"images[{idx}]", "error": "image_path_must_be_non_empty_string"})
                        continue
                    rel_p = Path(img_name)
                    if rel_p.is_absolute() or ".." in rel_p.parts or "\\" in img_name or "\x00" in img_name:
                        field_errors.append({"line": line_no, "field": f"images[{idx}]", "error": "image_path_must_be_relative_no_traversal"})
                        continue
                    mime = mimetypes.guess_type(rel_p.name)[0]
                    if mime not in SUPPORTED_IMAGE_MIMES:
                        field_errors.append({"line": line_no, "field": f"images[{idx}]", "error": f"unsupported_image_mime:{mime}"})
                        continue

                    img_raw: bytes | None = None
                    if images_map is not None and img_name in images_map:
                        img_raw = images_map[img_name]
                    elif assets_root is not None:
                        try:
                            resolved = resolve_within_root(assets_root, img_name)
                            if resolved.is_file():
                                img_raw = resolved.read_bytes()
                        except AppError:
                            field_errors.append({"line": line_no, "field": f"images[{idx}]", "error": "image_path_escapes_dataset_root"})
                            continue

                    if img_raw is None:
                        field_errors.append(
                            {
                                "line": line_no,
                                "field": f"images[{idx}]",
                                "error": f"image_file_missing:{img_name}",
                            }
                        )
                        continue
                    actual_magic_mime = _detect_image_magic_mime(img_raw)
                    if actual_magic_mime is None or actual_magic_mime != mime:
                        field_errors.append(
                            {
                                "line": line_no,
                                "field": f"images[{idx}]",
                                "error": f"invalid_image_bytes_or_mime_mismatch:expected={mime},actual={actual_magic_mime}",
                            }
                        )
                        continue

                    referenced_image_digests[img_name] = hashlib.sha256(img_raw).hexdigest()
                    image_count += 1

            cases.append(obj)

        if not cases and not field_errors:
            field_errors.append({"line": 0, "field": "cases_jsonl", "error": "no_cases_found"})

        if field_errors:
            raise AppError(
                422,
                "SCHEMA_VALIDATION_FAILED",
                f"Direct cases.jsonl 未通过原生 direct.py:33-37 格式校验（共 {len(field_errors)} 处错误）",
                {"fields": field_errors[:50], "total_errors": len(field_errors)},
            )

        norm_jsonl = "".join(json.dumps(c, sort_keys=True, ensure_ascii=False) + "\n" for c in cases)
        if referenced_image_digests:
            img_manifest = json.dumps(referenced_image_digests, sort_keys=True, ensure_ascii=False)
            cases_sha256 = hashlib.sha256((norm_jsonl + "\n#IMAGES\n" + img_manifest).encode("utf-8")).hexdigest()
        else:
            cases_sha256 = hashlib.sha256(norm_jsonl.encode("utf-8")).hexdigest()
        return {
            "valid": True,
            "case_count": len(cases),
            "image_count": image_count,
            "image_digests": referenced_image_digests,
            "cases_sha256": cases_sha256,
            "scorer_id": sc["scorer_id"],
            "scorer_sha256": sc["scorer_sha256"],
            "adapter_variant": "native_direct_v1",
            "cases": cases,
            "methodology_version": METHODOLOGY_VERSION,
        }

    def run_direct_batch(self, user: AuthUser, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate, stage to 0700 storage, call configured gateway via server-side profile,
        verify outputs through real `report.py`, and persist run fingerprint in `direct_lab_runs`."""
        require_role(user, "researcher")

        # Explicitly reject raw API keys or arbitrary scorer code passed by client (S-04 / S-03)
        for forbidden_secret_key in ("api_key", "openai_api_key", "bearer_token", "scorer_code", "custom_scorer_script"):
            if payload.get(forbidden_secret_key):
                raise AppError(
                    422,
                    "SCHEMA_VALIDATION_FAILED",
                    f"安全策略禁止从浏览器提交 {forbidden_secret_key}，请使用服务端白名单 Profile/评分器",
                    {"fields": [{"field": forbidden_secret_key, "error": "forbidden_client_supplied_secret_or_code"}]},
                )

        # Reject unimplemented extended adapter parameters explicitly (02-6 §2.2)
        requested_variant = str(payload.get("adapter_variant") or "native_direct_v1").strip()
        if requested_variant != "native_direct_v1":
            raise AppError(
                422,
                "SCHEMA_VALIDATION_FAILED",
                "扩展参数变体处于 extended_adapter_pending 状态，尚未接入上游 direct.py，当前仅允许 native_direct_v1",
                {"fields": [{"field": "adapter_variant", "error": "extended_adapter_pending"}]},
            )
        for ext_p in EXTENDED_ADAPTER_PARAMS:
            if payload.get(ext_p) is not None:
                raise AppError(
                    422,
                    "SCHEMA_VALIDATION_FAILED",
                    f"参数 {ext_p} 属于 extended_adapter_pending（上游 direct.py 暂未支持），请留空以保持原生 native_direct_v1 契约",
                    {"fields": [{"field": ext_p, "error": "extended_adapter_pending"}]},
                )

        project_id = str(payload.get("project_id") or "default").strip()
        require_project_access(user, project_id, owner_id=user.user_id)

        suite_id = str(payload.get("suite_id") or "direct-custom").strip()
        validate_safe_id(suite_id, "suite_id")
        scorer_id = str(payload.get("scorer_id") or "exact_match").strip()
        sc = self._get_scorer(scorer_id)
        gateway_profile_id = str(payload.get("gateway_profile_id") or "").strip()
        if not gateway_profile_id:
            raise AppError(
                422,
                "SCHEMA_VALIDATION_FAILED",
                "必须选择服务端已登记的 gateway_profile_id",
                {"fields": [{"field": "gateway_profile_id", "error": "required"}]},
            )
        validate_safe_id(gateway_profile_id, "gateway_profile_id")

        # Decode optional uploaded base64 images map {relative_path: base64_data}
        images_bytes = self.decode_images_b64(payload.get("images_b64"))

        cases_jsonl = str(payload.get("cases_jsonl") or "")
        val_res = self.validate_cases_jsonl(cases_jsonl, scorer_id=scorer_id, images_map=images_bytes)
        cases = val_res["cases"]

        # Fail-Closed 0700 staging probe BEFORE writing any staged files (S-06)!
        assert self.settings.staging_dir is not None
        probe_isolated_dir(self.settings.staging_dir, create=True)

        # Resolve gateway profile + secret via Fail-Closed 0600 gateways.env / env
        self.gateway_profiles.reload()
        prof, api_key = self.gateway_profiles.resolve_runtime_credentials(gateway_profile_id)
        model_route = str(payload.get("model_route") or prof.get("default_model") or "").strip()
        if not model_route:
            raise AppError(422, "SCHEMA_VALIDATION_FAILED", "必须指定 model_route")

        run_id = f"dlab_{secrets.token_hex(6)}"
        run_root = self.settings.staging_dir / "direct-lab" / run_id
        run_root.mkdir(parents=True, mode=0o700, exist_ok=False)
        os.chmod(run_root, 0o700)

        cases_path = run_root / "cases.jsonl"
        cases_path.write_text(cases_jsonl, encoding="utf-8")
        os.chmod(cases_path, 0o600)
        if images_bytes:
            for rel_img, bdata in images_bytes.items():
                target_img = resolve_within_root(run_root, rel_img)
                target_img.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                target_img.write_bytes(bdata)
                os.chmod(target_img, 0o600)

        # Execute each case strictly following direct.py:77-117
        endpoint = str(prof["base_url"]).rstrip("/") + "/chat/completions"
        timeout_sec = float(payload.get("timeout_sec") or 60.0)
        timeout_sec = max(1.0, min(timeout_sec, 300.0))

        batch_out = run_root / "published"
        (batch_out / "trials").mkdir(parents=True, mode=0o700, exist_ok=True)
        manifest_doc = {
            "schema_version": 1,
            "dataset": suite_id,
            "harness": "direct",
            "model": model_route,
            "batch": run_id,
            "runner": "direct",
        }
        (batch_out / "manifest.json").write_text(json.dumps(manifest_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        episode_rows: list[dict[str, Any]] = []
        case_results: list[dict[str, Any]] = []
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        for case in cases:
            trial_id = str(uuid4())
            started_at = datetime.now(timezone.utc).isoformat()
            # Build exact direct.py:33-54 message payload
            imgs = case.get("images") or []
            if not imgs:
                messages = [{"role": "user", "content": case["prompt"]}]
            else:
                content: list[dict[str, Any]] = [{"type": "text", "text": case["prompt"]}]
                for img_rel in imgs:
                    img_file = resolve_within_root(run_root, img_rel)
                    mime = mimetypes.guess_type(img_file.name)[0] or "image/png"
                    b64 = base64.b64encode(img_file.read_bytes()).decode("ascii")
                    content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                messages = [{"role": "user", "content": content}]

            req_body = {"model": model_route, "messages": messages, "stream": False, "temperature": 0}
            error_type: str | None = None
            error_msg: str | None = None
            reward: float | None = None
            prediction: str | None = None
            actual_model: str | None = None
            usage: dict[str, Any] | None = None

            try:
                resp = self._http_post_fn(endpoint, req_body, headers, timeout_sec)
                choices = resp.get("choices") or []
                msg_obj = (choices[0] or {}).get("message") or {}
                prediction = msg_obj.get("content")
                if not isinstance(prediction, str):
                    raise ValueError("Expected a text completion")
                actual_model = resp.get("model")
                usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else None
                reward = _run_builtin_scorer(sc["builtin_rule"], case, prediction)
            except urllib.error.HTTPError as exc:
                error_type = f"HTTPError{exc.code}"
                error_msg = f"HTTP {exc.code}"
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError, OSError) as exc:
                error_type = type(exc).__name__
                error_msg = redact_text(str(exc))

            finished_at = datetime.now(timezone.utc).isoformat()
            t0 = datetime.fromisoformat(started_at)
            t1 = datetime.fromisoformat(finished_at)
            dur = max(0.0, (t1 - t0).total_seconds())

            status = "failed" if error_type else ("scored" if reward is not None and finite(reward) else "unscored")
            if error_type:
                reward = None

            ep_usage: dict[str, Any] = {"duration_seconds": round(dur, 4)}
            if usage:
                if finite(usage.get("prompt_tokens")):
                    ep_usage["input_tokens"] = int(usage["prompt_tokens"])
                if finite(usage.get("completion_tokens")):
                    ep_usage["output_tokens"] = int(usage["completion_tokens"])

            trial_dir = batch_out / "trials" / trial_id
            trial_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            evidence = {
                "id": trial_id,
                "task_name": case["id"],
                "task_checksum": hashlib.sha256(json.dumps(case, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
                "agent_info": {"name": "direct", "model_info": {"name": actual_model}},
                "verifier_rewards": {"reward": reward} if reward is not None else {},
                "exception_type": error_type,
                "failure": {"kind": "model_gateway_http_error", "summary": error_msg} if error_type else None,
                "started_at": started_at,
                "finished_at": finished_at,
                "usage": ep_usage,
                "direct_result": {
                    "prompt": redact_text(case["prompt"]),
                    "answer": redact_text(str(case["answer"])) if "answer" in case and case["answer"] is not None else None,
                    "prediction": redact_text(prediction) if prediction is not None else None,
                    "reported_model": actual_model,
                    "usage": usage,
                },
            }
            (trial_dir / "result.json").write_text(json.dumps(evidence, ensure_ascii=False) + "\n", encoding="utf-8")

            episode_rows.append(
                {
                    "schema_version": 1,
                    "trial_id": trial_id,
                    "dataset": suite_id,
                    "task_id": case["id"],
                    "task_checksum": evidence["task_checksum"],
                    "harness": "direct",
                    "runner": "direct",
                    "model_route": model_route,
                    "status": status,
                    "reward": reward,
                    "verifier_rewards": evidence["verifier_rewards"],
                    "exception_type": error_type,
                    "failure": evidence["failure"],
                    "usage": ep_usage,
                    "result": f"trials/{trial_id}/result.json",
                    "trajectory": None,
                    "trajectory_status": "direct_response",
                    "gateway_trace": None,
                    "gateway_status": "not_captured",
                }
            )
            case_results.append(
                {
                    "task_id": case["id"],
                    "trial_id": trial_id,
                    "status": status,
                    "reward": reward,
                    "exception_type": error_type,
                    "duration_sec": round(dur, 4),
                    "prompt": redact_text(case["prompt"]),
                    "answer": redact_text(str(case["answer"])) if "answer" in case and case["answer"] is not None else None,
                    "prediction": redact_text(prediction) if prediction is not None else None,
                }
            )

        (batch_out / "episodes.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in episode_rows),
            encoding="utf-8",
        )
        (batch_out / "batch-status.json").write_text(
            json.dumps(
                {
                    "status": "finished",
                    "planned": len(cases),
                    "completed": len(episode_rows),
                    "task_attempts": {c["id"]: 1 for c in cases},
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        # Verify output strictly via pinned managed report.py with scrubbed environment (S-04)!
        report_py = resolve_verified_managed_script(self.settings, "report.py")
        proc = subprocess.run(
            [sys.executable, "-B", str(report_py), str(batch_out), "--format", "json"],
            env=build_sanitized_subprocess_env(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            report_summary = json.loads(proc.stdout)
            report_verified = True
        else:
            raise AppError(
                500,
                "INTERNAL_ERROR",
                f"Direct 运行产物未通过 report.py 校验: {redact_text(proc.stderr or proc.stdout)}",
            )

        totals = report_summary.get("totals") or {}
        summary_doc = {
            "report_py_verified": report_verified,
            "case_count": len(cases),
            "planned": totals.get("planned", len(cases)),
            "recorded": totals.get("recorded", len(episode_rows)),
            "scored": totals.get("scored", 0),
            "coverage": totals.get("coverage"),
            "valid_mean": totals.get("valid_mean"),
            "passed": totals.get("passed", 0),
            "outcomes": totals.get("outcomes") or {},
            "cases_file_ref": f"direct-lab/{run_id}/cases.jsonl",
            "results_file_ref": f"direct-lab/{run_id}/case_results.json",
        }
        (run_root / "case_results.json").write_text(json.dumps(case_results, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(run_root / "case_results.json", 0o600)

        now = utcnow_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO direct_lab_runs (
                    run_id, project_id, owner_id, suite_id, cases_sha256, scorer_id, scorer_sha256,
                    adapter_variant, model_route, gateway_profile, params_json, summary_json, batch_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'native_direct_v1', ?, ?, ?, ?, NULL, ?)
                """,
                (
                    run_id,
                    project_id,
                    user.user_id,
                    suite_id,
                    val_res["cases_sha256"],
                    sc["scorer_id"],
                    sc["scorer_sha256"],
                    model_route,
                    gateway_profile_id,
                    json.dumps(
                        {
                            "timeout_sec": timeout_sec,
                            "temperature": 0,
                            "image_count": val_res["image_count"],
                            "image_digests": val_res["image_digests"],
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(summary_doc, ensure_ascii=False),
                    now,
                ),
            )

        return self.get_run_detail(user, run_id)

    def _load_authorized_run(self, user: AuthUser, run_id: str) -> dict[str, Any]:
        validate_safe_id(run_id, "run_id")
        row = self.db.fetchone("SELECT * FROM direct_lab_runs WHERE run_id = ?", (run_id,))
        if row is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"Direct 验证记录不存在: {run_id}")
        require_project_access(user, row["project_id"], owner_id=row["owner_id"])
        return row

    def _read_case_results(self, run_id: str) -> list[dict[str, Any]]:
        assert self.settings.staging_dir is not None
        target = resolve_within_root(self.settings.staging_dir, f"direct-lab/{run_id}/case_results.json")
        if not target.is_file():
            return []
        return json.loads(target.read_text(encoding="utf-8"))

    def _filter_case_for_user(self, user: AuthUser, item: dict[str, Any]) -> dict[str, Any]:
        can_qa = has_scope(user, "direct_vlm_qa:read")
        can_ans = has_scope(user, "dataset:answer_read")
        out: dict[str, Any] = {
            "task_id": item["task_id"],
            "trial_id": item["trial_id"],
            "status": item["status"],
            "reward": item["reward"],
            "exception_type": item.get("exception_type"),
            "duration_sec": item.get("duration_sec"),
        }
        if can_qa:
            out["prompt"] = redact_text(str(item.get("prompt") or ""))
            out["prediction"] = redact_text(str(item.get("prediction") or "")) if item.get("prediction") is not None else None
            if can_ans:
                out["answer"] = redact_text(str(item.get("answer") or "")) if item.get("answer") is not None else None
        else:
            out["qa_redacted"] = True
        return out

    def list_runs(self, user: AuthUser) -> dict[str, Any]:
        proj_sql, params = sql_project_filter(user, "project_id")
        rows = self.db.fetchall(
            f"""
            SELECT run_id, project_id, owner_id, suite_id, cases_sha256, scorer_id, scorer_sha256,
                   adapter_variant, model_route, gateway_profile, params_json, summary_json, batch_id, created_at
            FROM direct_lab_runs
            WHERE {proj_sql}
            ORDER BY created_at DESC
            LIMIT 100
            """,
            params,
        )
        items = []
        for r in rows:
            summary = json.loads(r.pop("summary_json") or "{}")
            summary.pop("cases_file_ref", None)
            summary.pop("results_file_ref", None)
            r["params"] = json.loads(r.pop("params_json") or "{}")
            r["summary"] = summary
            items.append(r)
        return {"items": items, "total": len(items), "methodology_version": METHODOLOGY_VERSION}

    def get_run_detail(self, user: AuthUser, run_id: str) -> dict[str, Any]:
        row = self._load_authorized_run(user, run_id)
        summary = json.loads(row.pop("summary_json") or "{}")
        summary.pop("cases_file_ref", None)
        summary.pop("results_file_ref", None)
        row["params"] = json.loads(row.pop("params_json") or "{}")
        row["summary"] = summary
        raw_cases = self._read_case_results(run_id)
        can_qa = has_scope(user, "direct_vlm_qa:read")
        row["qa_redacted"] = not can_qa
        row["items"] = [self._filter_case_for_user(user, c) for c in raw_cases]
        row["methodology_version"] = METHODOLOGY_VERSION
        return row

    def delete_run(self, user: AuthUser, run_id: str) -> dict[str, Any]:
        require_role(user, "researcher")
        self._load_authorized_run(user, run_id)
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM direct_lab_runs WHERE run_id = ?", (run_id,))
        assert self.settings.staging_dir is not None
        try:
            rdir = resolve_within_root(self.settings.staging_dir, f"direct-lab/{run_id}")
            if rdir.is_dir():
                shutil.rmtree(rdir, ignore_errors=True)
        except AppError:
            pass
        return {"ok": True, "run_id": run_id}

    def compare_runs(self, user: AuthUser, run_a_id: str, run_b_id: str) -> dict[str, Any]:
        """Same-fingerprint A/B diff strictly requiring identical cases_sha256 AND scorer_sha256 (02-6 §2.2)."""
        ra = self._load_authorized_run(user, run_a_id)
        rb = self._load_authorized_run(user, run_b_id)

        if ra["cases_sha256"] != rb["cases_sha256"]:
            raise AppError(
                400,
                "INCOMPARABLE_PROTOCOL",
                "两次 Direct 运行的题集指纹 (cases_sha256) 不一致，禁止同题 A/B 对照",
                {"run_a_cases_sha256": ra["cases_sha256"], "run_b_cases_sha256": rb["cases_sha256"]},
            )
        if ra["scorer_sha256"] != rb["scorer_sha256"]:
            raise AppError(
                400,
                "INCOMPARABLE_PROTOCOL",
                "两次 Direct 运行的评分器指纹 (scorer_sha256) 不一致，禁止同题 A/B 对照",
                {"run_a_scorer_sha256": ra["scorer_sha256"], "run_b_scorer_sha256": rb["scorer_sha256"]},
            )

        map_a = {c["task_id"]: c for c in self._read_case_results(run_a_id)}
        map_b = {c["task_id"]: c for c in self._read_case_results(run_b_id)}
        common_covered = sorted(set(map_a.keys()) & set(map_b.keys()))
        common_valid = [
            t
            for t in common_covered
            if map_a[t]["status"] == "scored"
            and map_b[t]["status"] == "scored"
            and finite(map_a[t]["reward"])
            and finite(map_b[t]["reward"])
        ]
        fault_excluded = len(common_covered) - len(common_valid)

        a_wins = both_pass = both_fail = b_wins = 0
        a_rewards: list[float] = []
        b_rewards: list[float] = []
        can_qa = has_scope(user, "direct_vlm_qa:read")
        can_ans = has_scope(user, "dataset:answer_read")
        diff_items: list[dict[str, Any]] = []

        for t in common_valid:
            ca, cb = map_a[t], map_b[t]
            va, vb = float(ca["reward"]), float(cb["reward"])
            a_rewards.append(va * 100.0)
            b_rewards.append(vb * 100.0)
            pa, pb = va >= 1.0, vb >= 1.0
            if pa and not pb:
                a_wins += 1
                kind = "a_wins"
            elif pa and pb:
                both_pass += 1
                kind = "both_pass"
            elif not pa and not pb:
                both_fail += 1
                kind = "both_fail"
            else:
                b_wins += 1
                kind = "b_wins"

            entry: dict[str, Any] = {
                "task_id": t,
                "diff_type": kind,
                "a": {"trial_id": ca["trial_id"], "status": ca["status"], "reward": va, "duration_sec": ca.get("duration_sec")},
                "b": {"trial_id": cb["trial_id"], "status": cb["status"], "reward": vb, "duration_sec": cb.get("duration_sec")},
            }
            if can_qa:
                entry["prompt"] = redact_text(str(ca.get("prompt") or ""))
                entry["a"]["prediction"] = redact_text(str(ca.get("prediction") or "")) if ca.get("prediction") is not None else None
                entry["b"]["prediction"] = redact_text(str(cb.get("prediction") or "")) if cb.get("prediction") is not None else None
                if can_ans:
                    entry["answer"] = redact_text(str(ca.get("answer") or "")) if ca.get("answer") is not None else None
            diff_items.append(entry)

        a_pct = (math.fsum(a_rewards) / len(a_rewards)) if a_rewards else None
        b_pct = (math.fsum(b_rewards) / len(b_rewards)) if b_rewards else None
        d_pp, r_pct = compute_unrounded_delta(a_pct, b_pct) if (a_pct is not None and b_pct is not None) else (None, None)

        return {
            "methodology_version": METHODOLOGY_VERSION,
            "cases_sha256": ra["cases_sha256"],
            "scorer_id": ra["scorer_id"],
            "scorer_sha256": ra["scorer_sha256"],
            "run_a": {"run_id": run_a_id, "model_route": ra["model_route"], "suite_id": ra["suite_id"], "score_pct": round(a_pct, 2) if a_pct is not None else None},
            "run_b": {"run_id": run_b_id, "model_route": rb["model_route"], "suite_id": rb["suite_id"], "score_pct": round(b_pct, 2) if b_pct is not None else None},
            "delta_pp": d_pp,
            "rel_pct": r_pct,
            "intersection": {"common_covered_tasks": len(common_covered), "common_valid_scored_tasks": len(common_valid)},
            "win_tie_loss": {
                "a_wins": a_wins,
                "both_pass": both_pass,
                "both_fail": both_fail,
                "b_wins": b_wins,
                "fault_excluded": fault_excluded,
            },
            "qa_redacted": not can_qa,
            "items": diff_items,
        }
