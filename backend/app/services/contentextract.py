"""内容提取业务规则：状态流转、字段校验、字段覆盖率写入与质量回写清单。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.locks import locks
from app.services.qualityreport import find_confirmed_snapshot_versions
from app.store import store

MODULE = "contentextract"
REQUIRED_FIELDS = ["提取器编号", "内容类型", "正文选择器"]
STATUS_ORDER = ["草稿", "提取器评审中", "已发布", "已废弃"]
ACTION_RULES = {"提交提取器评审": "提取器评审中", "发布提取器": "已发布", "废弃提取器": "已废弃"}
NEGATIVE_ACTIONS = []


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class ContentextractService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        job_no: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("提取器编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if job_no:
            rows = [row for row in rows if job_no in str(row.get("关联作业编号", ""))]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [dict(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return dict(row) if row else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["关联作业编号"] = values.get("关联作业编号")
        entry["提取器版本"] = int(values.get("提取器版本") or 1)
        entry["字段覆盖率"] = values.get("字段覆盖率")
        entry["最新质量结论"] = "待生成"
        entry["最近质量回写"] = None
        entry["质量回写流水"] = []
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"提取器 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于内容提取可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"提取器已{action}"

    def update_coverage(
        self,
        entry_id: int,
        *,
        coverage: Any,
        request_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """字段覆盖率写入。

        与确认并发时的保护：若当前提取版本已被已确认/已归档的质量快照引用（冻结），
        旧指标不能写进已确认版本——本次写入被拒绝，需以新版本提取器重新参与生成。
        重复 request_id 幂等返回，不产生重复流水。
        """
        try:
            coverage_value = round(float(coverage), 4)
        except (TypeError, ValueError):
            return None, "字段覆盖率必须是 0~1 之间的数值"
        if not 0 <= coverage_value <= 1:
            return None, "字段覆盖率必须落在 0~1 之间"

        with locks.quality_then_content():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"提取器 {entry_id} 不存在或已归档"
            ledger = entry.setdefault("覆盖率写入流水", [])
            if request_id:
                prior = next((item for item in ledger if item.get("request_id") == request_id), None)
                if prior is not None:
                    return dict(entry), "覆盖率写入请求重复到达，已按幂等忽略"

            extractor_no = str(entry.get("提取器编号"))
            extractor_version = int(entry.get("提取器版本", 1))
            frozen = find_confirmed_snapshot_versions(extractor_no, extractor_version)
            if frozen:
                refs = "、".join(f"{row.get('报告编号')}@v{row.get('快照版本')}" for row in frozen)
                return None, (
                    f"提取器 {extractor_no} v{extractor_version} 已被已确认质量快照（{refs}）冻结，"
                    "旧指标不能写进已确认版本；请升版提取器后再写入覆盖率"
                )

            old_value = entry.get("字段覆盖率")
            entry["字段覆盖率"] = coverage_value
            ledger.append({
                "request_id": request_id,
                "旧覆盖率": old_value,
                "新覆盖率": coverage_value,
                "写入时间": _now(),
            })
            return dict(entry), f"字段覆盖率已更新为 {coverage_value}"
