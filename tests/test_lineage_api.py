"""字段谱系 API / 页面 / 发布幂等 / 失败保留旧版。"""
import pytest

from fastapi.testclient import TestClient

from app import settings
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_lineage_dir(tmp_path, monkeypatch):
    lineage_dir = tmp_path / "lineage"
    monkeypatch.setattr(settings, "LINEAGE_DIR", lineage_dir)
    monkeypatch.setattr(settings, "LINEAGE_VERSION_DIR", lineage_dir / "versions")
    monkeypatch.setattr(settings, "LINEAGE_ATTEMPT_DIR", lineage_dir / "attempts")
    yield lineage_dir


def publish(payload):
    return client.post("/api/lineage/publish", json=payload)


def test_lineage_workbench_renders_samples():
    res = client.get("/lineage")
    assert res.status_code == 200
    assert "多版本字段谱系工作台" in res.text
    assert "dws_member_profile" in res.text
    assert "歧义隔离清单" in res.text
    assert client.get("/lineage?sample=E").status_code == 200


def test_lineage_sample_api():
    body = client.get("/api/lineage/samples/D").json()
    assert body["table"] == "dws_member_profile"
    assert len(body["versions"]) == 3
    assert body["resolutions"]
    assert client.get("/api/lineage/samples/Z").status_code == 404


def test_preview_sample_d_shows_resolved_ambiguity_and_plan():
    body = client.post("/api/lineage/preview", json={"sample": "D"}).json()
    assert body["publishable"] is True
    assert body["stats"]["versions"] == 3
    assert body["stats"]["ambiguities"] == 1
    assert body["stats"]["resolved"] == 1
    ambiguity = body["ambiguities"][0]
    assert ambiguity["code"] == "id_slot_reused"
    assert ambiguity["resolved"] is True
    assert ambiguity["resolution"]["decision"] == "same_field"
    assert body["plan_sql"]
    assert body["binding_matrix"]["versions"] == ["v1-2025.06", "v2-2025.12", "v3-2026.06"]


def test_preview_sample_e_is_blocked_and_lists_conflict():
    body = client.post("/api/lineage/preview", json={"sample": "E"}).json()
    assert body["publishable"] is False
    assert body["stats"]["blocking"] == 2
    codes = {a["code"] for a in body["ambiguities"] if a["blocking"]}
    assert codes == {"identity_conflict", "type_blocked"}
    conflict = next(a for a in body["ambiguities"] if a["code"] == "identity_conflict")
    assert [opt["value"] for opt in conflict["options"]] == ["follow_name", "keep_id", "split_all"]


def test_preview_custom_versions_accepts_json_strings():
    payload = {
        "table": "t",
        "versions": '[{"version":"v1","fields":[{"id":1,"name":"a","type":"int32"}]},'
                    '{"version":"v2","fields":[{"id":1,"name":"a","type":"int64"}]}]',
    }
    res = client.post("/api/lineage/preview", json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["publishable"] is True
    old = next(r for r in body["read_plan_rows"] if r["version"] == "v1")
    assert old["expression"] == "CAST(a AS int64) AS a"


@pytest.mark.parametrize("payload,needle", [
    ({"versions": "not json"}, "不是合法 JSON"),
    ({"versions": []}, "versions 必须是非空数组"),
    ({"table": "t", "versions": [{"version": "v1", "fields": []}]}, "fields 必须是非空数组"),
    ({"table": "t", "versions": [{"version": "v1", "fields": [{"id": 1, "name": "a", "type": "money"}]}]},
     "类型不合法"),
    ({"table": "t", "versions": [{"version": "v1", "fields": [{"id": 1, "name": "a", "type": "string"}]},
                                 {"version": "v1", "fields": [{"id": 1, "name": "a", "type": "string"}]}]},
     "版本名重复"),
    ({"sample": "D", "resolutions": {"unknown-key": "same_field"}}, "未命中"),
])
def test_preview_bad_inputs_return_400(payload, needle):
    res = client.post("/api/lineage/preview", json=payload)
    assert res.status_code == 400
    assert needle in res.json()["detail"]


def test_publish_sample_d_then_idempotent_replay():
    first = publish({"sample": "D"})
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "published"
    assert body["published"] is True
    assert body["mappings_added"] > 0
    version = body["lineage_version"]
    assert body["redirect"] == f"/lineage/result/{version}"

    second = publish({"sample": "D"}).json()
    assert second["status"] == "deduped"
    assert second["deduped"] is True
    assert second["mappings_added"] == 0
    assert second["lineage_version"] == version

    listed = client.get("/api/lineage").json()
    assert listed["count"] == 1
    snapshot = client.get(f"/api/lineage/versions/{version}").json()
    assert snapshot["table"] == "dws_member_profile"
    assert len(snapshot["mappings"]) == body["data"]["stats"]["mappings"]
    # 结果页 / 历史 / 下载方案 指向同一版谱系
    result_page = client.get(f"/lineage/result/{version}")
    assert result_page.status_code == 200
    assert version in result_page.text
    sql = client.get(f"/api/lineage/versions/{version}/read-plan.sql")
    assert sql.status_code == 200
    assert "方式 A" in sql.text and "方式 B" in sql.text
    assert version in sql.text
    assert "attachment" in sql.headers.get("content-disposition", "")


def test_publish_rejected_keeps_previous_version_and_records_attempt():
    good = publish({"sample": "D"}).json()
    good_version = good["lineage_version"]

    rejected = publish({"sample": "E"}).json()
    assert rejected["status"] == "rejected"
    assert rejected["published"] is False
    assert rejected["active_version_before"] == good_version
    assert rejected["active_version_after"] == good_version
    assert rejected["lineage_version"] is None
    assert rejected["mappings_added"] == 0

    # 旧版仍在生效，映射未变
    versions = client.get("/api/lineage").json()["versions"]
    assert len(versions) == 1
    assert versions[0]["lineage_version"] == good_version
    assert versions[0]["is_current"] is True
    assert client.get(f"/api/lineage/versions/{good_version}").status_code == 200

    # 结果页与下载方案仍对应旧版
    assert client.get(f"/lineage/result/{good_version}").status_code == 200
    assert good_version in client.get(
        f"/api/lineage/versions/{good_version}/read-plan.sql").text

    attempt_id = rejected["attempt_id"]
    attempt_page = client.get(f"/lineage/attempt/{attempt_id}")
    assert attempt_page.status_code == 200
    assert "发布失败" in attempt_page.text or "发布被拒绝" in attempt_page.text
    attempt = client.get(f"/api/lineage/attempts/{attempt_id}").json()
    assert attempt["status"] == "rejected"
    assert attempt["active_version_after"] == good_version

    history = client.get("/lineage/history")
    assert history.status_code == 200
    assert good_version in history.text and attempt_id in history.text


def test_rejected_publish_retried_after_resolution_creates_one_new_version():
    good = publish({"sample": "D"}).json()
    rejected = publish({"sample": "E"}).json()
    assert rejected["status"] == "rejected"

    # 用同一批输入、补上身份冲突决议后重跑：类型收窄仍阻断
    attempt = client.get(f"/api/lineage/attempts/{rejected['attempt_id']}").json()
    conflict_key = next(a["key"] for a in attempt["build"]["ambiguities"]
                        if a["code"] == "identity_conflict")
    sample = client.get("/api/lineage/samples/E").json()
    resolutions = dict(sample["resolutions"])
    resolutions[conflict_key] = "follow_name"
    still = publish({"sample": "E", "resolutions": resolutions}).json()
    assert still["status"] == "rejected"
    assert still["active_version_after"] == good["lineage_version"]

    versions = client.get("/api/lineage").json()["versions"]
    assert len(versions) == 1


def test_new_batch_supersedes_but_keeps_old_snapshot_downloadable():
    sample = client.get("/api/lineage/samples/D").json()
    versions = sample["versions"][:2]
    first = publish({"table": sample["table"], "versions": versions,
                     "resolutions": sample["resolutions"]}).json()
    assert first["status"] == "published"

    second = publish({"sample": "D"}).json()
    assert second["status"] == "published"
    assert second["lineage_version"] != first["lineage_version"]
    assert second["active_version_before"] == first["lineage_version"]

    history = client.get("/api/lineage").json()["versions"]
    assert {row["lineage_version"] for row in history} == {
        first["lineage_version"], second["lineage_version"]}
    assert [row["lineage_version"] for row in history if row["is_current"]] == [
        second["lineage_version"]]
    # 旧版结果页明确标注为历史版本
    old_page = client.get(f"/lineage/result/{first['lineage_version']}")
    assert old_page.status_code == 200
    assert "历史版本" in old_page.text
    # 两个版本的下载方案内容不同，且各自对应自己的版本
    old_sql = client.get(f"/api/lineage/versions/{first['lineage_version']}/read-plan.sql").text
    new_sql = client.get(f"/api/lineage/versions/{second['lineage_version']}/read-plan.sql").text
    assert first["lineage_version"] in old_sql
    assert second["lineage_version"] in new_sql
    assert old_sql != new_sql


def test_missing_lineage_version_and_attempt_return_404():
    assert client.get("/api/lineage/versions/lin-000000000000").status_code == 404
    assert client.get("/lineage/result/lin-000000000000").status_code == 404
    assert client.get("/api/lineage/attempts/att-20990101T000000-abcdef").status_code == 404
    assert client.get("/lineage/attempt/att-20990101T000000-abcdef").status_code == 404
    assert client.get("/api/lineage/versions/bogus/read-plan.sql").status_code == 404
