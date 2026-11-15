"""字段谱系发布存储：发布版本快照 + 发布尝试历史。

目录结构（DATA_DIR/lineage）：

    index.json                    表 → 当前生效谱系版本
    versions/<lineage_version>.json  已发布谱系快照（含映射与读取方案原文）
    attempts/<attempt_id>.json       每次构建 / 发布尝试（含被拒绝的发布）

规则：
- 发布被阻断（存在未决议歧义）时只记录 attempt，不写快照、不动 index，旧版继续生效。
- 同一 table + 同一批输入（batch_hash 相同）重复发布是幂等的：复用已发布版本，
  不新增任何映射行，返回 deduped=True。
- 每次成功发布新批次会新增一个谱系版本，旧版本快照保留在历史中可回看、可下载。
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

from . import settings

ATTEMPT_ID_RE = re.compile(r"^att-\d{8}T\d{6}-[0-9a-f]{6}$")
LINEAGE_VERSION_RE = re.compile(r"^lin-[0-9a-f]{16}$")


class LineageStoreError(ValueError):
    """存储层输入不合法。"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _ensure_dirs() -> None:
    for path in (settings.LINEAGE_DIR, settings.LINEAGE_VERSION_DIR, settings.LINEAGE_ATTEMPT_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _index_path():
    return settings.LINEAGE_DIR / "index.json"


def _read_index() -> dict:
    path = _index_path()
    if not path.exists():
        return {"tables": {}}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"tables": {}}
    if not isinstance(doc, dict) or not isinstance(doc.get("tables"), dict):
        return {"tables": {}}
    return doc


def _write_index(doc: dict) -> None:
    _ensure_dirs()
    _index_path().write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def new_attempt_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"att-{stamp}-{uuid.uuid4().hex[:6]}"


def active_version(table: str) -> dict | None:
    entry = _read_index()["tables"].get(table)
    return entry if entry else None


def load_published(lineage_version: str) -> dict:
    if not LINEAGE_VERSION_RE.match(lineage_version or ""):
        raise KeyError(f"谱系版本 {lineage_version!r} 不存在")
    path = settings.LINEAGE_VERSION_DIR / f"{lineage_version}.json"
    if not path.exists():
        raise KeyError(f"谱系版本 {lineage_version!r} 不存在")
    return json.loads(path.read_text(encoding="utf-8"))


def list_published(limit: int = 50) -> list[dict]:
    _ensure_dirs()
    paths = sorted(settings.LINEAGE_VERSION_DIR.glob("lin-*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    tables = _read_index()["tables"]
    rows = []
    for path in paths[:limit]:
        try:
            snap = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        current = tables.get(snap.get("table"), {})
        rows.append({
            "lineage_version": snap.get("lineage_version"),
            "table": snap.get("table"),
            "batch_hash": snap.get("batch_hash"),
            "published_at": snap.get("published_at"),
            "source": snap.get("source"),
            "stats": snap.get("stats", {}),
            "version_labels": snap.get("version_labels", []),
            "is_current": current.get("lineage_version") == snap.get("lineage_version"),
        })
    return rows


def save_attempt(attempt: dict) -> dict:
    _ensure_dirs()
    attempt = dict(attempt)
    attempt.setdefault("attempt_id", new_attempt_id())
    if not ATTEMPT_ID_RE.match(attempt["attempt_id"]):
        raise LineageStoreError(f"attempt_id 不合法：{attempt['attempt_id']!r}")
    path = settings.LINEAGE_ATTEMPT_DIR / f"{attempt['attempt_id']}.json"
    path.write_text(json.dumps(attempt, ensure_ascii=False, indent=2), encoding="utf-8")
    _prune()
    return attempt


def load_attempt(attempt_id: str) -> dict:
    if not ATTEMPT_ID_RE.match(attempt_id or ""):
        raise KeyError(f"发布尝试 {attempt_id!r} 不存在")
    path = settings.LINEAGE_ATTEMPT_DIR / f"{attempt_id}.json"
    if not path.exists():
        raise KeyError(f"发布尝试 {attempt_id!r} 不存在")
    return json.loads(path.read_text(encoding="utf-8"))


def list_attempts(limit: int = 50) -> list[dict]:
    _ensure_dirs()
    paths = sorted(settings.LINEAGE_ATTEMPT_DIR.glob("att-*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    rows = []
    for path in paths[:limit]:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        rows.append({
            "attempt_id": doc.get("attempt_id"),
            "created_at": doc.get("created_at"),
            "table": doc.get("table"),
            "source": doc.get("source"),
            "status": doc.get("status"),
            "status_label": doc.get("status_label"),
            "lineage_version": doc.get("lineage_version"),
            "batch_hash": doc.get("batch_hash"),
            "blocking_count": doc.get("summary", {}).get("blocking", 0),
            "mappings_added": doc.get("mappings_added", 0),
        })
    return rows


def publish(build_result: dict, source: str = "自定义输入") -> dict:
    """按构建结果发布；返回发布结果字典（同时落 attempt 记录）。"""
    _ensure_dirs()
    table = build_result["table"]
    batch_hash = build_result["batch_hash"]
    lineage_version = build_result["lineage_version"]
    before = active_version(table)
    before_version = before.get("lineage_version") if before else None

    attempt = {
        "attempt_id": new_attempt_id(),
        "created_at": _now(),
        "table": table,
        "source": source,
        "batch_hash": batch_hash,
        "lineage_version": None,
        "active_version_before": before_version,
        "resolutions": build_result.get("resolutions", {}),
        "summary": build_result.get("stats", {}),
        "build": build_result,
        "mappings_added": 0,
    }

    # 1) 有未决议歧义：拒绝发布，旧版保持不变
    if not build_result["publishable"]:
        attempt.update({
            "status": "rejected",
            "status_label": "发布失败（歧义未决议）",
            "active_version_after": before_version,
            "message": build_result.get("publish_blocked_reason")
            or "存在未决议歧义，发布被拒绝；旧版谱系继续生效",
        })
        save_attempt(attempt)
        return _result(attempt, published=False, deduped=False)

    # 2) 同一批已发布过：幂等复用，不新增映射
    #    lineage_version 由 batch_hash 派生，快照存在即说明同批已发布。
    existing_path = settings.LINEAGE_VERSION_DIR / f"{lineage_version}.json"
    existing = None
    if existing_path.exists():
        try:
            candidate = json.loads(existing_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            candidate = None
        if candidate and candidate.get("batch_hash") == batch_hash:
            existing = candidate
    if existing is not None:
        attempt.update({
            "status": "deduped",
            "status_label": "幂等重放（未新增映射）",
            "lineage_version": existing["lineage_version"],
            "active_version_after": before_version,
            "mappings_added": 0,
            "message": f"同一批输入已发布为 {existing['lineage_version']}，本次重跑不新增映射",
        })
        save_attempt(attempt)
        return _result(attempt, published=True, deduped=True)

    # 3) 成功发布新版本：写快照、更新 index；旧版本快照保留
    snapshot = {
        "lineage_version": lineage_version,
        "table": table,
        "batch_hash": batch_hash,
        "published_at": _now(),
        "source": source,
        "attempt_id": attempt["attempt_id"],
        "supersedes": before_version,
        "resolutions": build_result.get("resolutions", {}),
        "version_labels": build_result.get("version_labels", []),
        "versions": build_result["versions"],
        "boundaries": build_result["boundaries"],
        "ambiguities": build_result["ambiguities"],
        "blocking_ambiguities": build_result["blocking_ambiguities"],
        "logical_fields": build_result["logical_fields"],
        "mappings": build_result["mappings"],
        "read_plan": build_result["read_plan"],
        "plan_sql": build_result["plan_sql"],
        "stats": build_result["stats"],
    }
    path = settings.LINEAGE_VERSION_DIR / f"{lineage_version}.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    doc = _read_index()
    doc["tables"][table] = {
        "lineage_version": lineage_version,
        "batch_hash": batch_hash,
        "published_at": snapshot["published_at"],
        "previous_lineage_version": before_version,
    }
    _write_index(doc)

    attempt.update({
        "status": "published",
        "status_label": "已发布",
        "lineage_version": lineage_version,
        "active_version_after": lineage_version,
        "mappings_added": len(snapshot["mappings"]),
        "message": f"已发布谱系 {lineage_version}，旧版 {before_version or '（无）'} 保留在历史",
    })
    save_attempt(attempt)
    return _result(attempt, published=True, deduped=False)


def _result(attempt: dict, *, published: bool, deduped: bool) -> dict:
    return {
        "attempt_id": attempt["attempt_id"],
        "published": published,
        "deduped": deduped,
        "status": attempt["status"],
        "status_label": attempt["status_label"],
        "message": attempt.get("message"),
        "table": attempt["table"],
        "lineage_version": attempt.get("lineage_version"),
        "active_version_before": attempt.get("active_version_before"),
        "active_version_after": attempt.get("active_version_after"),
        "mappings_added": attempt.get("mappings_added", 0),
        "blocking_count": attempt.get("summary", {}).get("blocking", 0),
        "redirect": (f"/lineage/result/{attempt['lineage_version']}"
                     if attempt.get("lineage_version") else f"/lineage/attempt/{attempt['attempt_id']}"),
    }


def _prune() -> None:
    current = {e.get("lineage_version") for e in _read_index()["tables"].values()}
    paths = sorted(settings.LINEAGE_VERSION_DIR.glob("lin-*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    kept = 0
    for path in paths:
        if path.stem in current:
            continue
        kept += 1
        if kept > settings.MAX_LINEAGE_KEPT:
            try:
                path.unlink()
            except OSError:
                pass
    attempts = sorted(settings.LINEAGE_ATTEMPT_DIR.glob("att-*.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    for path in attempts[settings.MAX_LINEAGE_KEPT:]:
        try:
            path.unlink()
        except OSError:
            pass
