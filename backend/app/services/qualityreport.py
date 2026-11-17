"""采集质量业务规则。

链路：抓取作业 URL 结果（crawl_url_result）→ 字段提取覆盖率（contentextract）
→ 质量报告指标冻结（quality_snapshot）→ 确认/归档 → 回写字段提取侧清单。

关键约束（与需求逐条对应）：
- 报告登记与生成的全部统计字段都落库，不只停留在页面；
- 状态机强制：未生成完成（无冻结快照）的报告不能确认，未确认不能归档；
- 同一作业并发生成只允许一个结论，重复提交按 request_id 幂等；
- 生成回调按 callback_id 幂等，重复到达不重复累计指标；
- 确认后快照冻结，字段覆盖率写入若命中已确认版本会被拒绝，旧指标进不了已确认版本；
- 归档后重试生成保留上一有效版本，版本冲突时历史确认结论优先、新一轮结果另建快照；
- 报告引用的字段覆盖率绑定提取版本，读快照时复核漂移。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.locks import locks
from app.store import store

MODULE = "qualityreport"
SNAPSHOT_MODULE = "quality_snapshot"
URL_RESULT_MODULE = "crawl_url_result"
CONTENT_MODULE = "contentextract"
CRAWLJOB_MODULE = "crawljob"

REQUIRED_FIELDS = ["报告编号", "作业编号"]
# 登记时允许持久化的全部业务字段（统计字段不再被静默丢弃）。
PERSIST_FIELDS = [
    "报告编号", "作业编号", "提取器编号",
    "总URL数", "成功URL数", "失败URL数", "内容完整率", "字段覆盖率",
    "报告生成时间",
]
# 草稿 → 生成中 → 已生成 → 已确认 → 已归档
STATUS_ORDER = ["草稿", "生成中", "已生成", "已确认", "已归档"]
# 冲突处置优先级：已确认/已归档的历史结论优先保留。
CONFIRMED_STATUSES = ("已确认", "已归档")
# 合格判定阈值。
PASS_RATE = 0.9
COVERAGE_PASS_RATE = 0.8
ACCEPTABLE_RATE = 0.7


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _round4(value: float) -> float:
    return round(value + 1e-12, 4)


class QualityreportService:
    # ------------------------------------------------------------------ 查询
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        job_no: str | None = None,
        conclusion: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("报告编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if job_no:
            rows = [row for row in rows if job_no in str(row.get("作业编号", ""))]
        if conclusion:
            rows = [row for row in rows if conclusion in str(row.get("质量结论", ""))]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [dict(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return dict(row) if row else None

    def list_checklist(
        self,
        *,
        job_no: str | None = None,
        report_no: str | None = None,
        confirm_state: str | None = None,
    ) -> list[dict[str, Any]]:
        """作业质量清单数据集：每个冻结/待确认快照一行，可按作业与确认状态复核。"""
        rows = store.rows(SNAPSHOT_MODULE)
        if job_no:
            rows = [row for row in rows if row.get("作业编号") == job_no]
        if report_no:
            rows = [row for row in rows if row.get("报告编号") == report_no]
        if confirm_state:
            rows = [row for row in rows if row.get("确认状态") == confirm_state]
        return [dict(row) for row in rows]

    def get_snapshot(self, entry_id: int, version: int) -> dict[str, Any] | None:
        """读取单个快照，并附上与当前提取侧版本的漂移复核结果。"""
        report = store.find(MODULE, entry_id)
        if report is None:
            return None
        snapshot = next(
            (row for row in store.rows(SNAPSHOT_MODULE)
             if row.get("报告编号") == report.get("报告编号") and int(row.get("快照版本", 0)) == version),
            None,
        )
        if snapshot is None:
            return None
        detail = dict(snapshot)
        extractor = self._find_extractor(str(snapshot.get("提取器编号") or ""))
        drift: list[str] = []
        if extractor is None:
            drift.append(f"提取器 {snapshot.get('提取器编号')} 已不存在，无法复核当前覆盖率")
        else:
            if int(extractor.get("提取器版本", 0)) != int(snapshot.get("提取器版本", 0)):
                drift.append(
                    f"提取侧当前版本为 v{extractor.get('提取器版本')}，"
                    f"快照引用 v{snapshot.get('提取器版本')}，覆盖率已发生版本漂移；"
                    f"快照仍以冻结值 {snapshot.get('字段覆盖率')} 为准"
                )
            elif float(extractor.get("字段覆盖率", 0)) != float(snapshot.get("字段覆盖率", 0)):
                drift.append("同版本提取侧覆盖率与快照冻结值不一致，以快照冻结值为准")
        # 来源 URL 结果，供外部按快照重新核对数量关系。
        detail["来源URL结果"] = [
            dict(row) for row in store.rows(URL_RESULT_MODULE)
            if row.get("作业编号") == snapshot.get("作业编号")
            and int(row.get("作业版本", 0)) == int(snapshot.get("作业版本", 0))
        ]
        detail["漂移复核"] = drift
        return detail

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        report_no = str(values.get("报告编号") or "").strip()
        if any(row.get("报告编号") == report_no for row in rows):
            return None, [f"报告编号 {report_no} 已存在"]
        with locks.get(MODULE):
            entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            for field in PERSIST_FIELDS:
                entry[field] = values.get(field)
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            # 版本与生成编排字段。
            entry["作业版本"] = None
            entry["提取器版本"] = None
            entry["质量结论"] = "待生成"
            entry["当前快照版本"] = None
            entry["生成状态"] = "未生成"
            entry["进行中请求"] = None
            entry["已处理请求"] = []
            entry["已处理回调"] = []
            entry["版本冲突"] = []
            entry["快照"] = []
            entry["回写流水"] = []
            entry["数据指纹"] = None
            rows.append(entry)
            return dict(entry), []

    # ------------------------------------------------------------------ 动作
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "生成报告":
            return self.start_generation(entry_id)
        if action == "确认报告":
            return self.confirm(entry_id)
        if action == "归档报告":
            return self.archive(entry_id)
        return None, f"动作「{action}」不属于采集质量可执行范围"

    # ------------------------------------------------------------ 生成（核心）
    def start_generation(
        self,
        entry_id: int,
        *,
        request_id: str | None = None,
        extractor_no: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """生成质量结论与作业质量清单；同作业并发只成一个，重复提交幂等。"""
        with locks.quality_then_content():
            report = store.find(MODULE, entry_id)
            if report is None:
                return None, f"质量报告 {entry_id} 不存在或已归档"

            # 幂等：同一 request_id 重放直接返回既有结果，不重新累计。
            if request_id and request_id in report.get("已处理请求", []):
                return dict(report), "生成请求已处理，按幂等返回既有质量结论"
            if request_id and report.get("进行中请求") == request_id:
                return dict(report), "同一生成请求仍在处理中，按幂等返回当前报告"

            # 并发互斥：同一报告（同一作业结论）同时只允许一个生成流程。
            if report.get("生成状态") == "生成中" and report.get("进行中请求"):
                return None, "采集质量正在并发生成，同一作业只能生成一个质量结论，请稍后重试"
            if report.get("status") == "已确认":
                return None, "报告已确认，历史结论已冻结；如需新一轮抓取结果请先归档再重试生成"

            job_no = str(report.get("作业编号") or "").strip()
            if not any(row.get("作业编号") == job_no for row in store.rows(CRAWLJOB_MODULE)):
                return None, f"抓取作业 {job_no} 不存在，无法读取 URL 抓取结果"
            url_rows = [row for row in store.rows(URL_RESULT_MODULE) if row.get("作业编号") == job_no]
            if not url_rows:
                return None, f"作业 {job_no} 暂无 URL 抓取结果，质量报告无法生成"

            # 数据版本幂等：底层数据未变化时，重复/并发提交直接返回既有结论，
            # 保证同一作业版本只产生一个质量结论与一份清单，不重复累计。
            fingerprint = self._data_fingerprint(report, url_rows, extractor_no)
            if report.get("当前快照版本") is not None and report.get("数据指纹") == fingerprint:
                if request_id and request_id not in report.get("已处理请求", []):
                    report["已处理请求"] = [*report.get("已处理请求", []), request_id]
                return dict(report), "抓取结果与提取覆盖率未变化，按幂等返回既有质量结论"

            request_key = request_id or f"req-{report.get('报告编号')}-{_now()}"
            report["进行中请求"] = request_key
            report["status"] = "生成中"
            report["生成状态"] = "生成中"
            report["abnormal"] = True
            snapshot_table = store.rows(SNAPSHOT_MODULE)
            snapshot_count_before = len(snapshot_table)
            try:
                snapshot, message = self._build_snapshot(report, url_rows, extractor_no)
            except _GenerationError as exc:
                # 业务校验失败：回退到可重试状态，并清掉本次可能残留的半截快照。
                del snapshot_table[snapshot_count_before:]
                self._reset_after_failure(report)
                return None, str(exc)
            except Exception as exc:  # noqa: BLE001 - 任何异常都不能让报告永久卡在生成中
                del snapshot_table[snapshot_count_before:]
                self._reset_after_failure(report)
                return None, f"生成过程出现异常，已回退到可重试状态：{exc}"

            # 指标冻结并落库到报告主记录（页面刷新后仍然一致）。
            self._apply_snapshot_to_report(report, snapshot)
            report["数据指纹"] = fingerprint
            report["进行中请求"] = None
            report["已处理请求"] = [*report.get("已处理请求", []), request_key]
            report["生成状态"] = "完成"
            report["status"] = "已生成"
            report["abnormal"] = False
            report["pending"] = True
            # 回写字段提取侧清单（生成完成阶段，带幂等流水）。
            self._writeback_extractor(report, snapshot, stage="生成完成")
            return dict(report), message

    def _data_fingerprint(
        self,
        report: dict[str, Any],
        url_rows: list[dict[str, Any]],
        extractor_no: str | None,
    ) -> str:
        """抓取结果 + 提取侧版本的口径指纹：相同指纹视为同一轮数据，不重复生成。"""
        job_version = max(int(row.get("作业版本", 0)) for row in url_rows)
        version_rows = [row for row in url_rows if int(row.get("作业版本", 0)) == job_version]
        success = sum(1 for row in version_rows if row.get("抓取结果") == "成功")
        failed = sum(1 for row in version_rows if row.get("抓取结果") == "失败")
        wanted = extractor_no or str(report.get("提取器编号") or "").strip() or None
        if wanted:
            extractor = self._find_extractor(wanted)
            ext_key = f"{wanted}#{extractor.get('提取器版本')}#{extractor.get('字段覆盖率')}" if extractor else "missing"
        else:
            matched = [row for row in store.rows(CONTENT_MODULE)
                       if row.get("关联作业编号") == report.get("作业编号")]
            if matched:
                latest = sorted(matched, key=lambda row: int(row.get("提取器版本", 0)))[-1]
                ext_key = f"{latest.get('提取器编号')}#{latest.get('提取器版本')}#{latest.get('字段覆盖率')}"
            else:
                ext_key = "missing"
        return f"{report.get('作业编号')}|jobv{job_version}|n{len(version_rows)}|ok{success}|fail{failed}|{ext_key}"

    def finish_callback(
        self,
        entry_id: int,
        *,
        callback_id: str | None,
        request_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """生成回调登记：重复回调不重复累计指标。"""
        with locks.quality_then_content():
            report = store.find(MODULE, entry_id)
            if report is None:
                return None, f"质量报告 {entry_id} 不存在或已归档"
            if report.get("生成状态") != "完成":
                return None, "报告尚未生成完成，回调不能被确认，请先完成生成报告"
            if callback_id and callback_id in report.get("已处理回调", []):
                return dict(report), "生成回调重复到达，已按幂等忽略，指标未重复累计"
            if callback_id:
                report["已处理回调"] = [*report.get("已处理回调", []), callback_id]
            # 回调只登记到达事实，不重算任何指标。
            return dict(report), "生成回调已登记"

    def confirm(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        with locks.quality_then_content():
            report = store.find(MODULE, entry_id)
            if report is None:
                return None, f"质量报告 {entry_id} 不存在或已归档"
            if report.get("生成状态") != "完成" or report.get("当前快照版本") is None:
                return None, "未生成完成的报告不能确认，请先执行生成报告并等待指标冻结"
            if report.get("status") != "已生成":
                return None, f"当前状态为「{report.get('status')}」，只有已生成的报告才能确认"
            version = int(report["当前快照版本"])
            snapshot = self._find_snapshot(str(report["报告编号"]), version)
            # 确认即冻结：此后覆盖率写入命中该版本会被提取侧拒绝。
            snapshot["确认状态"] = "已确认"
            snapshot["冻结"] = True
            snapshot["确认时间"] = _now()
            report["status"] = "已确认"
            report["pending"] = False
            report["abnormal"] = False
            self._sync_embedded_snapshot(report, snapshot)
            self._writeback_extractor(report, snapshot, stage="确认报告")
            return dict(report), "质量报告已确认，指标快照已冻结"

    def archive(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        with locks.get(MODULE):
            report = store.find(MODULE, entry_id)
            if report is None:
                return None, f"质量报告 {entry_id} 不存在或已归档"
            if report.get("status") != "已确认":
                return None, f"当前状态为「{report.get('status')}」，只有已确认的报告才能归档"
            report["status"] = "已归档"
            report["pending"] = False
            return dict(report), "质量报告已归档，历史结论与清单已保留"

    # ------------------------------------------------------------ 内部计算
    @staticmethod
    def _reset_after_failure(report: dict[str, Any]) -> None:
        """生成失败后统一回退：清除在途标记，回到可重试状态，已冻结指标不动。"""
        report["进行中请求"] = None
        report["生成状态"] = "失败"
        report["status"] = "草稿" if report.get("当前快照版本") is None else "已生成"
        report["abnormal"] = True

    def _build_snapshot(
        self,
        report: dict[str, Any],
        url_rows: list[dict[str, Any]],
        extractor_no: str | None,
    ) -> tuple[dict[str, Any], str]:
        job_no = str(report["作业编号"])
        # 取该作业最新一轮抓取版本；历史版本仍保留在 URL 结果表里。
        job_version = max(int(row.get("作业版本", 0)) for row in url_rows)
        version_rows = [
            row for row in url_rows if int(row.get("作业版本", 0)) == job_version
        ]
        total = len(version_rows)
        success = sum(1 for row in version_rows if row.get("抓取结果") == "成功")
        failed = sum(1 for row in version_rows if row.get("抓取结果") == "失败")

        # 数量关系校验：成功 + 失败必须等于总数，否则结论不成立、不允许生成。
        checks = [f"成功URL数({success}) + 失败URL数({failed}) = 总URL数({total})"]
        if success + failed != total or total == 0:
            raise _GenerationError(
                f"作业 {job_no} v{job_version} 数量关系校验失败：成功 {success} + 失败 {failed} ≠ 总数 {total}"
            )

        extractor = self._resolve_extractor(report, extractor_no, job_no)
        extractor_version = int(extractor["提取器版本"])
        field_coverage = round(float(extractor["字段覆盖率"]), 4)
        completeness = _round4(success / total)
        conclusion = self._conclude(completeness, field_coverage, failed)

        # 版本冲突记录：报告已有确认/归档快照，而新一轮抓取版本不同。
        conflict_notes: list[dict[str, Any]] = list(report.get("版本冲突", []))
        prior_confirmed = [
            snap for snap in store.rows(SNAPSHOT_MODULE)
            if snap.get("报告编号") == report.get("报告编号")
            and snap.get("确认状态") in CONFIRMED_STATUSES
        ]
        if prior_confirmed and any(int(s.get("作业版本", 0)) != job_version for s in prior_confirmed):
            note = {
                "记录时间": _now(),
                "历史作业版本": [int(s.get("作业版本", 0)) for s in prior_confirmed],
                "新一轮作业版本": job_version,
                "处置": "历史已确认结论优先保留，新一轮抓取结果另建快照，两组版本依据并存",
            }
            conflict_notes.append(note)

        next_snapshot_version = job_version
        existing_versions = [
            int(row.get("快照版本", 0))
            for row in store.rows(SNAPSHOT_MODULE)
            if row.get("报告编号") == report.get("报告编号")
        ]
        if existing_versions:
            # 同一作业版本重试（例如归档后重试）：快照版本递增但保留上一有效版本。
            next_snapshot_version = max(max(existing_versions) + 1, job_version)

        snapshot: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in store.rows(SNAPSHOT_MODULE)), default=0) + 1,
            "报告编号": report["报告编号"],
            "快照版本": next_snapshot_version,
            "作业编号": job_no,
            "作业版本": job_version,
            "提取器编号": extractor["提取器编号"],
            "提取器版本": extractor_version,
            "总URL数": total,
            "成功URL数": success,
            "失败URL数": failed,
            "内容完整率": completeness,
            "字段覆盖率": field_coverage,
            "质量结论": conclusion,
            "确认状态": "已生成",
            "冻结": False,
            "生成时间": _now(),
            "确认时间": None,
            "来源URL结果": [
                {"URL": row.get("URL"), "抓取结果": row.get("抓取结果"),
                 "HTTP状态码": row.get("HTTP状态码"), "失败原因码": row.get("失败原因码")}
                for row in version_rows
            ],
            "校验": checks,
        }
        store.rows(SNAPSHOT_MODULE).append(snapshot)
        report["版本冲突"] = conflict_notes
        return snapshot, f"质量结论已生成：{conclusion}（作业 v{job_version}，快照 v{next_snapshot_version}）"

    def _apply_snapshot_to_report(self, report: dict[str, Any], snapshot: dict[str, Any]) -> None:
        for field in ("总URL数", "成功URL数", "失败URL数", "内容完整率", "字段覆盖率"):
            report[field] = snapshot[field]
        report["作业版本"] = snapshot["作业版本"]
        report["提取器编号"] = snapshot["提取器编号"]
        report["提取器版本"] = snapshot["提取器版本"]
        report["质量结论"] = snapshot["质量结论"]
        report["当前快照版本"] = snapshot["快照版本"]
        report["报告生成时间"] = snapshot["生成时间"]
        self._sync_embedded_snapshot(report, snapshot)

    @staticmethod
    def _sync_embedded_snapshot(report: dict[str, Any], snapshot: dict[str, Any]) -> None:
        """报告内嵌快照摘要与快照表保持一致（清单数据集以快照表为准）。"""
        summary = {k: snapshot.get(k) for k in (
            "快照版本", "作业版本", "提取器版本", "总URL数", "成功URL数", "失败URL数",
            "内容完整率", "字段覆盖率", "质量结论", "确认状态", "冻结", "生成时间", "确认时间",
        )}
        embedded = [item for item in report.get("快照", []) if item.get("快照版本") != snapshot["快照版本"]]
        embedded.append(summary)
        embedded.sort(key=lambda item: int(item.get("快照版本", 0)))
        report["快照"] = embedded

    def _resolve_extractor(
        self,
        report: dict[str, Any],
        extractor_no: str | None,
        job_no: str,
    ) -> dict[str, Any]:
        wanted = (
            extractor_no
            or str(report.get("提取器编号") or "").strip()
            or None
        )
        if wanted:
            extractor = self._find_extractor(wanted)
            if extractor is None:
                raise _GenerationError(f"提取器 {wanted} 不存在，字段覆盖率无版本依据")
        else:
            matched = [
                row for row in store.rows(CONTENT_MODULE)
                if row.get("关联作业编号") == job_no
            ]
            if not matched:
                raise _GenerationError(f"作业 {job_no} 未绑定内容提取器，无法获取字段覆盖率")
            extractor = sorted(matched, key=lambda row: int(row.get("提取器版本", 0)))[-1]
            report["提取器编号"] = extractor["提取器编号"]
        coverage = extractor.get("字段覆盖率")
        if coverage is None:
            raise _GenerationError(
                f"提取器 {extractor.get('提取器编号')} 尚未提供字段覆盖率，不能生成报告"
            )
        return extractor

    @staticmethod
    def _find_extractor(extractor_no: str) -> dict[str, Any] | None:
        return next(
            (row for row in store.rows(CONTENT_MODULE) if row.get("提取器编号") == extractor_no),
            None,
        )

    @staticmethod
    def _find_snapshot(report_no: str, version: int) -> dict[str, Any]:
        snapshot = next(
            row for row in store.rows(SNAPSHOT_MODULE)
            if row.get("报告编号") == report_no and int(row.get("快照版本", 0)) == version
        )
        return snapshot

    @staticmethod
    def _conclude(completeness: float, field_coverage: float, failed: int) -> str:
        if failed == 0 and completeness >= PASS_RATE and field_coverage >= COVERAGE_PASS_RATE:
            return "合格"
        if completeness >= ACCEPTABLE_RATE and field_coverage >= ACCEPTABLE_RATE:
            return "待改进"
        return "不合格"

    def _writeback_extractor(
        self,
        report: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        stage: str,
    ) -> None:
        """把质量结论与作业质量清单回写到字段提取侧清单，幂等流水防重复累计。"""
        extractor = self._find_extractor(str(snapshot.get("提取器编号") or ""))
        if extractor is None:
            return
        ledger = extractor.setdefault("质量回写流水", [])
        version = int(snapshot["快照版本"])
        if any(item.get("报告编号") == report["报告编号"]
               and int(item.get("快照版本", 0)) == version
               and item.get("阶段") == stage for item in ledger):
            return
        record = {
            "报告编号": report["报告编号"],
            "快照版本": version,
            "阶段": stage,
            "质量结论": snapshot["质量结论"],
            "字段覆盖率": snapshot["字段覆盖率"],
            "回写时间": _now(),
        }
        ledger.append(record)
        extractor["最新质量结论"] = snapshot["质量结论"]
        extractor["最近质量回写"] = {
            "报告编号": report["报告编号"],
            "快照版本": version,
            "阶段": stage,
        }
        report.setdefault("回写流水", []).append(
            {"快照版本": version, "阶段": stage, "质量结论": snapshot["质量结论"], "回写时间": record["回写时间"]}
        )

    # ------------------------------------------------------------ 数据接入
    def ingest_url_result(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """登记一条作业 URL 抓取结果（按 作业+版本+URL 幂等），作为报告数量口径来源。"""
        job_no = str(values.get("作业编号") or "").strip()
        url = str(values.get("URL") or "").strip()
        try:
            job_version = int(values.get("作业版本"))
        except (TypeError, ValueError):
            return None, "作业版本必须是整数"
        result_kind = str(values.get("抓取结果") or "").strip()
        if not job_no or not url:
            return None, "作业编号与 URL 必填"
        if result_kind not in ("成功", "失败"):
            return None, "抓取结果只能是「成功」或「失败」"
        with locks.get(MODULE):
            table = store.rows(URL_RESULT_MODULE)
            existed = next(
                (row for row in table
                 if row.get("作业编号") == job_no and row.get("URL") == url
                 and int(row.get("作业版本", 0)) == job_version),
                None,
            )
            if existed is not None:
                return dict(existed), "URL 结果已存在，按幂等忽略"
            ok = result_kind == "成功"
            entry = {
                "id": max((int(row.get("id", 0)) for row in table), default=0) + 1,
                "作业编号": job_no,
                "作业版本": job_version,
                "URL": url,
                "抓取结果": result_kind,
                "HTTP状态码": int(values.get("HTTP状态码") or (200 if ok else 500)),
                "失败原因码": values.get("失败原因码") if not ok else None,
                "采集时间": values.get("采集时间") or _now(),
            }
            table.append(entry)
            return dict(entry), "URL 抓取结果已登记"

    # ------------------------------------------------------------ 导出文件
    def build_conclusion_file(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """整理质量结论为可下载、可书签复核的文件载荷（数据仍取自冻结快照）。"""
        report = store.find(MODULE, entry_id)
        if report is None:
            return None, f"质量报告 {entry_id} 不存在或已归档"
        if report.get("当前快照版本") is None:
            return None, "报告尚未生成，暂无可下载的质量结论"
        snapshot = self._find_snapshot(str(report["报告编号"]), int(report["当前快照版本"]))
        payload = {
            "报告编号": report["报告编号"],
            "作业编号": report["作业编号"],
            "质量结论": snapshot["质量结论"],
            "确认状态": snapshot["确认状态"],
            "生成时间": snapshot["生成时间"],
            "确认时间": snapshot["确认时间"],
            "指标": {field: snapshot[field] for field in (
                "快照版本", "作业版本", "提取器编号", "提取器版本",
                "总URL数", "成功URL数", "失败URL数", "内容完整率", "字段覆盖率",
            )},
            "数量校验": snapshot.get("校验", []),
            "来源URL结果": snapshot.get("来源URL结果", []),
            "版本冲突": report.get("版本冲突", []),
        }
        lines = [
            f"质量结论：{payload['质量结论']}",
            f"报告编号：{payload['报告编号']}",
            f"作业编号：{payload['作业编号']}（作业版本 v{payload['指标']['作业版本']}）",
            f"提取器：{payload['指标']['提取器编号']} v{payload['指标']['提取器版本']}",
            f"总URL数：{payload['指标']['总URL数']}，成功：{payload['指标']['成功URL数']}，失败：{payload['指标']['失败URL数']}",
            f"内容完整率：{payload['指标']['内容完整率']}",
            f"字段覆盖率：{payload['指标']['字段覆盖率']}（与提取版本绑定）",
            f"确认状态：{payload['确认状态']}",
            f"数量校验：{'；'.join(payload['数量校验'])}",
        ]
        return {"json": payload, "text": "\n".join(lines)}, snapshot["质量结论"]


class _GenerationError(Exception):
    """生成流程内的可恢复业务错误：抛出后报告回退到可重试状态。"""


# 供其他模块（如内容提取侧覆盖率写入）复用的查询入口。
def find_confirmed_snapshot_versions(extractor_no: str, extractor_version: int) -> list[dict[str, Any]]:
    return [
        row for row in store.rows(SNAPSHOT_MODULE)
        if row.get("提取器编号") == extractor_no
        and int(row.get("提取器版本", 0)) == extractor_version
        and bool(row.get("冻结"))
    ]
