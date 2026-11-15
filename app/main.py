"""FastAPI 路由（薄）：页面渲染 + 少量 JSON API，核心规则全部在纯函数模块。"""
from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import lineage, lineage_report, lineage_store, report, schemaio, settings, store
from .lineage import CONFIDENCE_LABELS, LineageInputError
from .schemaio import SchemaError
from .types import DECIMAL_RULES, matrix_table

app = FastAPI(title=settings.APP_NAME)
app.mount("/static", StaticFiles(directory=str(settings.BASE_DIR / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(settings.BASE_DIR / "app" / "templates"))


def _ctx(request: Request, active: str, **extra) -> dict:
    ctx = {"app_name": settings.APP_NAME, "active": active, "matrix_version": settings.MATRIX_VERSION}
    ctx.update(extra)
    return ctx


def _json_body(value, label: str):
    """页面表单可能把 JSON 作为字符串提交；API 也接受原生 JSON。"""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise LineageInputError(f"{label} 为空，请粘贴 JSON 或选择样例")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LineageInputError(f"{label} 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列 {exc.msg}")
    return value


def _resolve_lineage_inputs(payload) -> tuple[str, list, dict, str, dict | None]:
    if not isinstance(payload, dict):
        raise LineageInputError("请求体必须是 JSON 对象")
    sample_key = str(payload.get("sample") or "").strip().upper()
    if sample_key:
        sample = schemaio.load_lineage_sample(sample_key)
        raw = payload.get("resolutions")
        if raw in (None, "", {}):
            resolutions = lineage.normalize_resolutions(sample["resolutions"])
        else:
            resolutions = lineage.normalize_resolutions(_json_body(raw, "resolutions"))
        return sample["table"], sample["versions"], resolutions, f"谱系样例 {sample_key}", sample
    versions = _json_body(payload.get("versions"), "versions")
    resolutions = lineage.normalize_resolutions(_json_body(payload.get("resolutions"), "resolutions"))
    table = payload.get("table")
    if table is None and isinstance(versions, list) and versions and isinstance(versions[0], dict):
        table = versions[0].get("table")
    return str(table or "").strip(), versions, resolutions, "自定义输入", None


def _index_stats(sample_count: int, history: list[dict]) -> dict:
    """总览页只读统计：全部从既有运行记录派生，不改动任何业务判定。"""
    steps = [int(r.get("summary", {}).get("sql_steps") or 0) for r in history]
    return {
        "sample_count": sample_count,
        "run_count": len(history),
        "full_count": sum(1 for r in history if r.get("summary", {}).get("level") == "full"),
        "backfill_count": sum(1 for r in history if int(r.get("summary", {}).get("backfill_count") or 0) > 0),
        "breaking_count": sum(1 for r in history if not r.get("summary", {}).get("can_execute", True)),
        "avg_steps": round(sum(steps) / len(steps), 1) if steps else 0.0,
    }


def _resolve_inputs(payload: dict) -> tuple[dict, dict, dict, str]:
    if not isinstance(payload, dict):
        raise SchemaError("请求体必须是 JSON 对象")
    sample_key = str(payload.get("sample") or "").strip().upper()
    if sample_key in settings.SAMPLE_KEYS:
        sample = schemaio.load_sample(sample_key)
        return sample["old"], sample["new"], sample["consumers"], f"样例 {sample_key}"
    old = schemaio.parse_schema(payload.get("old"), "旧 schema")
    new = schemaio.parse_schema(payload.get("new"), "新 schema")
    consumers = schemaio.parse_consumers(payload.get("consumers"), "数据文件清单")
    return old, new, consumers, "自定义输入"


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "app": "schemaevo", "matrix_version": settings.MATRIX_VERSION}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    samples = schemaio.load_sample_index()
    history = store.list_runs(limit=50)
    return templates.TemplateResponse(
        request, "index.html",
        _ctx(
            request, "/",
            page="index",
            samples=samples,
            recent=history[:3],
            stats=_index_stats(len(samples), history),
        ),
    )


@app.get("/workbench", response_class=HTMLResponse)
async def workbench(request: Request):
    key = (request.query_params.get("sample") or "A").strip().upper()
    if key not in settings.SAMPLE_KEYS:
        key = "A"
    sample = schemaio.load_sample(key)
    # 右侧面板「预演」：复用既有纯函数算一遍所选样例，不落库、不改接口。
    preview = report.build_run(sample["old"], sample["new"], sample["consumers"], f"样例 {key}")
    return templates.TemplateResponse(
        request, "workbench.html",
        _ctx(
            request, "/workbench",
            page="workbench",
            samples=schemaio.load_sample_index(),
            selected=key,
            sample=sample,
            preview=preview,
            old_text=sample["raw_old"],
            new_text=sample["raw_new"],
            consumers_text=sample["raw_consumers"],
        ),
    )


@app.post("/api/compare")
async def api_compare(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON，且 Content-Type 为 application/json"}, status_code=400)
    try:
        old, new, consumers, source = _resolve_inputs(payload)
        if old["table"] != new["table"]:
            raise SchemaError(f"新旧 schema 的表名不一致：{old['table']} vs {new['table']}")
        if consumers.get("table") and consumers["table"] != new["table"]:
            raise SchemaError(f"数据文件清单的表名 {consumers['table']} 与新 schema {new['table']} 不一致")
        run = report.build_run(old, new, consumers, source)
        run = store.save_run(run)
    except SchemaError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {
        "run_id": run["run_id"],
        "summary": run["summary"],
        "redirect": f"/result/{run['run_id']}",
    }


@app.get("/api/samples/{key}")
async def api_sample(key: str):
    try:
        sample = schemaio.load_sample(key)
    except SchemaError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=404)
    return {
        "key": sample["key"],
        "label": sample["label"],
        "description": sample["description"],
        "expected": sample["expected"],
        "old": sample["raw_old"],
        "new": sample["raw_new"],
        "consumers": sample["raw_consumers"],
    }


@app.get("/api/matrix")
async def api_matrix() -> dict:
    return {"version": settings.MATRIX_VERSION, "promotions": matrix_table(), "decimal_rules": DECIMAL_RULES}


@app.get("/api/runs")
async def api_runs() -> dict:
    rows = store.list_runs(limit=50)
    return {"count": len(rows), "runs": rows}


@app.get("/api/runs/{run_id}")
async def api_run(run_id: str):
    try:
        run = store.load_run(run_id)
    except KeyError:
        return JSONResponse({"detail": f"运行记录 {run_id} 不存在"}, status_code=404)
    return run


@app.get("/api/runs/{run_id}/migration.sql", response_class=PlainTextResponse)
async def api_sql(run_id: str):
    try:
        run = store.load_run(run_id)
    except KeyError:
        return PlainTextResponse(f"-- 运行记录 {run_id} 不存在\n", status_code=404)
    return PlainTextResponse(
        run["migration"]["sql_text"],
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{report.sql_download_name(run)}"'},
    )


@app.get("/result/{run_id}", response_class=HTMLResponse)
async def result_page(request: Request, run_id: str):
    try:
        run = store.load_run(run_id)
    except KeyError:
        return templates.TemplateResponse(
            request, "not_found.html",
            _ctx(
                request, "",
                page="not_found",
                run_id=run_id,
                max_runs=settings.MAX_RUNS_KEPT,
                error=f"运行记录 {run_id} 不存在（历史记录只保留最近 {settings.MAX_RUNS_KEPT} 次）",
            ),
            status_code=404,
        )
    return templates.TemplateResponse(
        request, "result.html",
        _ctx(request, "", page="result", run=run, overview=report.overview(run)),
    )


@app.get("/review", response_class=HTMLResponse)
async def review_page(request: Request):
    runs = store.list_runs(limit=50)
    latest = store.load_run(runs[0]["run_id"]) if runs else None
    return templates.TemplateResponse(
        request, "review.html",
        _ctx(
            request, "/review",
            page="review",
            runs=runs,
            run_count=len(runs),
            latest=latest,
            promotions=matrix_table(),
            decimal_rules=DECIMAL_RULES,
            sample_index=schemaio.load_sample_index(),
            max_runs=settings.MAX_RUNS_KEPT,
        ),
    )


# ---------------------------------------------------------------------------
# 字段谱系（多版本）：工作台预览 / 编译 / 发布 / 结果页 / 历史 / 读取方案下载
# ---------------------------------------------------------------------------

@app.get("/lineage", response_class=HTMLResponse)
async def lineage_workbench(request: Request):
    samples = schemaio.load_lineage_sample_index()
    key = (request.query_params.get("sample") or "D").strip().upper()
    if key not in {s.get("key") for s in samples}:
        key = samples[0]["key"] if samples else "D"
    sample = schemaio.load_lineage_sample(key)
    try:
        build = lineage.build(sample["table"], sample["versions"], sample["resolutions"])
        preview = lineage_report.compile_summary(build)
        preview.update({
            "version_timeline": lineage_report.version_timeline(build),
            "binding_matrix": lineage_report.binding_matrix(build),
            "read_plan_rows": lineage_report.read_plan_rows(build),
            "plan_sql": build["plan_sql"],
        })
        error = None
    except LineageInputError as exc:
        preview, error = None, str(exc)
    return templates.TemplateResponse(
        request, "lineage_workbench.html",
        _ctx(
            request, "/lineage",
            page="lineage",
            samples=samples,
            selected=key,
            sample=sample,
            versions_text=json.dumps(sample["versions"], ensure_ascii=False, indent=2),
            resolutions_text=json.dumps(sample["resolutions"], ensure_ascii=False, indent=2),
            preview=preview,
            error=error,
            current=lineage_store.active_version(sample["table"]),
        ),
    )


@app.post("/lineage/preview", response_class=HTMLResponse)
async def lineage_preview_page(request: Request):
    """工作台「重新预演」：把表单里的自定义版本序列服务端渲染成同一张工作台。"""
    form = await request.form()
    table = str(form.get("table") or "").strip()
    versions_text = str(form.get("versions") or "")
    resolutions_text = str(form.get("resolutions") or "")
    samples = schemaio.load_lineage_sample_index()
    key = str(form.get("sample") or "D").strip().upper()
    if key not in {s.get("key") for s in samples}:
        key = samples[0]["key"] if samples else "D"
    sample = schemaio.load_lineage_sample(key)
    try:
        versions = _json_body(versions_text, "versions")
        resolutions = _json_body(resolutions_text, "resolutions")
        build = lineage.build(table, versions, resolutions)
        preview = lineage_report.compile_summary(build)
        preview.update({
            "version_timeline": lineage_report.version_timeline(build),
            "binding_matrix": lineage_report.binding_matrix(build),
            "read_plan_rows": lineage_report.read_plan_rows(build),
            "plan_sql": build["plan_sql"],
        })
        error = None
    except (LineageInputError, SchemaError) as exc:
        preview, error = None, str(exc)
    return templates.TemplateResponse(
        request, "lineage_workbench.html",
        _ctx(
            request, "/lineage",
            page="lineage",
            samples=samples,
            selected=key,
            sample={**sample, "table": table or sample["table"]},
            versions_text=versions_text,
            resolutions_text=resolutions_text,
            preview=preview,
            error=error,
            current=lineage_store.active_version(table or sample["table"]),
        ),
    )


@app.post("/api/lineage/preview")
async def api_lineage_preview(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON，且 Content-Type 为 application/json"}, status_code=400)
    try:
        table, versions, resolutions, source, _ = _resolve_lineage_inputs(payload)
        build = lineage.build(table, versions, resolutions)
    except (LineageInputError, SchemaError) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    summary = lineage_report.compile_summary(build)
    summary.update({
        "version_timeline": lineage_report.version_timeline(build),
        "binding_matrix": lineage_report.binding_matrix(build),
        "read_plan_rows": lineage_report.read_plan_rows(build),
        "plan_sql": build["plan_sql"],
        "source": source,
    })
    return summary


@app.post("/api/lineage/publish")
async def api_lineage_publish(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON，且 Content-Type 为 application/json"}, status_code=400)
    try:
        table, versions, resolutions, source, _ = _resolve_lineage_inputs(payload)
        build = lineage.build(table, versions, resolutions)
        outcome = lineage_store.publish(build, source)
    except (LineageInputError, SchemaError) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    body = dict(outcome)
    body["data"] = lineage_report.compile_summary(build)
    # 发布被拒绝不是 4xx：输入合法，但业务上禁止发布，旧版继续生效
    body["redirect"] = outcome["redirect"]
    return JSONResponse(body, status_code=200)


@app.get("/lineage/history", response_class=HTMLResponse)
async def lineage_history(request: Request):
    versions = lineage_store.list_published(limit=50)
    attempts = lineage_store.list_attempts(limit=50)
    tables = {}
    for row in versions + attempts:
        if row.get("table"):
            tables.setdefault(row["table"], lineage_store.active_version(row["table"]))
    return templates.TemplateResponse(
        request, "lineage_history.html",
        _ctx(
            request, "/lineage/history",
            page="lineage_history",
            versions=versions,
            attempts=attempts,
            active_by_table=tables,
            max_kept=settings.MAX_LINEAGE_KEPT,
            samples=schemaio.load_lineage_sample_index(),
        ),
    )


@app.get("/lineage/result/{lineage_version}", response_class=HTMLResponse)
async def lineage_result(request: Request, lineage_version: str):
    try:
        snapshot = lineage_store.load_published(lineage_version)
    except KeyError:
        return templates.TemplateResponse(
            request, "not_found.html",
            _ctx(request, "", page="not_found", run_id=lineage_version,
                 max_runs=settings.MAX_LINEAGE_KEPT,
                 error=f"谱系版本 {lineage_version} 不存在（历史保留最近 {settings.MAX_LINEAGE_KEPT} 版）"),
            status_code=404,
        )
    current = lineage_store.active_version(snapshot["table"]) or {}
    return templates.TemplateResponse(
        request, "lineage_result.html",
        _ctx(
            request, "", page="lineage_result",
            snapshot=snapshot,
            is_current=current.get("lineage_version") == lineage_version,
            active_version=current.get("lineage_version"),
            confidence_labels=CONFIDENCE_LABELS,
            version_timeline=lineage_report.version_timeline(snapshot),
            binding_matrix=lineage_report.binding_matrix(snapshot),
            read_plan_rows=lineage_report.read_plan_rows(snapshot),
            download_name=lineage_report.plan_download_name(snapshot["table"], lineage_version),
        ),
    )


@app.get("/lineage/attempt/{attempt_id}", response_class=HTMLResponse)
async def lineage_attempt(request: Request, attempt_id: str):
    try:
        attempt = lineage_store.load_attempt(attempt_id)
    except KeyError:
        return templates.TemplateResponse(
            request, "not_found.html",
            _ctx(request, "", page="not_found", run_id=attempt_id,
                 max_runs=settings.MAX_LINEAGE_KEPT,
                 error=f"发布尝试 {attempt_id} 不存在"),
            status_code=404,
        )
    return templates.TemplateResponse(
        request, "lineage_attempt.html",
        _ctx(
            request, "", page="lineage_attempt",
            attempt=attempt,
            build=attempt.get("build") or {},
            ambiguity_rows=lineage_report.ambiguity_rows(attempt.get("build") or {}),
        ),
    )


@app.get("/api/lineage/samples/{key}")
async def api_lineage_sample(key: str):
    try:
        sample = schemaio.load_lineage_sample(key)
    except SchemaError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=404)
    return {
        "key": sample["key"],
        "label": sample["label"],
        "table": sample["table"],
        "expected": sample["expected"],
        "description": sample["description"],
        "versions": sample["versions"],
        "resolutions": sample["resolutions"],
    }


@app.get("/api/lineage")
async def api_lineage_versions() -> dict:
    rows = lineage_store.list_published(limit=50)
    return {"count": len(rows), "versions": rows}


@app.get("/api/lineage/versions/{lineage_version}")
async def api_lineage_version(lineage_version: str):
    try:
        return lineage_store.load_published(lineage_version)
    except KeyError:
        return JSONResponse({"detail": f"谱系版本 {lineage_version} 不存在"}, status_code=404)


@app.get("/api/lineage/attempts")
async def api_lineage_attempts() -> dict:
    rows = lineage_store.list_attempts(limit=50)
    return {"count": len(rows), "attempts": rows}


@app.get("/api/lineage/attempts/{attempt_id}")
async def api_lineage_attempt(attempt_id: str):
    try:
        return lineage_store.load_attempt(attempt_id)
    except KeyError:
        return JSONResponse({"detail": f"发布尝试 {attempt_id} 不存在"}, status_code=404)


@app.get("/api/lineage/versions/{lineage_version}/read-plan.sql", response_class=PlainTextResponse)
async def api_lineage_plan_sql(lineage_version: str):
    try:
        snapshot = lineage_store.load_published(lineage_version)
    except KeyError:
        return PlainTextResponse(f"-- 谱系版本 {lineage_version} 不存在\n", status_code=404)
    return PlainTextResponse(
        snapshot["plan_sql"],
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": (
            f'attachment; filename="{lineage_report.plan_download_name(snapshot["table"], lineage_version)}"'
        )},
    )
