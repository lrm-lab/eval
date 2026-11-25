from __future__ import annotations

import base64
import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def _sniff_image_mime(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


@dataclass(frozen=True)
class MediaMatchResult:
    status: str  # unique_hit | missing | ambiguous_multi_hit | unregistered_source
    refs: list[dict[str, Any]]


class VersionedMediaMatcher:
    """Versioned multimodal media matcher (v2.2.1 02-2 §3 & A-09):
    - CMMMU: unique basename hit -> unique_hit (87/87 on 197)
    - MMMU_Pro_10c: context-aware + duplicate basename guard -> ambiguous_multi_hit (NEVER guesses!)
    - MM-IFEval: TSV id/index -> image lookup
    - Unregistered benchmarks -> unregistered_source (zero 404 broken images)
    """

    def __init__(self, rules_file: Path) -> None:
        self.rules_file = rules_file
        self.rules: dict[str, dict[str, Any]] = {}
        self.allowed_sources: dict[str, Path] = {}
        self._basename_index: dict[str, dict[str, list[str]]] = {}
        self._tsv_index: dict[str, dict[str, list[str]]] = {}
        self._loaded = False
        self.reload()

    def reload(self) -> None:
        if self.rules_file.is_file():
            cfg = yaml.safe_load(self.rules_file.read_text(encoding="utf-8")) or {}
        else:
            cfg = {}
        self.rules = cfg.get("rules", {})
        self.allowed_sources = {
            k: Path(v) for k, v in (cfg.get("allowed_media_sources") or {}).items()
        }
        self._basename_index.clear()
        self._tsv_index.clear()
        self._loaded = True

    def _ensure_basename_index(self, benchmark_key: str) -> dict[str, list[str]]:
        if benchmark_key in self._basename_index:
            return self._basename_index[benchmark_key]
        rule = self.rules.get(benchmark_key, {})
        root_dir = Path(rule.get("root_dir", ""))
        source_key = rule.get("source_key", benchmark_key)
        source_root = self.allowed_sources.get(source_key, root_dir)
        idx: dict[str, list[str]] = defaultdict(list)
        if root_dir.is_dir():
            for p in root_dir.rglob("*"):
                if p.is_file():
                    try:
                        rel = str(p.relative_to(source_root))
                    except ValueError:
                        rel = str(p.relative_to(root_dir))
                    idx[p.name.lower()].append(rel)
        # Sort for determinism
        for k in idx:
            idx[k].sort()
        self._basename_index[benchmark_key] = dict(idx)
        return self._basename_index[benchmark_key]

    def _ensure_tsv_index(self, benchmark_key: str) -> dict[str, list[str]]:
        if benchmark_key in self._tsv_index:
            return self._tsv_index[benchmark_key]
        rule = self.rules.get(benchmark_key, {})
        tsv_path = Path(rule.get("tsv_file", ""))
        id_cols = rule.get("id_columns", ["id", "index"])
        img_cols = rule.get("image_columns", ["image", "image_path"])
        idx: dict[str, list[str]] = defaultdict(list)
        if tsv_path.is_file():
            try:
                # Raise field size limit because some TSVs store large base64 strings
                csv.field_size_limit(10 * 1024 * 1024)
            except OverflowError:
                pass
            try:
                with open(tsv_path, "r", encoding="utf-8", errors="replace", newline="") as fh:
                    reader = csv.DictReader(fh, delimiter="\t")
                    for row_num, row in enumerate(reader):
                        img_vals: list[str] = []
                        for ic in img_cols:
                            val = (row.get(ic) or "").strip()
                            if val:
                                img_vals.append(val)
                        if not img_vals:
                            continue
                        keys = set()
                        for col in id_cols:
                            v = (row.get(col) or "").strip()
                            if v:
                                keys.add(v)
                        keys.add(str(row_num))
                        for k in keys:
                            for iv in img_vals:
                                if iv not in idx[k]:
                                    idx[k].append(iv)
            except OSError:
                pass
        self._tsv_index[benchmark_key] = dict(idx)
        return self._tsv_index[benchmark_key]

    def _extract_image_names(self, sample: dict[str, Any], rule: dict[str, Any]) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()

        def _add(raw: str) -> None:
            name = Path(raw.strip().strip('"').strip("'")).name
            if name and name.lower() not in seen:
                seen.add(name.lower())
                found.append(name)

        for fld in rule.get("image_fields", []):
            val = sample.get(fld)
            if isinstance(val, str) and val.strip():
                _add(val)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str) and item.strip():
                        _add(item)

        patterns = [re.compile(p) for p in rule.get("regex_patterns", [])]
        if patterns:
            texts = []
            for k in ("question", "origin_prompt", "prompt", "A", "B", "C", "D"):
                v = sample.get(k)
                if isinstance(v, str) and v:
                    texts.append(v)
            joined = "\n".join(texts)
            for pat in patterns:
                for m in pat.finditer(joined):
                    _add(m.group(1))
        return found

    def match_sample(self, benchmark_key: str, sample: dict[str, Any]) -> MediaMatchResult:
        rule = self.rules.get(benchmark_key)
        if not rule:
            return MediaMatchResult(
                status="unregistered_source",
                refs=[{"reason": "unregistered_media_source", "benchmark": benchmark_key}],
            )

        strategy = rule.get("strategy", "unique_basename")
        source_key = rule.get("source_key", benchmark_key)
        rule_ver = rule.get("rule_version", "v1")

        if strategy == "tsv_id_or_index_lookup":
            tsv_idx = self._ensure_tsv_index(benchmark_key)
            base_idx = self._ensure_basename_index(benchmark_key)
            lookup_keys = []
            for k in ("id", "index", "question_id", "sample_id"):
                if sample.get(k) is not None:
                    lookup_keys.append(str(sample[k]).strip())
            matched_vals: list[str] = []
            matched_key: str = lookup_keys[0] if lookup_keys else ""
            for lk in lookup_keys:
                if lk in tsv_idx:
                    matched_vals = tsv_idx[lk]
                    matched_key = lk
                    break
            if not matched_vals:
                return MediaMatchResult(status="missing", refs=[{"rule_version": rule_ver, "reason": "tsv_key_not_found"}])
            refs: list[dict[str, Any]] = []
            for val_idx, val in enumerate(matched_vals):
                if (
                    val.startswith(("data:image/", "/9j/", "iVBOR", "R0lGOD", "UklGR"))
                    or len(val) > 256
                ):
                    # Embedded base64 in TSV -> reference by tsv_key with direct authenticated media URL
                    refs.append({
                        "kind": "tsv_inline",
                        "source": source_key,
                        "tsv_key": matched_key,
                        "tsv_idx": val_idx,
                        "url": f"/api/v1/media/{source_key}/tsv-inline/{matched_key}?idx={val_idx}",
                        "rule_version": rule_ver,
                    })
                else:
                    bname = Path(val).name.lower()
                    hits = base_idx.get(bname, [])
                    if len(hits) == 1:
                        refs.append({
                            "kind": "file",
                            "source": source_key,
                            "rel_path": hits[0],
                            "url": f"/api/v1/media/{source_key}/{hits[0]}",
                            "rule_version": rule_ver,
                        })
                    elif len(hits) > 1:
                        return MediaMatchResult(
                            status="ambiguous_multi_hit",
                            refs=[{"rule_version": rule_ver, "basename": bname, "candidate_count": len(hits)}],
                        )
                    else:
                        # Check direct relative path under root_dir
                        root_dir = Path(rule.get("root_dir", ""))
                        cand = root_dir / val
                        if cand.is_file():
                            rel = str(cand.relative_to(root_dir))
                            refs.append({
                                "kind": "file",
                                "source": source_key,
                                "rel_path": rel,
                                "url": f"/api/v1/media/{source_key}/{rel}",
                                "rule_version": rule_ver,
                            })
                        else:
                            return MediaMatchResult(
                                status="missing",
                                refs=[{"rule_version": rule_ver, "basename": bname, "reason": "file_missing"}],
                            )
            return MediaMatchResult(status="unique_hit", refs=refs)

        # Basename / context strategies (CMMMU, MMMU_Pro_10c)
        img_names = self._extract_image_names(sample, rule)
        base_idx = self._ensure_basename_index(benchmark_key)

        # Support <image N> placeholder samples keyed by sample['id'] prefix (e.g. MMMU_Pro_10c: test_History_1 -> test_History_1_0.png)
        if not img_names and strategy == "context_then_unique_basename" and sample.get("id"):
            prefix = f"{str(sample['id']).strip().lower()}_"
            img_names = sorted(bname for bname in base_idx.keys() if bname.startswith(prefix))

        if not img_names:
            return MediaMatchResult(status="missing", refs=[{"rule_version": rule_ver, "reason": "no_image_ref_in_sample"}])
        ctx_tokens = [
            str(sample.get(cf)).strip().lower()
            for cf in rule.get("context_fields", [])
            if sample.get(cf)
        ]

        refs = []
        any_ambiguous = False
        any_missing = False
        for name in img_names:
            hits = base_idx.get(name.lower(), [])
            if len(hits) > 1 and strategy == "context_then_unique_basename" and ctx_tokens:
                narrowed = [h for h in hits if any(ct in h.lower() for ct in ctx_tokens)]
                if len(narrowed) == 1:
                    hits = narrowed
            if len(hits) == 1:
                refs.append({
                    "kind": "file",
                    "source": source_key,
                    "rel_path": hits[0],
                    "url": f"/api/v1/media/{source_key}/{hits[0]}",
                    "rule_version": rule_ver,
                })
            elif len(hits) > 1:
                any_ambiguous = True
                refs.append({
                    "kind": "ambiguous",
                    "basename": name,
                    "candidate_count": len(hits),
                    "rule_version": rule_ver,
                })
            else:
                any_missing = True
                refs.append({
                    "kind": "missing",
                    "basename": name,
                    "rule_version": rule_ver,
                })

        if any_ambiguous:
            return MediaMatchResult(status="ambiguous_multi_hit", refs=refs)
        if any_missing:
            return MediaMatchResult(status="missing", refs=refs)
        return MediaMatchResult(status="unique_hit", refs=refs)

    def read_tsv_inline_image(self, source_key: str, tsv_key: str, idx: int = 0) -> tuple[bytes, str] | None:
        """Decode an embedded base64 image from a registered TSV rule (e.g. MM-IFEval) on demand."""
        bm_key = next(
            (k for k, rule in self.rules.items() if rule.get("source_key", k) == source_key and rule.get("strategy") == "tsv_id_or_index_lookup"),
            None,
        )
        if bm_key is None:
            return None
        tsv_idx = self._ensure_tsv_index(bm_key)
        vals = tsv_idx.get(str(tsv_key).strip())
        if not vals or idx < 0 or idx >= len(vals):
            return None
        raw_val = vals[idx].strip()
        mime_hint: str | None = None
        if raw_val.startswith("data:"):
            header, _, b64_part = raw_val.partition(",")
            m = re.match(r"^data:(image/[A-Za-z0-9.+-]+);base64$", header.strip(), re.I)
            if m:
                mime_hint = m.group(1).lower()
            raw_val = b64_part
        try:
            data = base64.b64decode(re.sub(r"\s+", "", raw_val), validate=False)
        except Exception:
            return None
        if not data:
            return None
        return data, mime_hint or _sniff_image_mime(data)
