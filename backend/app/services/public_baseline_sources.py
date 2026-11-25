"""First-party score feeds. Only aggregate scores and their provenance leave this module."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_HARBOR_ROWS = "https://api.harborframework.com/functions/v1/api/v1/leaderboards/rows"
_HARBOR_BOARD_ID = "60330f75-0dd8-47ea-bd1d-e2ea28945731"
_HARBOR_HEADERS = {"Harbor-API-Version": "2026-09-10.preview"}
_DEEPSWE_FEED = "https://deepswe.datacurve.ai/artifacts/v1.1/leaderboard-live.json"
_GAIA_FEED = "https://datasets-server.huggingface.co/filter?" + urlencode({
    "dataset": "gaia-benchmark/results_public", "config": "2023", "split": "test",
    "orderby": '"score" DESC', "offset": 0, "length": 100,
})


@dataclass(frozen=True)
class PublicScore:
    id: str
    model: str | None
    harness: str
    configuration: str | None
    score_pct: float
    task_count: int
    sample_count: int
    published_at: str | None = None


@dataclass(frozen=True)
class PublicSource:
    id: str
    benchmark_slug: str
    label: str
    url: str
    dataset_version: str
    split: str
    task_count: int
    metric: str
    protocol: str
    scoring_rule: str
    release_id: str
    task_ids_sha256: str | None
    runner: str | None
    fetch: Callable[[], tuple[list[PublicScore], str | None]]


def _read_json(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "LRM-Eval/1.0", **(headers or {})})
    max_bytes = 4 * 1024 * 1024
    with urlopen(request, timeout=12) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError("score feed exceeds size limit")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("expected a score feed object")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ValueError("invalid score label")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    return _text(value) if value else None


def _date(value: Any) -> str:
    return datetime.fromisoformat(_text(value).replace("Z", "+00:00")).isoformat()


def _count(value: Any) -> int:
    if type(value) is not int or not 0 < value <= 1_000_000:
        raise ValueError("invalid score denominator")
    return value


def _percent(value: Any, *, ratio: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("invalid score")
    score = float(value) * (100 if ratio else 1)
    if not 0 <= score <= 100:
        raise ValueError("score out of range")
    return score


def _id(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:24]


def _ranked(rows: list[PublicScore]) -> list[PublicScore]:
    return sorted(rows, key=lambda row: (-row.score_pct, row.id))


def _fetch_terminal() -> tuple[list[PublicScore], str | None]:
    scores = []
    updates = []
    expected_total = None
    seen_cursors = set()
    cursor = None
    for _ in range(5):
        query = {"leaderboard_id": _HARBOR_BOARD_ID, "page_size": "100"}
        if cursor:
            query["cursor"] = cursor
        data = _read_json(f"{_HARBOR_ROWS}?{urlencode(query)}", headers=_HARBOR_HEADERS)
        total = _count(data["total"])
        if expected_total is None:
            expected_total = total
        if total != expected_total:
            raise ValueError("Harbor leaderboard changed during pagination")
        for row in data["items"]:
            if row["leaderboard_id"] != _HARBOR_BOARD_ID or row["status"] != "display":
                raise ValueError("unexpected Harbor leaderboard row")
            meta = row["metadata"]
            metrics = row["metrics"]
            scores.append(PublicScore(
                id="terminal-2.1:" + _text(row["id"]),
                model=_text(meta["model_display"]["label"]),
                harness=_text(meta["agent_display"]["label"]),
                configuration=_optional_text(meta.get("reasoning_effort")),
                score_pct=_percent(metrics["accuracy"]),
                task_count=89,
                sample_count=_count(metrics["n_trials"]),
                published_at=_date(meta["date"]),
            ))
            updates.append(_date(row["updated_at"]))
        cursor = data["next_cursor"]
        if not cursor:
            if len(scores) != expected_total:
                raise ValueError("incomplete Harbor leaderboard")
            return _ranked(scores), max(updates)
        if cursor in seen_cursors:
            raise ValueError("repeated Harbor leaderboard page")
        seen_cursors.add(_text(cursor))
    raise ValueError("Harbor leaderboard exceeded pagination limit")


def _fetch_deepswe() -> tuple[list[PublicScore], str | None]:
    data = _read_json(_DEEPSWE_FEED)
    unit = data["unit"]
    if (
        data["n_tasks_in_set"] != 113
        or not unit.startswith("pass@1 is attempt pass rate over scored rollout attempts.")
        or "Context-window failures and agent timeouts are scored failures; provider/verifier/network errors are excluded." not in unit
    ):
        raise ValueError("DeepSWE v1.1 score definition changed")
    scores = []
    for row in data["rows"]:
        if row["source"] != "deep-swe":
            raise ValueError("unexpected DeepSWE score source")
        tasks = _count(row["n_tasks_attempted"])
        samples = _count(row["n_attempted"])
        score = _percent(row["pass_at_1"], ratio=True)
        if tasks > 113 or tasks > samples or not math.isclose(score, row["n_passed"] / samples * 100, abs_tol=1e-6):
            raise ValueError("inconsistent DeepSWE Pass@1")
        scores.append(PublicScore(
            id="deepswe-1.1:" + _id(_text(row["config"])),
            model=_text(row["model"]),
            harness=_text(row["harness"]),
            configuration=_optional_text(row.get("reasoning_effort")),
            score_pct=score,
            task_count=tasks,
            sample_count=samples,
        ))
    return _ranked(scores), _date(data["generated_at"])


def _fetch_gaia() -> tuple[list[PublicScore], str | None]:
    data = _read_json(_GAIA_FEED)
    total = _count(data["num_rows_total"])
    if data["partial"] is not False or len(data["rows"]) != min(total, 100):
        raise ValueError("incomplete GAIA test results")
    scores: dict[str, PublicScore] = {}
    last_score = 100.0
    for item in data["rows"]:
        row = item["row"]
        agent = _optional_text(row["model"])
        family = _optional_text(row.get("model_family"))
        org = _optional_text(row.get("organisation"))
        date = _date(row["date"])
        score = _percent(row["score"], ratio=True)
        correct = score * 301 / 100
        if not math.isclose(correct, round(correct), abs_tol=1e-6) or score > last_score:
            raise ValueError("unexpected GAIA test score or ordering")
        last_score = score
        if agent:
            key = "gaia-test:" + _id(agent, family or "", org or "", date)
            scores.setdefault(key, PublicScore(key, family, agent, org, score, 301, 301, date))
    return _ranked(list(scores.values())), max(row.published_at for row in scores.values())


SOURCES = (
    PublicSource(
        id="terminal-2.1", benchmark_slug="terminal-bench", label="Terminal-Bench 2.1 官方榜",
        url="https://www.tbench.ai/?version=2.1", dataset_version="2.1", split="default", task_count=89,
        metric="任务解决率", protocol="terminal-bench-2.1-public-resolution-v1",
        scoring_rule="奖励大于 0 算通过，错误和取消资格的尝试计为失败",
        release_id="terminal-bench__2.1__default", runner="harbor",
        # Task names at the maintainers' 2.1 release tree, commit 12b7565679ce5006230b820666353673a133db93.
        task_ids_sha256="5c7ed05c61e6768b71bebb5ffd05d9ce07e185b3cf519b06ca2ae5d476f99638",
        fetch=_fetch_terminal,
    ),
    PublicSource(
        id="deepswe-1.1", benchmark_slug="deepswe", label="Datacurve DeepSWE v1.1 官方榜",
        url="https://deepswe.datacurve.ai/", dataset_version="1.1", split="default", task_count=113,
        metric="Pass@1", protocol="datacurve-deepswe-1.1-pass-at-1",
        scoring_rule="按已计分尝试计算，超时和上下文超限计失败，提供商、验证器及网络故障排除",
        release_id="deepswe__1.1__default", runner="pier",
        # Datacurve's official 113 task IDs. The v1.1 announcement confirms that task IDs were kept.
        task_ids_sha256="50bfc05e098cdd9c739ac8e09f957d34cd1d1ec516162e0274526704f8f74666",
        fetch=_fetch_deepswe,
    ),
    PublicSource(
        id="gaia-test", benchmark_slug="gaia", label="GAIA 官方 Test 榜",
        url="https://huggingface.co/spaces/gaia-benchmark/leaderboard", dataset_version="2023", split="test",
        task_count=301, metric="准确率", protocol="gaia-official-scorer",
        scoring_rule="提交答案由官方评分器判定，以全部 301 道 test 题为分母",
        release_id="gaia__2023__test", runner=None, task_ids_sha256=None, fetch=_fetch_gaia,
    ),
)
SOURCES_BY_ID = {source.id: source for source in SOURCES}
