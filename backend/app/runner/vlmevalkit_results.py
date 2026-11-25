from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from app.security.redaction import redact_json_recursive

OFFICIAL_RESULT_FILE = "official-result.json"
VLM_METRICS_FILE = "vlmevalkit/metrics.json"
VLM_SCORING_FILES = (VLM_METRICS_FILE, OFFICIAL_RESULT_FILE)
MAX_SCORING_FILE_BYTES = 1024 * 1024


def read_vlmevalkit_results(batch_dir: Path) -> dict[str, Any]:
    """Read the aggregate scoring files emitted by the managed native publisher."""
    if batch_dir.is_symlink() or batch_dir.parent.is_symlink():
        raise ValueError("Invalid native VLMEvalKit output directory")
    batch_dir = batch_dir.resolve()
    results: dict[str, Any] = {}
    for relative in VLM_SCORING_FILES:
        path = batch_dir / relative
        if not path.exists() and not path.is_symlink():
            continue
        if not path.is_file() or path.resolve() != path:
            raise ValueError(f"Invalid native VLMEvalKit artifact {relative}")
        with path.open("rb") as stream:
            raw = stream.read(MAX_SCORING_FILE_BYTES + 1)
        if len(raw) > MAX_SCORING_FILE_BYTES:
            raise ValueError(f"Native VLMEvalKit artifact exceeds size limit: {relative}")
        results[relative] = redact_json_recursive(json.loads(raw), strip_answers=True)
    return results


def encode_vlmevalkit_results(results: Any) -> dict[str, bytes]:
    """Validate signed scoring data before adding it to the public generation."""
    if not isinstance(results, dict) or results.keys() - set(VLM_SCORING_FILES):
        raise ValueError("Invalid VLMEvalKit scoring artifacts")
    if not isinstance(results.get(VLM_METRICS_FILE), (dict, list)):
        raise ValueError("Native VLMEvalKit mode=all did not produce aggregate metrics")

    if OFFICIAL_RESULT_FILE in results:
        official = results[OFFICIAL_RESULT_FILE]
        if not isinstance(official, dict):
            raise ValueError("Invalid VLMEvalKit official result")
        score = official.get("score")
        denominator = official.get("denominator")
        if (
            type(score) not in (int, float)
            or not math.isfinite(score)
            or type(denominator) is not int
            or denominator <= 0
            or official.get("source") != VLM_METRICS_FILE
        ):
            raise ValueError("Invalid VLMEvalKit official score, denominator, or source")

    return {
        name: (json.dumps(doc, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
        for name, doc in results.items()
    }
