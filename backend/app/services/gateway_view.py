from typing import Any

from app.security.redaction import redact_json_recursive


def gateway_window(
    data: dict[str, Any], *, turn_offset: int, turn_limit: int, include_tools: bool,
    redacted_reason: str | None = None, strip_answers: bool = False,
) -> dict[str, Any]:
    turns = data.get("turns") if isinstance(data.get("turns"), list) else []
    summary = {
        "model_calls": data.get("model_calls", len(turns)),
        "validated": data.get("validated"),
        "turn_count": len(turns),
        "discarded_turns": sum(1 for t in turns if isinstance(t, dict) and t.get("discarded")),
        "finish_reasons": sorted({str(t["finish_reason"]) for t in turns if isinstance(t, dict) and t.get("finish_reason")}),
    }
    if redacted_reason:
        return {"redacted": True, "redacted_reason": redacted_reason, "summary": summary, "turns": []}
    sliced = []
    for turn in turns[turn_offset : turn_offset + turn_limit]:
        if isinstance(turn, dict):
            item = dict(turn)
            if not include_tools:
                item.pop("request_tools", None)
            sliced.append(redact_json_recursive(item, strip_answers=strip_answers))
    return {"redacted": False, "summary": summary, "turn_offset": turn_offset, "turn_limit": turn_limit, "turns": sliced}
