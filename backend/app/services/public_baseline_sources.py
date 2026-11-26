"""First-party score feeds. Only aggregate scores and their provenance leave this module."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_HARBOR_ROWS = "https://api.harborframework.com/functions/v1/api/v1/leaderboards/rows"
_HARBOR_BOARD_ID = "60330f75-0dd8-47ea-bd1d-e2ea28945731"
_HARBOR_HEADERS = {"Harbor-API-Version": "2026-09-10.preview"}
_DEEPSWE_FEED = "https://deepswe.datacurve.ai/artifacts/v1.1/leaderboard-live.json"
_APEX_SHORTLIST_FEED = "https://matharena.ai/competition_tables/apex--shortlist_2025"
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
    sample_count: int | None
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


@dataclass(frozen=True)
class PublicTextLeader:
    benchmark_slug: str
    legacy_release_id: str
    legacy_metric: str
    label: str
    url: str
    dataset_version: str
    split: str
    metric: str
    scoring_rule: str
    row: PublicScore
    captured_on: str = "2026-11-25"
    reference_kind: str = "leaderboard"
    reference_note: str = ""
    fetch: Callable[[], tuple[list[PublicScore], str | None]] | None = None

    @property
    def id(self) -> str:
        return self.row.id.partition(":")[0]


class _MathArenaResults(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, str]] = []
        self._row: dict[str, str] | None = None
        self._field: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "tr" and "model-row" in classes:
            self._row = {"id": attributes.get("data-config") or ""}
        if tag == "td" and self._row is not None:
            self._field = next((field for field in ("rank", "model-name", "metric-bar-cell--accuracy")
                                if field in classes), None)
            if self._field == "metric-bar-cell--accuracy" and attributes.get("data-predicted") != "no":
                self._field = None

    def handle_data(self, data: str) -> None:
        if self._row is not None and self._field is not None:
            self._row[self._field] = self._row.get(self._field, "") + data

    def handle_endtag(self, tag: str) -> None:
        if tag == "td":
            self._field = None
        if tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def _fetch_apex_shortlist() -> tuple[list[PublicScore], str | None]:
    table = _read_json(_APEX_SHORTLIST_FEED)["table"]
    if not isinstance(table, str):
        raise ValueError("missing MathArena accuracy table")
    parsed = _MathArenaResults()
    parsed.feed(table)
    leaders = []
    for row in parsed.rows:
        if row.get("rank", "").strip() != "1":
            continue
        measured = re.match(r"\s*(\d+(?:\.\d+)?)\s*%", row.get("metric-bar-cell--accuracy", ""))
        if measured is None:
            continue
        name = _text(" ".join(row.get("model-name", "").replace("⚠️", "").split()))
        leaders.append(PublicScore(
            f"matharena-apex-shortlist:{_id(_text(row['id']), name)}",
            name, "", None, _percent(float(measured[1])), 47, 188,
        ))
    if not leaders:
        raise ValueError("no measured rank-one result in MathArena Apex Shortlist")
    return _ranked(leaders), None


_LONGMEMEVAL_PAPER = (
    "https://proceedings.iclr.cc/paper_files/paper/2025/file/"
    "d813d324dbf0598bbdc9c8e79740ed01-Paper-Conference.pdf#page=6"
)


def _longmemeval_reference(split: str, label: str, score: float) -> PublicTextLeader:
    return PublicTextLeader(
        "longmemeval", f"longmemeval__2026-11-20__{split}", "accuracy",
        "LongMemEval 原论文 · Figure 3b", _LONGMEMEVAL_PAPER, "2024 论文评测版", label,
        "问答准确率",
        "500 道题，由 GPT-4o 评判答案。取原论文 Figure 3b 该配置列的最高成绩。",
        PublicScore(f"longmemeval-{split}:snapshot", "GPT-4o", "", None, score, 500, 500),
        captured_on="2026-11-26", reference_kind="paper",
        reference_note="原论文对照实验最高分。站内历史题集修订和判分配置未能逐项对齐。",
    )


# Published first-party leaderboard observations and dated paper references.
# The historical CSV gives no per-configuration scoring protocol for these rows.
TEXT_LEADERS = (
    PublicTextLeader(
        "aa-lcr", "aa-lcr__1.1__default", "accuracy", "Artificial Analysis · AA-LCR v1.1",
        "https://artificialanalysis.ai/evaluations/artificial-analysis-long-context-reasoning",
        "1.1", "全量", "Pass@1",
        "100 道公开题，每题 3 次。按 v1.1 答案和等价判定评分。",
        PublicScore("aa-lcr-1.1:snapshot", "Kimi K3 (Max)", "", None, 88.7, 100, 300),
    ),
    PublicTextLeader(
        "gpqa-diamond", "gpqa-diamond__2026-11-20__default", "accuracy", "Artificial Analysis · GPQA Diamond",
        "https://artificialanalysis.ai/evaluations/gpqa-diamond",
        "GPQA Diamond 原版", "gpqa_diamond", "Pass@1",
        "198 道 Diamond 四选一题，用正则提取答案。该行计分次数未公布。",
        PublicScore("gpqa-diamond:snapshot", "GPT-6 Astra (Xhigh)", "", None, 96.3, 198, None),
    ),
    PublicTextLeader(
        "ifbench", "ifbench__2026-11-20__default", "score", "Artificial Analysis · IFBench",
        "https://artificialanalysis.ai/evaluations/ifbench",
        "IFBench 单轮 294 题版", "test", "提示级宽松准确率",
        "294 道单轮题，每题 5 次。使用官方宽松校验。",
        PublicScore("ifbench:snapshot", "Grok 4.3 (Medium)", "", None, 83.3, 294, 1470),
    ),
    PublicTextLeader(
        "hle", "hle__2026-11-20__default", "accuracy", "Artificial Analysis · HLE 纯文本",
        "https://artificialanalysis.ai/evaluations/humanitys-last-exam",
        "2025-05", "纯文本 test", "Pass@1",
        "2025 年 5 月版的 2,158 道纯文本题，按答案等价性判定。",
        PublicScore("hle-text:snapshot", "Claude Fable 5.2 (Max, Default Fallback)", "", None, 74.3, 2158, 2158),
    ),
    PublicTextLeader(
        "ifeval", "ifeval__2026-11-20__default", "Prompt-level-strict-accuracy", "FrozeBench · IFEval 开放权重榜",
        "https://www.frozebench.com/benchmarks/ifeval",
        "Google IFEval 2023", "train", "提示级严格准确率",
        "单轮 541 条提示，每条提示全部约束通过才记为正确。",
        PublicScore("ifeval-strict:snapshot", "google/gemma-4-31B-it", "lm-evaluation-harness", None, 91.3, 541, 541),
    ),
    PublicTextLeader(
        "apex-shortlist", "apex-shortlist__2026-11-20__default", "accuracy", "MathArena · Apex Shortlist",
        "https://matharena.ai/?comp=apex--shortlist_2025", "Apex Shortlist 2025", "全量", "准确率",
        "MathArena 公布的实测准确率。47 道题，每题 4 次。",
        PublicScore("matharena-apex-shortlist:snapshot", "GPT-5.5", "", "xhigh", 98.4, 47, 188),
        captured_on="2026-11-26",
        reference_note="MathArena 榜单另行标记题集公开后发布的模型。",
        fetch=_fetch_apex_shortlist,
    ),
    _longmemeval_reference("oracle-direct", "Oracle · Direct", 87.0),
    _longmemeval_reference("oracle-con", "Oracle · CoN", 92.4),
    _longmemeval_reference("s-direct", "S · Direct", 60.6),
    _longmemeval_reference("s-con", "S · CoN", 64.0),
    PublicTextLeader(
        "multi-challenge", "multi-challenge__2026-11-20__default", "overall-score", "Scale Labs · MultiChallenge",
        "https://labs.scale.com/leaderboard/multichallenge", "2026 榜单修订版", "Large", "Overall score",
        "Scale Labs Large 榜按置信区间排名，基于按题判定规则的自动裁判。",
        PublicScore("scale-multichallenge-large:snapshot", "Gemini 4 Carbon", "one-pass-agent", "high", 85.90, 266, None),
        captured_on="2026-11-26",
        reference_note="266 为当前公开 test 题数。站内历史分与公榜修订后的题目和裁判口径尚未对齐。",
    ),
    PublicTextLeader(
        "multi-lcb", "multi-lcb__2026-11-20__default", "pass@1", "Multi-LCB 官方榜 · 12 种语言",
        "https://multi-lcb.github.io/#leaderboard", "Multi-LCB", "2025-01-26 至 2025-04-06",
        "12 种语言平均 Pass@1",
        "取公开榜默认时间窗口中覆盖全部 12 种语言的最高分。榜首配置覆盖窗口内 131/134 道题。",
        PublicScore("multi-lcb-12-languages:snapshot", "GPT-OSS-120B", "", "High", 81.9, 131, None),
        captured_on="2026-11-26",
        reference_note="公开榜全语言组第 1，公开榜总榜第 1 仅测 Python。站内历史题目时间窗口尚未对齐。",
    ),
)
