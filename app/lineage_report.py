"""谱系视图组装：把纯函数构建结果拼成页面直接可渲染的结构。"""
from __future__ import annotations

from .lineage import CATEGORY_LABELS, CONFIDENCE_LABELS, OPTION_LABELS


def version_timeline(build: dict) -> list[dict]:
    """版本时间线：每版物理字段数、分区字段、与上一版的边界摘要。"""
    boundaries = {b["to_version"]: b for b in build.get("boundaries", [])}
    rows = []
    for index, version in enumerate(build["versions"]):
        if index == 0:
            continue
        boundary = boundaries.get(version["version"], {})
        rows.append({
            "version": version["version"],
            "fields": len(version["fields"]),
            "partitions": version["partitions"],
            "matched": len(boundary.get("edges", [])),
            "removed": len(boundary.get("removed", [])),
            "added": len(boundary.get("added", [])),
            "ambiguities": len(boundary.get("ambiguities", [])),
        })
    return rows


def ambiguity_rows(build: dict) -> list[dict]:
    rows = []
    for item in build.get("ambiguities", []):
        rows.append({
            "key": item["key"],
            "code": item["code"],
            "category": item["category"],
            "category_label": item.get("category_label") or CATEGORY_LABELS.get(item["category"], item["category"]),
            "text": item["text"],
            "versions": item["versions"],
            "fields": item["fields"],
            "fields_text": "；".join(
                f"{f['version']} · {f['field_id'] if f['field_id'] is not None else '—'} · "
                f"{f['name']} · {f['type']}"
                for f in item["fields"]
            ),
            "options": [
                {"value": opt, "label": OPTION_LABELS.get(opt, opt)}
                for opt in item["options"]
            ],
            "resolution": item["resolution"],
            "resolved": item["resolved"],
            "blocking": item["blocking"],
            "severity": item["severity"],
            "severity_label": item["severity_label"],
        })
    return rows


def logical_rows(build: dict) -> list[dict]:
    rows = []
    for lf in build.get("logical_fields", []):
        rows.append({
            "logical_id": lf["logical_id"],
            "name": lf["name"],
            "status": lf["status"],
            "status_label": lf["status_label"],
            "versions": lf["versions"],
            "first_version": lf["first_version"],
            "last_version": lf["last_version"],
            "unified_type": lf["unified_type"],
            "type_confidence": lf["type_confidence"],
            "type_confidence_label": CONFIDENCE_LABELS.get(lf["type_confidence"], lf["type_confidence"]),
            "requires_backfill": lf["requires_backfill"],
            "isolation_count": len(lf["isolation"]),
            "isolation": lf["isolation"],
            "notes": lf["notes"],
            "renames_text": "；".join(
                f"{r['from_version']} {r['from_name']} → {r['to_version']} {r['to_name']}"
                for r in lf["renames"]
            ) or "—",
            "bindings": lf["bindings"],
            "type_trail": lf["type_trail"],
        })
    return rows


def binding_matrix(build: dict) -> dict:
    """逻辑字段 × 版本 的物理绑定矩阵（页面表格用）。"""
    labels = build.get("version_labels", [])
    lookup = {(m["logical_id"], m["version"]): m for m in build.get("mappings", [])}
    rows = []
    for lf in build.get("logical_fields", []):
        cells = []
        for label in labels:
            mapping = lookup.get((lf["logical_id"], label))
            cells.append({
                "version": label,
                "bound": mapping is not None,
                "field_id": mapping["field_id"] if mapping else None,
                "physical_name": mapping["physical_name"] if mapping else None,
                "physical_type": mapping["physical_type"] if mapping else None,
            })
        rows.append({
            "logical_id": lf["logical_id"],
            "name": lf["name"],
            "unified_type": lf["unified_type"],
            "status": lf["status"],
            "status_label": lf["status_label"],
            "cells": cells,
        })
    return {"versions": labels, "rows": rows}


def read_plan_rows(build: dict) -> list[dict]:
    return build.get("read_plan", {}).get("rows", [])


def plan_download_name(table: str, lineage_version: str) -> str:
    return f"{table}-{lineage_version}-read-plan.sql"


def compile_summary(build: dict) -> dict:
    """页面 / API 共用的谱系摘要（不含逐版本绑定矩阵与 SQL 原文）。"""
    return {
        "table": build["table"],
        "lineage_version": build["lineage_version"],
        "batch_hash": build["batch_hash"],
        "version_labels": build["version_labels"],
        "publishable": build["publishable"],
        "publish_blocked_reason": build["publish_blocked_reason"],
        "stats": build["stats"],
        "resolutions": build["resolutions"],
        "ambiguities": ambiguity_rows(build),
        "logical_fields": logical_rows(build),
    }
