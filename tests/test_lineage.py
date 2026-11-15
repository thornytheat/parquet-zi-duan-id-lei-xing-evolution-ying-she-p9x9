"""多版本字段谱系：歧义隔离、逻辑字段合成、发布幂等与失败保留旧版。"""
import pytest

from app import lineage, lineage_store, settings
from app.lineage import LineageInputError


@pytest.fixture(autouse=True)
def isolated_lineage_dir(tmp_path, monkeypatch):
    """发布测试互不干扰：每个用例一份独立的谱系存储目录。"""
    lineage_dir = tmp_path / "lineage"
    monkeypatch.setattr(settings, "LINEAGE_DIR", lineage_dir)
    monkeypatch.setattr(settings, "LINEAGE_VERSION_DIR", lineage_dir / "versions")
    monkeypatch.setattr(settings, "LINEAGE_ATTEMPT_DIR", lineage_dir / "attempts")
    yield lineage_dir


def f(fid, name, type_="string", nullable=True, default=None, partition=False):
    return {"id": fid, "name": name, "type": type_, "nullable": nullable,
            "default": default, "partition": partition}


def versions(*specs):
    out = []
    for label, fields in specs:
        out.append({"version": label, "fields": fields})
    return out


def logicals_by_name(build):
    return {lf["name"]: lf for lf in build["logical_fields"]}


# ---------------------------------------------------------------------------
# 歧义隔离
# ---------------------------------------------------------------------------

def test_stable_versions_need_no_resolution_and_are_publishable():
    build = lineage.build("t", versions(
        ("v1", [f(1, "a"), f(2, "b")]),
        ("v2", [f(1, "a"), f(2, "b")]),
    ))
    assert build["publishable"] is True
    assert build["ambiguities"] == []
    assert [lf["name"] for lf in build["logical_fields"]] == ["a", "b"]
    assert all(lf["unified_type"] == "string" for lf in build["logical_fields"])


def test_reused_id_with_changed_name_is_isolated_not_guessed():
    build = lineage.build("t", versions(
        ("v1", [f(1, "order_id"), f(4, "channel")]),
        ("v2", [f(1, "order_id"), f(4, "pay_channel")]),
    ))
    assert build["publishable"] is False
    codes = [a["code"] for a in build["ambiguities"]]
    assert codes == ["id_slot_reused"]
    amb = build["ambiguities"][0]
    assert amb["blocking"] is True
    assert amb["options"] == ["same_field", "split"]
    # 未决议时 channel 与 pay_channel 分属两个逻辑字段，不自动合并
    names = [lf["name"] for lf in build["logical_fields"]]
    assert "channel" in names and "pay_channel" in names
    assert logicals_by_name(build)["channel"]["versions"] == ["v1"]


def test_same_field_resolution_merges_and_keeps_history_binding():
    vs = versions(
        ("v1", [f(1, "order_id"), f(4, "channel"), f(3, "amount", "decimal(10,2)")]),
        ("v2", [f(1, "order_id"), f(4, "pay_channel"), f(3, "amount", "decimal(18,2)")]),
    )
    key = lineage.build("t", vs)["ambiguities"][0]["key"]
    build = lineage.build("t", vs, {key: "same_field"})
    assert build["publishable"] is True
    lf = logicals_by_name(build)["pay_channel"]
    assert lf["versions"] == ["v1", "v2"]
    assert lf["renames"][0]["from_name"] == "channel"
    # 历史分区仍按旧物理列名读取
    old_row = next(r for r in build["read_plan"]["rows"]
                   if r["version"] == "v1" and r["logical_name"] == "pay_channel")
    assert old_row["physical_name"] == "channel"
    assert old_row["expression"] == "channel AS pay_channel"
    # 类型提升：统一类型取最新，旧版本显式 CAST
    amount = next(r for r in build["read_plan"]["rows"]
                  if r["version"] == "v1" and r["logical_name"] == "amount")
    assert amount["expression"] == "CAST(amount AS decimal(18,2)) AS amount"


def test_id_reuse_with_name_migration_is_identity_conflict():
    build = lineage.build("t", versions(
        ("v1", [f(4, "channel")]),
        ("v2", [f(4, "branch"), f(9, "channel")]),
    ))
    conflict = build["ambiguities"][0]
    assert conflict["code"] == "identity_conflict"
    assert conflict["blocking"] is True
    assert conflict["options"] == ["follow_name", "keep_id", "split_all"]
    assert build["publishable"] is False
    # 冲突未决议时不得把 id=4 的 branch 当成 channel
    by_name = logicals_by_name(build)
    assert by_name["branch"]["versions"] == ["v2"]
    assert by_name["channel"]["versions"] == ["v2"]


def test_follow_name_keeps_id_slot_isolated():
    vs = versions(
        ("v1", [f(4, "channel")]),
        ("v2", [f(4, "branch"), f(9, "channel")]),
    )
    key = lineage.build("t", vs)["ambiguities"][0]["key"]
    build = lineage.build("t", vs, {key: "follow_name"})
    assert build["publishable"] is True
    by_name = logicals_by_name(build)
    assert by_name["channel"]["versions"] == ["v1", "v2"]
    assert by_name["branch"]["versions"] == ["v2"]
    plan = {r["version"]: r for r in build["read_plan"]["rows"]
            if r["logical_name"] == "channel"}
    assert plan["v1"]["physical_id"] == 4
    assert plan["v2"]["physical_id"] == 9


def test_changed_id_with_same_name_is_isolated_until_confirmed():
    build = lineage.build("t", versions(
        ("v1", [f(6, "carrier")]),
        ("v2", [f(9, "carrier")]),
    ))
    assert [a["code"] for a in build["ambiguities"]] == ["id_changed"]
    assert build["publishable"] is False
    names = [lf["name"] for lf in build["logical_fields"]]
    assert names.count("carrier") == 2


def test_blocked_type_change_cannot_be_resolved_away():
    vs = versions(
        ("v1", [f(4, "weight_gram", "int64", nullable=False)]),
        ("v2", [f(4, "weight_gram", "int32", nullable=False)]),
    )
    build = lineage.build("t", vs)
    assert build["publishable"] is False
    amb = build["ambiguities"][0]
    assert amb["code"] == "type_blocked"
    assert amb["options"] == []
    with pytest.raises(LineageInputError):
        lineage.build("t", vs, {amb["key"]: "accept_type"})


def test_unknown_type_requires_explicit_acceptance():
    vs = versions(
        ("v1", [f(1, "flag", "boolean")]),
        ("v2", [f(1, "flag", "int32")]),
    )
    build = lineage.build("t", vs)
    amb = build["ambiguities"][0]
    assert amb["code"] == "type_unknown"
    assert amb["blocking"] is True
    assert logicals_by_name(build)["flag"]["unified_type"] is None
    accepted = lineage.build("t", vs, {amb["key"]: "accept_type"})
    assert accepted["publishable"] is True
    lf = logicals_by_name(accepted)["flag"]
    assert lf["unified_type"] == "int32"
    assert lf["type_confidence"] == "manual"


def test_partition_changes_are_always_blocking():
    build = lineage.build("t", versions(
        ("v1", [f(1, "dt", "date", nullable=False, partition=True)]),
        ("v2", [f(1, "dt", "timestamp", nullable=False, partition=True)]),
    ))
    assert [a["code"] for a in build["ambiguities"]] == ["partition_type_changed"]
    assert build["publishable"] is False

    removed = lineage.build("t", versions(
        ("v1", [f(1, "dt", "date", nullable=False, partition=True)]),
        ("v2", [f(2, "x", "string")]),
    ))
    assert any(a["code"] == "partition_removed" for a in removed["ambiguities"])
    assert removed["publishable"] is False

    added = lineage.build("t", versions(
        ("v1", [f(1, "a")]),
        ("v2", [f(1, "a"), f(2, "dt", "date", nullable=False, partition=True)]),
    ))
    assert any(a["code"] == "partition_added" for a in added["ambiguities"])
    assert added["publishable"] is False


def test_required_added_column_blocks_and_plan_reads_null():
    build = lineage.build("t", versions(
        ("v1", [f(1, "a")]),
        ("v2", [f(1, "a"), f(2, "b", "int64", nullable=False)]),
    ))
    assert [a["code"] for a in build["ambiguities"]] == ["required_field_added"]
    row = next(r for r in build["read_plan"]["rows"]
               if r["version"] == "v1" and r["logical_name"] == "b")
    assert row["missing"] is True
    assert row["expression"] == "CAST(NULL AS int64) AS b"


def test_default_value_is_not_used_to_fake_history():
    build = lineage.build("t", versions(
        ("v1", [f(1, "a")]),
        ("v2", [f(1, "a"), f(2, "fee", "decimal(10,2)", nullable=False, default="0.00")]),
    ))
    assert build["publishable"] is True
    lf = logicals_by_name(build)["fee"]
    assert lf["requires_backfill"] is True
    row = next(r for r in build["read_plan"]["rows"]
               if r["version"] == "v1" and r["logical_name"] == "fee")
    assert "NULL" in row["expression"] and "0.00" not in row["expression"]


def test_missing_id_side_matches_by_name_only():
    build = lineage.build("t", versions(
        ("v1", [{"id": None, "name": "a", "type": "string", "nullable": True,
                 "default": None, "partition": False}]),
        ("v2", [f(1, "a")]),
    ))
    assert build["ambiguities"] == []
    assert logicals_by_name(build)["a"]["versions"] == ["v1", "v2"]


def test_three_versions_chain_and_retired_fields():
    build = lineage.build("t", versions(
        ("v1", [f(1, "a"), f(2, "legacy")]),
        ("v2", [f(1, "a"), f(2, "legacy")]),
        ("v3", [f(1, "a")]),
    ))
    assert build["publishable"] is True
    assert logicals_by_name(build)["a"]["versions"] == ["v1", "v2", "v3"]
    legacy = logicals_by_name(build)["legacy"]
    assert legacy["status"] == "retired"
    assert legacy["versions"] == ["v1", "v2"]


def test_batch_hash_is_deterministic_and_resolution_sensitive():
    vs = versions(("v1", [f(4, "channel")]), ("v2", [f(4, "pay_channel")]))
    first = lineage.build("t", vs)
    same = lineage.build("t", vs)
    assert first["batch_hash"] == same["batch_hash"]
    assert first["lineage_version"] == same["lineage_version"]
    key = first["ambiguities"][0]["key"]
    resolved = lineage.build("t", vs, {key: "same_field"})
    assert resolved["batch_hash"] != first["batch_hash"]


def test_unused_resolution_key_is_rejected():
    vs = versions(("v1", [f(1, "a")]), ("v2", [f(1, "a")]))
    with pytest.raises(LineageInputError) as exc:
        lineage.build("t", vs, {"nope": "same_field"})
    assert "未命中" in str(exc.value)


def test_resolution_structure_validation():
    vs = versions(("v1", [f(1, "a")]), ("v2", [f(1, "a")]))
    with pytest.raises(LineageInputError):
        lineage.build("t", vs, "bad")
    with pytest.raises(LineageInputError):
        lineage.build("t", vs, [{"key": "", "decision": "x"}])
    with pytest.raises(LineageInputError):
        lineage.build("t", vs, [{"key": "k", "decision": "x"}, {"key": "k", "decision": "y"}])


def test_duplicate_version_names_and_ids_rejected():
    with pytest.raises(LineageInputError):
        lineage.build("t", versions(("v1", [f(1, "a")]), ("v1", [f(1, "a")])))
    with pytest.raises(LineageInputError):
        lineage.build("t", versions(("v1", [f(1, "a"), f(1, "b")])))


def test_same_name_logical_fields_get_distinct_aliases():
    # 同名但 id 变了且未决议：两个逻辑字段都叫 name，查询别名必须区分，不能互相覆盖
    build = lineage.build("t", versions(
        ("v1", [f(2, "name", "string")]),
        ("v2", [f(9, "name", "string")]),
    ))
    aliases = [lf["alias"] for lf in build["logical_fields"]]
    assert len(aliases) == 2
    assert len(set(aliases)) == 2
    assert all(lf["alias_note"] for lf in build["logical_fields"])
    exprs = [r["expression"] for r in build["read_plan"]["rows"]]
    assert len(set(exprs)) == len(exprs)


def test_download_name_and_sql_contains_both_modes():
    build = lineage.build("t", versions(
        ("v1", [f(1, "a"), f(2, "b", "int32")]),
        ("v2", [f(1, "a"), f(2, "b", "int64")]),
    ))
    sql = build["plan_sql"]
    assert "方式 A" in sql and "方式 B" in sql
    assert "CAST(b AS int64) AS b" in sql
    assert build["lineage_version"] in sql


# ---------------------------------------------------------------------------
# 发布：失败保留旧版 / 幂等
# ---------------------------------------------------------------------------

def test_publish_rejected_keeps_previous_version(tmp_path):
    good = lineage.build("t", versions(
        ("v1", [f(1, "a"), f(4, "channel")]),
        ("v2", [f(1, "a"), f(4, "pay_channel")]),
    ))
    key = good["ambiguities"][0]["key"]
    good = lineage.build("t", versions(
        ("v1", [f(1, "a"), f(4, "channel")]),
        ("v2", [f(1, "a"), f(4, "pay_channel")]),
    ), {key: "same_field"})
    first = lineage_store.publish(good, "测试")
    assert first["published"] is True
    assert first["status"] == "published"
    active = lineage_store.active_version("t")
    assert active["lineage_version"] == good["lineage_version"]

    bad = lineage.build("t", versions(
        ("v1", [f(1, "a"), f(4, "channel")]),
        ("v2", [f(1, "a"), f(4, "pay_channel"), f(5, "x", "int64", nullable=False)]),
    ))
    # 无 resolution：歧义阻断发布
    rejected = lineage_store.publish(bad, "测试")
    assert rejected["published"] is False
    assert rejected["status"] == "rejected"
    assert rejected["active_version_after"] == good["lineage_version"]
    assert lineage_store.active_version("t")["lineage_version"] == good["lineage_version"]
    attempt = lineage_store.load_attempt(rejected["attempt_id"])
    assert attempt["status"] == "rejected"
    assert attempt["active_version_before"] == good["lineage_version"]


def test_publish_same_batch_is_idempotent_and_adds_no_mappings():
    vs = versions(
        ("v1", [f(1, "a"), f(4, "channel")]),
        ("v2", [f(1, "a"), f(4, "pay_channel")]),
    )
    build = lineage.build("t", vs)
    key = build["ambiguities"][0]["key"]
    build = lineage.build("t", vs, {key: "same_field"})
    first = lineage_store.publish(build, "测试")
    second = lineage_store.publish(build, "测试")
    assert first["status"] == "published"
    assert second["status"] == "deduped"
    assert second["deduped"] is True
    assert second["mappings_added"] == 0
    assert second["lineage_version"] == first["lineage_version"]
    versions_on_disk = [v for v in lineage_store.list_published() if v["table"] == "t"]
    assert len(versions_on_disk) == 1


def test_new_batch_keeps_old_version_snapshot_for_history():
    base = versions(("v1", [f(1, "a")]), ("v2", [f(1, "a")]))
    first = lineage_store.publish(lineage.build("t", base), "测试")
    extended = versions(
        ("v1", [f(1, "a")]),
        ("v2", [f(1, "a")]),
        ("v3", [f(1, "a"), f(2, "b")]),
    )
    second = lineage_store.publish(lineage.build("t", extended), "测试")
    assert second["lineage_version"] != first["lineage_version"]
    assert lineage_store.load_published(first["lineage_version"])["version_labels"] == ["v1", "v2"]
    assert lineage_store.load_published(second["lineage_version"])["version_labels"] == ["v1", "v2", "v3"]
    current = lineage_store.active_version("t")
    assert current["lineage_version"] == second["lineage_version"]
    assert current["previous_lineage_version"] == first["lineage_version"]
    rows = [v for v in lineage_store.list_published() if v["table"] == "t"]
    assert {r["lineage_version"] for r in rows} == {first["lineage_version"], second["lineage_version"]}
    assert sum(1 for r in rows if r["is_current"]) == 1


def test_snapshot_and_history_share_same_plan():
    build = lineage.build("t", versions(
        ("v1", [f(1, "a", "int32")]),
        ("v2", [f(1, "a", "int64")]),
    ))
    pub = lineage_store.publish(build, "测试")
    snap = lineage_store.load_published(pub["lineage_version"])
    assert snap["plan_sql"] == build["plan_sql"]
    assert snap["mappings"] == build["mappings"]
    assert snap["stats"]["mappings"] == len(build["mappings"])
