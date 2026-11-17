"""采集质量接口：质量报告生成、确认、归档、快照复核、清单数据集与结论文件下载。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.qualityreport import QualityreportService

router = APIRouter(prefix="/api/qualityreport", tags=["采集质量"])

service = QualityreportService()

LIST_FIELDS = ["报告编号", "作业编号", "总URL数", "成功URL数", "失败URL数", "内容完整率", "字段覆盖率", "报告生成时间"]
STATUSES = ["草稿", "生成中", "已生成", "已确认", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按报告编号检索"),
    status: str | None = Query(default=None, description="草稿、生成中、已生成、已确认、已归档"),
    job_no: str | None = Query(default=None, description="按作业编号检索"),
    conclusion: str | None = Query(default=None, description="按质量结论检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按报告编号、作业、结论与状态过滤采集质量列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, job_no=job_no,
        conclusion=conclusion, page=page, size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


# 注意：静态路径必须声明在 /{entry_id} 之前，否则会被动态路径吞掉。
@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集质量清单：返回全量报告（含快照摘要与版本冲突记录）。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "qualityreport", "total": total, "items": items}


@router.get("/checklist")
def quality_checklist(
    作业编号: str | None = Query(default=None, alias="job_no"),
    报告编号: str | None = Query(default=None, alias="report_no"),
    确认状态: str | None = Query(default=None, alias="confirm_state"),
) -> dict[str, Any]:
    """作业质量清单数据集：每个快照一行，支持书签收藏后直接复核。"""
    items = service.list_checklist(job_no=作业编号, report_no=报告编号, confirm_state=确认状态)
    return {"module": "quality_snapshot", "total": len(items), "items": items}


@router.post("/url-results", response_model=ActionResult)
def ingest_url_result(payload: EntryPayload) -> ActionResult:
    """登记作业 URL 抓取结果（新一轮抓取/失败 URL 更正的入口），按 作业+版本+URL 幂等。"""
    entry, message = service.ingest_url_result(payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条质量报告明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"质量报告 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条质量报告；统计字段随表单一并持久化，缺必填字段时说明原因。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段或编号冲突：{'、'.join(missing)}")
    return ActionResult(ok=True, message="质量报告已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行生成报告、确认报告、归档报告；状态机不满足时返回可读原因。"""
    action = str(payload.values.get("action") or "").strip()
    if action == "生成报告":
        entry, message = service.start_generation(
            entry_id,
            request_id=_optional_str(payload.values, "request_id"),
            extractor_no=_optional_str(payload.values, "提取器编号"),
        )
    elif action == "确认报告":
        entry, message = service.confirm(entry_id)
    elif action == "归档报告":
        entry, message = service.archive(entry_id)
    else:
        entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/callbacks", response_model=ActionResult)
def generation_callback(entry_id: int, payload: EntryPayload) -> ActionResult:
    """生成回调登记：重复 callback_id 幂等忽略，指标不会被重复累计。"""
    callback_id = _optional_str(payload.values, "callback_id")
    request_id = _optional_str(payload.values, "request_id")
    entry, message = service.finish_callback(
        entry_id, callback_id=callback_id, request_id=request_id
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/snapshots/{version}")
def get_snapshot(entry_id: int, version: int) -> dict[str, Any]:
    """按快照版本复核冻结指标与来源 URL 结果，并给出提取版本漂移说明。"""
    snapshot = service.get_snapshot(entry_id, version)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="快照不存在或报告不存在")
    return snapshot


@router.get("/{entry_id}/conclusion")
def conclusion_file(
    entry_id: int,
    format: str = Query(default="json", description="json 或 txt"),
) -> Any:
    """质量结论文件：json 可书签复核，txt 直接下载留存。"""
    payload, error = service.build_conclusion_file(entry_id)
    if payload is None:
        raise HTTPException(status_code=400, detail=error)
    if str(format).lower() == "txt":
        return PlainTextResponse(
            payload["text"],
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="quality-{entry_id}-conclusion.txt"'},
        )
    return payload["json"]


def _optional_str(values: dict[str, Any], key: str) -> str | None:
    value = values.get(key)
    text = str(value).strip() if value is not None else ""
    return text or None
