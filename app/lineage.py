"""多版本字段谱系（纯函数，无 IO）。

把 v1 → v2 → … → vN 的字段演进拆成两件事：

1. **先隔离歧义**：只在证据一致时连接相邻版本的字段；以下情况一律登记为
   歧义，不自动配对、不靠改名或默认值推断：

   - 同 id、列名变化（可能是改名，也可能是该 id 被新字段复用）
   - 同列名、id 变化（可能是 id 迁移，也可能是列名被新字段复用）
   - id 与名称的交叉证据互相矛盾（典型的 id 复用 + 列名迁移）
   - 类型变化不在显式矩阵内（unknown）或已被矩阵阻断（blocked）
   - 分区字段被删除 / 新增 / 改类型 / 改分区标记
   - 新增非空且无默认值的列

2. **再合成谱系**：把已确认的相邻关系连成逻辑字段（logical field）。每个
   逻辑字段保留逐版本物理绑定：历史分区继续按旧列名 / 旧 id / 旧类型读取，
   新查询统一按逻辑列名取数；类型变化只允许显式兼容的转换并给出 CAST。

只有「没有未决议的阻断歧义」才允许发布。所有输出都是确定性的：同一批
版本 + 同一组人工决议 → 同一 batch_hash、同一逻辑字段编号、同一份读取方案。
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from .types import TypeParseError, normalize_type, promotion

# 歧义分类
CATEGORY_LABELS = {
    "identity": "身份歧义",
    "type": "类型歧义",
    "partition": "分区歧义",
    "schema": "结构歧义",
}

IDENTITY_OPTIONS = ["same_field", "split"]
CONFLICT_OPTIONS = ["follow_name", "keep_id", "split_all"]
TYPE_OPTIONS = ["accept_type"]

OPTION_LABELS = {
    "same_field": "同一字段（确认延续）",
    "split": "不同字段（保持隔离）",
    "follow_name": "按列名延续（该 id 视为被新字段复用）",
    "keep_id": "按 id 保留（新列名视为该字段改名）",
    "split_all": "全部隔离（不建立跨版本映射）",
    "accept_type": "人工接受该类型转换",
}

STATUS_LABELS = {"active": "现行", "retired": "已退役"}

CONFIDENCE_LABELS = {
    "stable": "全程同类型",
    "compatible": "显式兼容提升",
    "manual": "人工接受转换",
    "unknown": "未确认（隔离）",
    "blocked": "被矩阵阻断",
}


class LineageInputError(ValueError):
    """谱系输入或人工决议不合法。"""


# ---------------------------------------------------------------------------
# 规范化 / 确定性
# ---------------------------------------------------------------------------

def normalize_versions(versions) -> list[dict]:
    """校验并复制版本列表；不修改入参。"""
    if not isinstance(versions, list) or not versions:
        raise LineageInputError("versions 必须是非空数组")
    out: list[dict] = []
    seen_versions: set[str] = set()
    for index, raw in enumerate(versions, start=1):
        if not isinstance(raw, dict):
            raise LineageInputError(f"versions 第 {index} 项必须是 JSON 对象")
        label = str(raw.get("version") or "").strip()
        if not label:
            raise LineageInputError(f"versions 第 {index} 项缺少 version 名称")
        if label in seen_versions:
            raise LineageInputError(f"版本名重复：{label}")
        seen_versions.add(label)
        fields = raw.get("fields")
        if not isinstance(fields, list) or not fields:
            raise LineageInputError(f"版本 {label} 的 fields 必须是非空数组")
        seen_names: set[str] = set()
        seen_ids: set[int] = set()
        normalized: list[dict] = []
        for fi, field in enumerate(fields, start=1):
            if not isinstance(field, dict):
                raise LineageInputError(f"版本 {label} 第 {fi} 个字段必须是 JSON 对象")
            name = str(field.get("name") or "").strip()
            if not name:
                raise LineageInputError(f"版本 {label} 第 {fi} 个字段缺少 name")
            if name in seen_names:
                raise LineageInputError(f"版本 {label} 字段名重复：{name}")
            seen_names.add(name)
            ftype = str(field.get("type") or "").strip()
            if not ftype:
                raise LineageInputError(f"版本 {label} 字段 {name} 缺少 type")
            try:
                ftype = normalize_type(ftype)
            except TypeParseError as exc:
                raise LineageInputError(f"版本 {label} 字段 {name} 类型不合法：{exc}")
            fid = field.get("id")
            if fid is not None:
                try:
                    fid = int(fid)
                except (TypeError, ValueError):
                    raise LineageInputError(f"版本 {label} 字段 {name} 的 id 必须是整数")
                if fid in seen_ids:
                    raise LineageInputError(f"版本 {label} field_id 重复：{fid}")
                seen_ids.add(fid)
            normalized.append({
                "id": fid,
                "name": name,
                "type": ftype,
                "nullable": bool(field.get("nullable", True)),
                "default": field.get("default"),
                "partition": bool(field.get("partition", False)),
            })
        out.append({
            "version": label,
            "fields": normalized,
            "partitions": [f["name"] for f in normalized if f["partition"]],
        })
    return out


def normalize_resolutions(raw) -> dict[str, str]:
    """接受 {key: decision} 或 [{key, decision}]；重复 key / 非法结构直接报错。"""
    if raw in (None, "", {}):
        return {}
    if isinstance(raw, str):
        raise LineageInputError("resolutions 必须是 JSON 对象，不能是字符串")
    if isinstance(raw, list):
        out: dict[str, str] = {}
        for index, item in enumerate(raw, start=1):
            if not isinstance(item, dict) or not item.get("key"):
                raise LineageInputError(f"resolutions 第 {index} 项必须是含 key 的对象")
            key = str(item["key"]).strip()
            if key in out:
                raise LineageInputError(f"resolutions key 重复：{key}")
            out[key] = _clean_decision(key, item.get("decision"))
        return out
    if not isinstance(raw, dict):
        raise LineageInputError("resolutions 必须是 JSON 对象")
    out = {}
    for raw_key, value in raw.items():
        key = str(raw_key).strip()
        if not key:
            raise LineageInputError("resolutions 存在空 key")
        out[key] = _clean_decision(key, value)
    return out


def _clean_decision(key: str, value) -> str:
    if isinstance(value, dict):
        value = value.get("decision")
    if not isinstance(value, str) or not value.strip():
        raise LineageInputError(f"resolutions[{key}] 的 decision 必须是非空字符串")
    return value.strip()


def compute_batch_hash(table: str, versions: list[dict], resolutions: dict[str, str]) -> str:
    payload = {
        "table": table,
        "versions": [
            {
                "version": v["version"],
                "fields": [
                    {
                        "id": f["id"],
                        "name": f["name"],
                        "type": f["type"],
                        "nullable": f["nullable"],
                        "default": f["default"],
                        "partition": f["partition"],
                    }
                    for f in v["fields"]
                ],
            }
            for v in versions
        ],
        "resolutions": dict(sorted(resolutions.items())),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 相邻版本匹配 + 歧义隔离
# ---------------------------------------------------------------------------

def _ref(version: str, field: dict) -> dict:
    return {
        "version": version,
        "field_id": field.get("id"),
        "name": field.get("name"),
        "type": field.get("type"),
    }


def _key_reuse(vfrom: str, vto: str, old: dict, new: dict) -> str:
    return f"{vfrom}>{vto}:id:{old.get('id')}:{old['name']}>{new['name']}"


def _key_id_changed(vfrom: str, vto: str, old: dict, new: dict) -> str:
    return f"{vfrom}>{vto}:name:{old['name']}:{old.get('id')}>{new.get('id')}"


def _key_conflict(vfrom: str, vto: str, olds: list[dict], news: list[dict]) -> str:
    old_part = "+".join(f"{f.get('id')}:{f['name']}" for f in sorted(olds, key=lambda f: str(f.get("id"))))
    new_part = "+".join(f"{f.get('id')}:{f['name']}" for f in sorted(news, key=lambda f: str(f.get("id"))))
    return f"{vfrom}>{vto}:reuse:{old_part}>{new_part}"


def _key_type(vfrom: str, vto: str, name: str, old_type: str, new_type: str) -> str:
    return f"{vfrom}>{vto}:type:{name}:{old_type}>{new_type}"


def _key_partition(vfrom: str, vto: str, name: str, change: str) -> str:
    return f"{vfrom}>{vto}:partition:{change}:{name}"


def _key_required(vfrom: str, vto: str, name: str) -> str:
    return f"{vfrom}>{vto}:required:{name}"


def _ambiguity(code: str, category: str, text: str, *, key: str, versions: list[str],
               fields: list[dict], options: list[str], resolution: dict | None = None) -> dict:
    return {
        "key": key,
        "code": code,
        "category": category,
        "category_label": CATEGORY_LABELS[category],
        "text": text,
        "versions": versions,
        "fields": fields,
        "options": options,
        "option_labels": {opt: OPTION_LABELS.get(opt, opt) for opt in options},
        "resolution": resolution,
        "resolved": resolution is not None,
        "blocking": resolution is None,
        "severity": "hazard" if resolution is not None else "block",
        "severity_label": "已隔离" if resolution is not None else "阻断",
    }


def _validate_decision(key: str, decision: str, options: list[str]) -> None:
    if decision not in options:
        allowed = " / ".join(options) if options else "（该歧义不可通过决议放行）"
        raise LineageInputError(f"resolutions[{key}] = {decision!r} 不适用，可选：{allowed}")


def _make_edge(oi: int, ni: int, basis: str, note: str, decision: str | None) -> dict:
    return {
        "old_index": oi,
        "new_index": ni,
        "basis": basis,
        "note": note,
        "decision": decision,
    }


def match_adjacent(va: dict, vb: dict, resolutions: dict[str, str],
                   used_keys: set[str]) -> dict:
    """匹配相邻两版：返回 edges / ambiguities / removed / added。"""
    vfrom, vto = va["version"], vb["version"]
    old_fields, new_fields = va["fields"], vb["fields"]

    old_by_id: dict[int, int] = {}
    new_by_id: dict[int, int] = {}
    old_by_name: dict[str, int] = {}
    new_by_name: dict[str, int] = {}
    for i, field in enumerate(old_fields):
        if field.get("id") is not None:
            old_by_id[int(field["id"])] = i
        old_by_name[field["name"]] = i
    for i, field in enumerate(new_fields):
        if field.get("id") is not None:
            new_by_id[int(field["id"])] = i
        new_by_name[field["name"]] = i

    edges: list[dict] = []
    ambiguities: list[dict] = []
    matched_old: set[int] = set()
    matched_new: set[int] = set()

    def add_edge(oi: int, ni: int, basis: str, note: str, decision: str | None = None) -> None:
        if oi in matched_old or ni in matched_new:
            raise LineageInputError(
                f"{vfrom}>{vto} 决议产生重复配对：id {old_fields[oi].get('id')} / {new_fields[ni].get('id')}")
        edges.append(_make_edge(oi, ni, basis, note, decision))
        matched_old.add(oi)
        matched_new.add(ni)

    # 1) id + name 双证据一致：唯一无需人工确认的连接
    for oi, of in enumerate(old_fields):
        oid = of.get("id")
        if oid is None:
            continue
        ni = new_by_id.get(int(oid))
        if ni is None or ni in matched_new:
            continue
        if new_fields[ni]["name"] == of["name"]:
            add_edge(oi, ni, "id+name", "field_id 与列名同时一致，认定为同一字段")

    # 1.5) 缺 field_id 的一侧：只剩列名一种证据，不存在 id 复用歧义，直接按名称配对。
    #      若对端的 id 槽位已被另一条旧字段占用，则跳过 —— 交给候选冲突检测，
    #      避免把「旧字段缺失 id」和「id 被复用」混为一谈。
    for oi, of in enumerate(old_fields):
        if oi in matched_old or of.get("id") is not None:
            continue
        ni = new_by_name.get(of["name"])
        if ni is None or ni in matched_new:
            continue
        nf = new_fields[ni]
        if nf.get("id") is not None and int(nf["id"]) in old_by_id:
            continue
        add_edge(oi, ni, "name", "旧版本未声明 field_id，仅按列名精确匹配（不做 id 推断）")
    for ni, nf in enumerate(new_fields):
        if ni in matched_new or nf.get("id") is not None:
            continue
        oi = old_by_name.get(nf["name"])
        if oi is None or oi in matched_old:
            continue
        of = old_fields[oi]
        if of.get("id") is not None and int(of["id"]) in new_by_id:
            continue
        add_edge(oi, ni, "name", "新版本未声明 field_id，仅按列名精确匹配（不做 id 推断）")

    # 2) 剩余字段的候选关系：id 相同或名称相同
    candidates: set[tuple[int, int, str]] = set()
    for oi, of in enumerate(old_fields):
        if oi in matched_old:
            continue
        oid = of.get("id")
        if oid is not None:
            ni = new_by_id.get(int(oid))
            if ni is not None and ni not in matched_new:
                candidates.add((oi, ni, "id"))
        ni = new_by_name.get(of["name"])
        if ni is not None and ni not in matched_new:
            candidates.add((oi, ni, "name"))

    # 候选关系连成连通分量；一个分量里出现交叉证据即视为冲突簇
    graph: dict[tuple[str, int], set[tuple[str, int]]] = defaultdict(set)
    for oi, ni, _kind in candidates:
        graph[("old", oi)].add(("new", ni))
        graph[("new", ni)].add(("old", oi))
    seen: set[tuple[str, int]] = set()
    clusters: list[dict] = []
    for node in list(graph):
        if node in seen:
            continue
        stack = [node]
        seen.add(node)
        node_set = set()
        while stack:
            cur = stack.pop()
            node_set.add(cur)
            for nxt in graph[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        old_nodes = sorted(i for side, i in node_set if side == "old")
        new_nodes = sorted(i for side, i in node_set if side == "new")
        cluster_candidates = [
            (oi, ni, kind) for (oi, ni, kind) in candidates
            if oi in old_nodes and ni in new_nodes
        ]
        clusters.append({
            "old": old_nodes,
            "new": new_nodes,
            "candidates": sorted(cluster_candidates),
            "involved_old": set(old_nodes),
            "involved_new": set(new_nodes),
        })

    for cluster in clusters:
        old_nodes, new_nodes = cluster["old"], cluster["new"]
        cluster_candidates = cluster["candidates"]
        involved_old, involved_new = cluster["involved_old"], cluster["involved_new"]
        clean = (
            len(cluster_candidates) == 1
            and len(old_nodes) == 1
            and len(new_nodes) == 1
            and len(graph[("old", old_nodes[0])]) == 1
            and len(graph[("new", new_nodes[0])]) == 1
        )
        if clean:
            oi, ni, kind = cluster_candidates[0]
            of, nf = old_fields[oi], new_fields[ni]
            key = (_key_reuse(vfrom, vto, of, nf) if kind == "id"
                   else _key_id_changed(vfrom, vto, of, nf))
            code = "id_slot_reused" if kind == "id" else "id_changed"
            if kind == "id":
                text = (f"field_id {of.get('id')} 未变但列名 {of['name']}→{nf['name']}："
                        f"可能是改名，也可能是该 id 被新字段复用。未决议前不建立映射。")
            else:
                text = (f"列名 {of['name']} 未变但 field_id {of.get('id')}→{nf.get('id')}："
                        f"可能是 id 迁移，也可能是该列名被新字段复用。未决议前不建立映射。")
            decision = resolutions.get(key)
            resolution = None
            if decision is not None:
                used_keys.add(key)
                _validate_decision(key, decision, IDENTITY_OPTIONS)
                resolution = {"decision": decision, "by": "input"}
            ambiguities.append(_ambiguity(
                code, "identity", text, key=key, versions=[vfrom, vto],
                fields=[_ref(vfrom, of), _ref(vto, nf)],
                options=IDENTITY_OPTIONS, resolution=resolution,
            ))
            if decision == "same_field":
                basis = "id" if kind == "id" else "name"
                add_edge(oi, ni, basis, f"人工决议 same_field：确认 {of['name']} 与 {nf['name']} 是同一字段")
            elif decision == "split":
                pass
            continue

        # 交叉证据冲突：id 槽位与列名指向不同字段（典型 id 复用 + 列名迁移）
        olds = [old_fields[i] for i in old_nodes]
        news = [new_fields[i] for i in new_nodes]
        key = _key_conflict(vfrom, vto, olds, news)
        decision = resolutions.get(key)
        resolution = None
        if decision is not None:
            used_keys.add(key)
            _validate_decision(key, decision, CONFLICT_OPTIONS)
            resolution = {"decision": decision, "by": "input"}
        old_desc = "、".join(f"{f.get('id')}:{f['name']}" for f in olds)
        new_desc = "、".join(f"{f.get('id')}:{f['name']}" for f in news)
        text = (f"field_id 与列名的证据互相矛盾（旧 {old_desc} → 新 {new_desc}）："
                f"最常见的原因是 id 被新字段复用、原字段改用新 id。"
                f"必须人工选择按列名还是按 id 延续，另一侧保持隔离；不得把复用 id 当成原字段。")
        ambiguities.append(_ambiguity(
            "identity_conflict", "identity", text, key=key, versions=[vfrom, vto],
            fields=[_ref(vfrom, f) for f in olds] + [_ref(vto, f) for f in news],
            options=CONFLICT_OPTIONS, resolution=resolution,
        ))
        if decision is None:
            continue
        if decision == "split_all":
            continue
        wanted = "name" if decision == "follow_name" else "id"
        paired = 0
        for oi, ni, kind in cluster_candidates:
            if kind != wanted or oi in matched_old or ni in matched_new:
                continue
            add_edge(oi, ni, "name" if wanted == "name" else "id",
                     f"人工决议 {decision}：{old_fields[oi]['name']} → {new_fields[ni]['name']}")
            paired += 1
        if paired == 0:
            raise LineageInputError(
                f"resolutions[{key}] = {decision!r} 在该冲突簇内无法建立任何映射；请改用 split_all 或检查决议")

    # 3) 类型 / 分区检查（只针对已建立的映射）
    for edge in edges:
        of = old_fields[edge["old_index"]]
        nf = new_fields[edge["new_index"]]
        edge["name_changed"] = of["name"] != nf["name"]
        edge["type_changed"] = of["type"] != nf["type"]
        edge["partition_changed"] = bool(of.get("partition")) != bool(nf.get("partition"))
        edge["type_result"] = promotion(of["type"], nf["type"]) if edge["type_changed"] else None
        edge["type_accepted"] = False

        if edge["type_changed"]:
            if of.get("partition") or nf.get("partition"):
                key = _key_type(vfrom, vto, nf["name"], of["type"], nf["type"])
                ambiguities.append(_ambiguity(
                    "partition_type_changed", "partition",
                    f"分区字段 {nf['name']} 类型 {of['type']}→{nf['type']}：分区路径与取值同时失效，不可自动迁移",
                    key=key, versions=[vfrom, vto],
                    fields=[_ref(vfrom, of), _ref(vto, nf)], options=[],
                ))
            else:
                verdict = edge["type_result"]["verdict"]
                key = _key_type(vfrom, vto, nf["name"], of["type"], nf["type"])
                if verdict == "blocked":
                    ambiguities.append(_ambiguity(
                        "type_blocked", "type",
                        f"{of['name']} 类型 {of['type']}→{nf['type']} 被显式矩阵阻断："
                        f"{edge['type_result']['reason']}",
                        key=key, versions=[vfrom, vto],
                        fields=[_ref(vfrom, of), _ref(vto, nf)], options=[],
                    ))
                elif verdict == "unknown":
                    decision = resolutions.get(key)
                    resolution = None
                    if decision is not None:
                        used_keys.add(key)
                        _validate_decision(key, decision, TYPE_OPTIONS)
                        resolution = {"decision": decision, "by": "input"}
                    ambiguities.append(_ambiguity(
                        "type_unknown", "type",
                        f"{of['name']} 类型 {of['type']}→{nf['type']} 不在显式矩阵内："
                        f"{edge['type_result']['reason']}；未人工确认前不生成转换规则",
                        key=key, versions=[vfrom, vto],
                        fields=[_ref(vfrom, of), _ref(vto, nf)],
                        options=TYPE_OPTIONS, resolution=resolution,
                    ))
                    if decision == "accept_type":
                        edge["type_accepted"] = True
        if edge["partition_changed"]:
            key = _key_partition(vfrom, vto, nf["name"], "flag")
            ambiguities.append(_ambiguity(
                "partition_changed", "partition",
                f"{nf['name']} 的分区标记由 {bool(of.get('partition'))} 变为 "
                f"{bool(nf.get('partition'))}：分区布局变化不可自动迁移",
                key=key, versions=[vfrom, vto],
                fields=[_ref(vfrom, of), _ref(vto, nf)], options=[],
            ))

    # 4) 未匹配字段的分区 / 非空约束检查。
    #    分区字段即使卷入身份歧义也要单独登记：无论最终按 id 还是按列名延续，
    #    历史分区能否被定位都是独立的风险，不能因为歧义还在排队就漏掉。
    involved_old: set[int] = set()
    involved_new: set[int] = set()
    for cluster in clusters:
        involved_old |= cluster["involved_old"]
        involved_new |= cluster["involved_new"]
    removed = [i for i in range(len(old_fields)) if i not in matched_old]
    added = [i for i in range(len(new_fields)) if i not in matched_new]
    for oi in removed:
        of = old_fields[oi]
        if of.get("partition"):
            key = _key_partition(vfrom, vto, of["name"], "removed")
            ambiguities.append(_ambiguity(
                "partition_removed", "partition",
                f"分区字段 {of['name']} 在 {vto} 被删除或未再声明：新查询无法定位 {vto} 之前的分区",
                key=key, versions=[vfrom, vto], fields=[_ref(vfrom, of)], options=[],
            ))
    for ni in added:
        nf = new_fields[ni]
        if nf.get("partition"):
            key = _key_partition(vfrom, vto, nf["name"], "added")
            ambiguities.append(_ambiguity(
                "partition_added", "partition",
                f"{vto} 新增分区字段 {nf['name']}：更早的历史分区没有该分区值",
                key=key, versions=[vfrom, vto], fields=[_ref(vto, nf)], options=[],
            ))
        elif (ni not in involved_new
              and not nf.get("nullable") and nf.get("default") is None):
            key = _key_required(vfrom, vto, nf["name"])
            ambiguities.append(_ambiguity(
                "required_field_added", "schema",
                f"{vto} 新增非空列 {nf['name']} 且没有默认值：历史分区无法满足，且不能用默认值伪造历史值",
                key=key, versions=[vfrom, vto], fields=[_ref(vto, nf)], options=[],
            ))

    return {
        "from_version": vfrom,
        "to_version": vto,
        "edges": edges,
        "ambiguities": ambiguities,
        "removed": removed,
        "added": added,
        "involved_old": sorted(involved_old),
        "involved_new": sorted(involved_new),
    }


# ---------------------------------------------------------------------------
# 组件（逻辑字段）合成
# ---------------------------------------------------------------------------

class _UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def build_components(versions: list[dict], resolutions: dict[str, str]) -> dict:
    sizes = [len(v["fields"]) for v in versions]
    offsets: list[int] = []
    total = 0
    for size in sizes:
        offsets.append(total)
        total += size
    uf = _UnionFind(total)

    boundaries: list[dict] = []
    ambiguities: list[dict] = []
    used_keys: set[str] = set()

    for index in range(1, len(versions)):
        result = match_adjacent(versions[index - 1], versions[index], resolutions, used_keys)
        boundaries.append(result)
        ambiguities.extend(result["ambiguities"])
        for edge in result["edges"]:
            uf.union(offsets[index - 1] + edge["old_index"], offsets[index] + edge["new_index"])

    groups: dict[int, list[tuple[int, int]]] = {}
    for vi, size in enumerate(sizes):
        for fi in range(size):
            groups.setdefault(uf.find(offsets[vi] + fi), []).append((vi, fi))

    ordered_nodes = sorted(groups.values(), key=lambda nodes: min(nodes))
    node_ci: dict[tuple[int, int], int] = {}
    for ci, nodes in enumerate(ordered_nodes):
        for node in nodes:
            node_ci[node] = ci

    comp_edges: dict[int, list[dict]] = defaultdict(list)
    for index, result in enumerate(boundaries, start=1):
        for edge in result["edges"]:
            ci = node_ci[(index - 1, edge["old_index"])]
            comp_edges[ci].append({
                **edge,
                "from_version": versions[index - 1]["version"],
                "to_version": versions[index]["version"],
            })

    unused = sorted(set(resolutions) - used_keys)
    if unused:
        raise LineageInputError(
            "以下 resolutions key 未命中任何歧义（不会静默忽略）：" + "，".join(unused))

    components = []
    for ci, nodes in enumerate(ordered_nodes):
        components.append({
            "index": ci,
            "nodes": nodes,
            "edges": comp_edges.get(ci, []),
        })

    return {
        "components": components,
        "node_component": node_ci,
        "boundaries": boundaries,
        "ambiguities": ambiguities,
        "used_resolutions": sorted(used_keys),
    }


def _binding(version: dict, field: dict) -> dict:
    return {
        "version": version["version"],
        "field_id": field.get("id"),
        "name": field["name"],
        "type": field["type"],
        "nullable": field["nullable"],
        "default": field["default"],
        "partition": field["partition"],
    }


def build_logical_fields(versions: list[dict], components: list[dict],
                         ambiguities: list[dict]) -> list[dict]:
    last_label = versions[-1]["version"]
    fields_by_version = {v["version"]: v["fields"] for v in versions}
    logicals: list[dict] = []

    for ci, component in enumerate(components):
        nodes = sorted(component["nodes"])
        bindings = [_binding(versions[vi], versions[vi]["fields"][fi]) for vi, fi in nodes]
        first_version = bindings[0]["version"]
        last_binding = bindings[-1]
        last_version = last_binding["version"]
        status = "active" if last_version == last_label else "retired"

        trail: list[dict] = []
        for edge in component["edges"]:
            if not edge.get("type_changed"):
                continue
            tr = edge["type_result"]
            trail.append({
                "from_version": edge["from_version"],
                "to_version": edge["to_version"],
                "from_type": tr["from"],
                "to_type": tr["to"],
                "verdict": tr["verdict"],
                "accepted": bool(edge.get("type_accepted")),
                "rule": tr["rule"],
                "reason": tr["reason"],
                "from_name": fields_by_version[edge["from_version"]][edge["old_index"]]["name"],
                "to_name": fields_by_version[edge["to_version"]][edge["new_index"]]["name"],
            })

        unified_type = last_binding["type"]
        confidence = "stable" if not trail else "compatible"
        for item in trail:
            if item["verdict"] == "blocked":
                unified_type = None
                confidence = "blocked"
                break
            if item["verdict"] == "unknown" and not item["accepted"]:
                unified_type = None
                confidence = "unknown"
                break
            if item["verdict"] == "unknown" and item["accepted"]:
                confidence = "manual"

        renames = [
            {
                "from_version": e["from_version"],
                "to_version": e["to_version"],
                "from_name": fields_by_version[e["from_version"]][e["old_index"]]["name"],
                "to_name": fields_by_version[e["to_version"]][e["new_index"]]["name"],
            }
            for e in component["edges"]
            if e.get("name_changed")
        ]
        backfill_required = any(
            b["default"] is not None and b["version"] != versions[0]["version"]
            for b in bindings
        )

        isolation: list[str] = []
        node_set = set(nodes)
        for amb in ambiguities:
            if amb["resolved"]:
                continue
            for ref in amb["fields"]:
                vi = next((i for i, v in enumerate(versions) if v["version"] == ref["version"]), None)
                if vi is None:
                    continue
                for fi, f in enumerate(versions[vi]["fields"]):
                    if f["name"] == ref["name"] and f.get("id") == ref["field_id"]:
                        if (vi, fi) in node_set:
                            isolation.append(amb["key"])

        logicals.append({
            "logical_id": f"LF-{ci + 1:03d}",
            "name": last_binding["name"],
            "status": status,
            "status_label": STATUS_LABELS[status],
            "first_version": first_version,
            "last_version": last_version,
            "versions": [b["version"] for b in bindings],
            "unified_type": unified_type,
            "type_confidence": confidence,
            "type_trail": trail,
            "renames": renames,
            "bindings": bindings,
            "requires_backfill": backfill_required,
            "isolation": sorted(set(isolation)),
            "notes": _logical_notes(bindings, status, backfill_required, trail, unified_type),
        })
    _assign_aliases(logicals)
    return logicals


def _assign_aliases(logicals: list[dict]) -> None:
    """逻辑列名必须唯一，否则新查询的 SELECT 别名会撞车。

    同名逻辑字段（常见于「旧字段退役、同名新字段出现」）用逻辑 ID 后缀区分，
    绝不把两者合并，也不默默地用后者覆盖前者。
    """
    counts: dict[str, int] = defaultdict(int)
    for lf in logicals:
        counts[lf["name"]] += 1
    for lf in logicals:
        if counts[lf["name"]] > 1:
            lf["alias"] = f"{lf['name']}__{lf['logical_id'].lower().replace('-', '_')}"
            lf["alias_note"] = (
                f"同名逻辑字段不止一个；新查询请用 {lf['alias']}，"
                f"避免与其它版本的 {lf['name']} 混用")
        else:
            lf["alias"] = lf["name"]
            lf["alias_note"] = None


def _logical_notes(bindings: list[dict], status: str, backfill: bool,
                   trail: list[dict], unified_type: str | None) -> list[str]:
    notes = []
    if status == "retired":
        notes.append("该逻辑字段在最新版本已退役；历史分区仍可按各版本物理列名读取。")
    if backfill:
        notes.append("该字段带默认值且历史版本没有该列：读取历史分区按 NULL 处理，需回填后才能与默认值口径一致。")
    if any(t["verdict"] == "unknown" and t["accepted"] for t in trail):
        notes.append("存在人工接受的未知类型转换；统一类型按最新声明，旧值读取时显式 CAST。")
    if trail and all(t["verdict"] == "compatible" for t in trail):
        notes.append("类型演进全部为显式兼容提升：旧值可按统一类型无损读取。")
    if unified_type is None:
        notes.append("类型演进未收敛，无法给出统一类型；该歧义未决议前不可发布。")
    return notes


# ---------------------------------------------------------------------------
# 读取方案
# ---------------------------------------------------------------------------

def build_read_plan(versions: list[dict], logicals: list[dict]) -> dict:
    """逐版本物理绑定 → 逻辑字段的映射；缺失一律 NULL，不用默认值填充。"""
    by_version: dict[str, list[dict]] = {}
    rows: list[dict] = []
    for version in versions:
        label = version["version"]
        version_rows: list[dict] = []
        for lf in logicals:
            binding = next((b for b in lf["bindings"] if b["version"] == label), None)
            if binding is None:
                expr = (f"CAST(NULL AS {lf['unified_type']}) AS {lf['alias']}"
                        if lf["unified_type"] else f"NULL AS {lf['alias']}")
                row = {
                    "version": label,
                    "logical_id": lf["logical_id"],
                    "logical_name": lf["name"],
                    "physical_name": None,
                    "physical_id": None,
                    "physical_type": None,
                    "unified_type": lf["unified_type"],
                    "expression": expr,
                    "missing": True,
                    "note": "该版本无此字段：按 NULL 读取，不以默认值伪造历史值",
                    "status": lf["status"],
                }
            else:
                if lf["unified_type"] and binding["type"] != lf["unified_type"]:
                    expr = f"CAST({binding['name']} AS {lf['unified_type']}) AS {lf['alias']}"
                    note = (f"物理类型 {binding['type']} 与统一类型 {lf['unified_type']} 不同，"
                            f"按显式兼容规则 CAST")
                else:
                    expr = f"{binding['name']} AS {lf['alias']}"
                    note = "物理类型与统一类型一致，直接读取"
                if binding["name"] != lf["alias"]:
                    note += f"；历史列名 {binding['name']} 继续兼容旧读取方式"
                if lf.get("alias_note"):
                    note += f"；{lf['alias_note']}"
                row = {
                    "version": label,
                    "logical_id": lf["logical_id"],
                    "logical_name": lf["name"],
                    "physical_name": binding["name"],
                    "physical_id": binding["field_id"],
                    "physical_type": binding["type"],
                    "unified_type": lf["unified_type"],
                    "expression": expr,
                    "missing": False,
                    "note": note,
                    "status": lf["status"],
                }
            version_rows.append(row)
            rows.append(row)
        by_version[label] = version_rows
    return {"rows": rows, "by_version": by_version}


def render_plan_sql(table: str, versions: list[dict], logicals: list[dict],
                    read_plan: dict, lineage_version: str, batch_hash: str,
                    publishable: bool = True) -> str:
    labels = [v["version"] for v in versions]
    active = [lf for lf in logicals if lf["status"] == "active"]
    lines = [
        "-- schemaevo 字段谱系读取方案",
        f"-- 表 {table} · 谱系 {lineage_version} · batch {batch_hash[:16]}",
        f"-- 版本序列：{' → '.join(labels)}",
        "-- 历史分区：不重写、不改列名；旧读取方式继续按各版本物理列名 / field_id 读取（方式 A）。",
        "-- 新查询：统一按逻辑列名取数，按分区所属版本选择对应表达式（方式 B）。",
        "-- 任何版本缺失的逻辑字段一律按 NULL 读取；不使用改名或默认值伪造历史值。",
    ]
    if not publishable:
        lines.append("-- ⚠ 该批次存在未决议歧义，尚未发布；本文件仅为预演，不可作为线上读取口径。")
    lines.append("")
    for version in versions:
        label = version["version"]
        rows = read_plan["by_version"].get(label, [])
        physical = [f["name"] for f in version["fields"]]
        lines.append(f"-- ===== 版本 {label} =====")
        lines.append("-- 方式 A：历史分区旧读取方式（物理列原样，不改名、不改类型）")
        lines.append(f"SELECT {', '.join(physical)}")
        lines.append(f"FROM {table} /* 仅 {label} 分区 */;")
        lines.append("-- 方式 B：新查询统一逻辑字段")
        lines.append("SELECT")
        logical_exprs = []
        for row in rows:
            if row["status"] != "active":
                continue
            logical_exprs.append("  " + row["expression"])
        lines.append(",\n".join(logical_exprs) if logical_exprs else "  *")
        lines.append(f"FROM {table} /* 仅 {label} 分区 */;")
        lines.append("")
    if not active:
        lines.append("-- 当前没有 active 逻辑字段。")
    lines.append("-- 说明：未决议歧义不会进入本方案；发布时已确认无阻断歧义。")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 总装
# ---------------------------------------------------------------------------

def build(table: str, versions, resolutions=None) -> dict:
    versions = normalize_versions(versions)
    table = str(table or "").strip()
    if not table:
        raise LineageInputError("缺少 table 名称")
    resolutions = normalize_resolutions(resolutions)
    batch_hash = compute_batch_hash(table, versions, resolutions)
    lineage_version = f"lin-{batch_hash[:16]}"

    compiled = build_components(versions, resolutions)
    logicals = build_logical_fields(versions, compiled["components"], compiled["ambiguities"])
    read_plan = build_read_plan(versions, logicals)

    ambiguities = compiled["ambiguities"]
    blocking = [a for a in ambiguities if a["blocking"]]
    resolved = [a for a in ambiguities if a["resolved"]]
    plan_sql = render_plan_sql(table, versions, logicals, read_plan, lineage_version,
                               batch_hash, publishable=not blocking)
    active = [lf for lf in logicals if lf["status"] == "active"]
    mappings = [
        {
            "logical_id": lf["logical_id"],
            "logical_name": lf["name"],
            "version": b["version"],
            "field_id": b["field_id"],
            "physical_name": b["name"],
            "physical_type": b["type"],
        }
        for lf in logicals for b in lf["bindings"]
    ]

    stats = {
        "versions": len(versions),
        "physical_fields": sum(len(v["fields"]) for v in versions),
        "logical_fields": len(logicals),
        "active_fields": len(active),
        "retired_fields": len(logicals) - len(active),
        "mappings": len(mappings),
        "ambiguities": len(ambiguities),
        "blocking": len(blocking),
        "resolved": len(resolved),
    }

    return {
        "table": table,
        "lineage_version": lineage_version,
        "batch_hash": batch_hash,
        "resolutions": dict(sorted(resolutions.items())),
        "used_resolutions": compiled["used_resolutions"],
        "versions": versions,
        "version_labels": [v["version"] for v in versions],
        "boundaries": compiled["boundaries"],
        "ambiguities": ambiguities,
        "blocking_ambiguities": blocking,
        "logical_fields": logicals,
        "mappings": mappings,
        "read_plan": read_plan,
        "plan_sql": plan_sql,
        "stats": stats,
        "publishable": not blocking,
        "publish_blocked_reason": (
            None if not blocking else
            f"存在 {len(blocking)} 项未决议歧义，禁止发布；历史分区仍按旧谱系读取"
        ),
    }
