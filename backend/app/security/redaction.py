from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import PurePosixPath

# Universal patterns for absolute paths (/home/*, /tmp/*, /var/tmp/*, /root/*)
_ABS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])/(?:home/[^/\s\"'<>:]+|tmp/[^/\s\"'<>:]+|var/tmp/[^/\s\"'<>:]+|root)(/[^\s\"'<>:,;]*)?"
)
_IPV4_RE = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
_SECRET_PATTERNS = [
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{12,}", re.IGNORECASE),
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{12,})\b"),
    re.compile(r"(?i)((?:api[_-]?key|secret[_-]?key|access[_-]?token|password|passwd)\s*[=:]\s*['\"]?)([^'\"\s,;]{6,})"),
    re.compile(r"\bs[0-9a-f]{24}\b"),  # OpenEnv rollout session keys
]


def redact_host(host: str | None) -> str | None:
    if not host:
        return None
    text = host.strip()
    m = _IPV4_RE.fullmatch(text)
    if m:
        return f"{m.group(1)}.{m.group(2)}.x.{m.group(4)}"
    return text


def _safe_tail(raw_path: str) -> str:
    """Keep safe relative tail segments (e.g. jobs/<batch>/...) without leaking /home/<user> or /tmp/<rand>."""
    p = PurePosixPath(raw_path)
    parts = [seg for seg in p.parts if seg not in ("/", "")]
    if not parts:
        return "<redacted>"
    # Prefer tail starting at known non-sensitive anchor directories
    for anchor in ("jobs", "harbor-runs", "trials", "datasets", "2026-11-20", "tianxi_benches", "work"):
        if anchor in parts:
            idx = parts.index(anchor)
            if anchor == "work" and idx + 1 < len(parts):
                idx += 1
            tail = "/".join(parts[idx:])
            return f"<redacted>/{tail}"
    # Otherwise keep only last 2 segments (if under /home/<user> or /tmp/<rand>, strip first 2)
    if parts[0] in ("home", "tmp") and len(parts) > 2:
        tail = "/".join(parts[2:])
        return f"<redacted>/{tail}"
    if parts[0] == "var" and len(parts) > 3 and parts[1] == "tmp":
        tail = "/".join(parts[3:])
        return f"<redacted>/{tail}"
    if parts[0] == "root" and len(parts) > 1:
        tail = "/".join(parts[1:])
        return f"<redacted>/{tail}"
    if len(parts) >= 2:
        return f"<redacted>/{'/'.join(parts[-2:])}"
    return f"<redacted>/{parts[-1]}"


def redact_path(path_value: str | None) -> str | None:
    if not path_value:
        return None
    text = str(path_value).strip()
    if not text:
        return None
    if text.startswith(("/home/", "/tmp/", "/var/tmp/", "/root/")):
        return _safe_tail(text)
    # Also redact any embedded sensitive prefix
    return _ABS_PATH_RE.sub(lambda m: _safe_tail(m.group(0)), text)


_ANSWER_KEYS = frozenset(
    {
        "answer",
        "answer_option",
        "gold",
        "gold_answer",
        "expected",
        "expected_answer",
        "ground_truth",
        "gt",
        "gt_answer",
        "gt_answer_details",
        "reference_solution",
        "reference_answer",
        "correct_answer",
        "standard_answer",
        "final_answer",
        "solution",
        "analysis",
        "grading_query",
        "judge_log",
        "judge_reasoning",
    }
)


def redact_text(text: str | None) -> str | None:
    if text is None:
        return None
    out = _ABS_PATH_RE.sub(lambda m: _safe_tail(m.group(0)), text)
    out = _SECRET_PATTERNS[0].sub(r"\1<redacted>", out)
    out = _SECRET_PATTERNS[1].sub("<redacted-key>", out)
    out = _SECRET_PATTERNS[2].sub(r"\1<redacted>", out)
    out = _SECRET_PATTERNS[3].sub("<redacted-key>", out)
    return out


def redact_json_recursive(obj: object, *, strip_answers: bool = False) -> object:
    """Recursively mask paths/credentials in all string leaves (Direct/VLM QA & legacy samples),
    and optionally strip gold/answer fields when caller lacks dataset:answer_read."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, list):
        return [redact_json_recursive(item, strip_answers=strip_answers) for item in obj]
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            if strip_answers and str(k).lower() in _ANSWER_KEYS:
                continue
            cleaned[k] = redact_json_recursive(v, strip_answers=strip_answers)
        return cleaned
    return obj


def compute_internal_source_fingerprint(host: str | None, raw_path: str | None, salt: str = "lrm-eval-src-v1") -> str | None:
    """Compute irreversible internal fingerprint from unredacted (host, raw_path).
    NEVER expose this value in any API DTO."""
    if not host and not raw_path:
        return None
    payload = f"{(host or '').strip().lower()}|{(raw_path or '').strip()}".encode("utf-8")
    return hmac.new(salt.encode("utf-8"), payload, hashlib.sha256).hexdigest()
