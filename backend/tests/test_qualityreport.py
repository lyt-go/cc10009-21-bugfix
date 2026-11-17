"""采集质量链路服务级测试（仅标准库，运行：.venv/bin/python -m unittest discover tests）。

覆盖：字段持久化、状态机、数量校验、指标冻结、并发幂等、确认冻结与覆盖率写入互斥、
回调幂等、提取侧回写、版本冲突与快照保留、漂移复核、结论文件、失败回退。
"""
from __future__ import annotations

import threading
import unittest

from app.services.contentextract import ContentextractService
from app.services.qualityreport import QualityreportService
from app.store import store


class QualityChainTestCase(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.quality = QualityreportService()
        self.content = ContentextractService()
        # 独立作业与提取器，避免依赖种子数据的具体取值。
        self.job_no = "CRAW-UT-0001"
        store.rows("crawljob").append({
            "id": 9001, "status": "执行中", "pending": False, "abnormal": False,
            "作业编号": self.job_no, "目标站点": "单测站", "起始URL": "https://ut.invalid",
        })
        self.extractor_no = "CONT-UT-0001"
        store.rows("contentextract").append({
            "id": 9001, "status": "已发布", "pending": False, "abnormal": False,
            "提取器编号": self.extractor_no, "内容类型": "文章", "正文选择器": "article",
            "关联作业编号": self.job_no, "提取器版本": 1, "字段覆盖率": 0.82,
            "最新质量结论": "待生成", "最近质量回写": None, "质量回写流水": [],
        })
        for i in range(1, 9):
            self._ingest(i, "成功", 200)
        self._ingest(9, "失败", 500, "HTTP_500")
        self._ingest(10, "失败", 504, "GATEWAY_TIMEOUT")

    def _ingest(self, index: int, result: str, code: int, reason: str | None = None, version: int = 1) -> None:
        entry, message = self.quality.ingest_url_result({
            "作业编号": self.job_no, "作业版本": version,
            "URL": f"https://ut.invalid/v{version}/{index}", "抓取结果": result,
            "HTTP状态码": code, "失败原因码": reason,
        })
        self.assertIsNotNone(entry, message)

    def _create_report(self, report_no: str = "QUAL-UT-0001") -> int:
        entry, missing = self.quality.create_entry({
            "报告编号": report_no, "作业编号": self.job_no,
            "提取器编号": self.extractor_no,
        })
        self.assertEqual(missing, [])
        return int(entry["id"])

    # ------------------------------------------------------------ 1. 持久化与状态机
    def test_stat_fields_persisted_and_confirm_requires_generation(self) -> None:
        entry, missing = self.quality.create_entry({
            "报告编号": "QUAL-UT-P", "作业编号": self.job_no,
            "成功URL数": 7, "失败URL数": 3, "内容完整率": 0.7, "字段覆盖率": 0.66,
        })
        self.assertEqual(missing, [])
        rid = int(entry["id"])
        stored = self.quality.get_entry(rid)
        self.assertEqual(stored["成功URL数"], 7)
        self.assertEqual(stored["字段覆盖率"], 0.66)

        confirmed, message = self.quality.confirm(rid)
        self.assertIsNone(confirmed)
        self.assertIn("未生成完成", message)
        archived, message = self.quality.archive(rid)
        self.assertIsNone(archived)

    # ------------------------------------------------------------ 2. 生成与数量校验
    def test_generation_reads_sources_validates_and_freezes(self) -> None:
        rid = self._create_report()
        entry, message = self.quality.start_generation(rid, request_id="r1")
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["status"], "已生成")
        self.assertEqual(entry["总URL数"], 10)
        self.assertEqual(entry["成功URL数"], 8)
        self.assertEqual(entry["失败URL数"], 2)
        self.assertEqual(entry["内容完整率"], 0.8)
        self.assertEqual(entry["字段覆盖率"], 0.82)
        self.assertEqual(entry["提取器版本"], 1)
        self.assertEqual(len(entry["快照"]), 1)
        version = int(entry["当前快照版本"])
        snapshot = self.quality.get_snapshot(rid, version)
        self.assertEqual(len(snapshot["来源URL结果"]), 10)
        self.assertTrue(any("=" in c for c in snapshot["校验"]))

    def test_generation_without_url_results_rejected_and_retryable(self) -> None:
        store.rows("crawljob").append({
            "id": 9002, "status": "草稿", "pending": True, "abnormal": False,
            "作业编号": "CRAW-UT-EMPTY", "目标站点": "空", "起始URL": "https://e.invalid",
        })
        entry, _ = self.quality.create_entry({"报告编号": "QUAL-UT-E", "作业编号": "CRAW-UT-EMPTY"})
        rid = int(entry["id"])
        result, message = self.quality.start_generation(rid, request_id="re")
        self.assertIsNone(result)
        self.assertIn("暂无 URL 抓取结果", message)
        self.assertEqual(self.quality.get_entry(rid)["status"], "草稿")

    # ------------------------------------------------------------ 3. 幂等与并发
    def test_idempotent_request_and_unchanged_data(self) -> None:
        rid = self._create_report()
        first, _ = self.quality.start_generation(rid, request_id="dup")
        again, message = self.quality.start_generation(rid, request_id="dup")
        self.assertIsNotNone(again)
        self.assertIn("幂等", message)
        other, message = self.quality.start_generation(rid, request_id="dup-other")
        self.assertIsNotNone(other)
        self.assertIn("幂等", message)
        self.assertEqual(len(self.quality.get_entry(rid)["快照"]), 1)

    def test_concurrent_generation_yields_single_snapshot(self) -> None:
        rid = self._create_report("QUAL-UT-CONC")
        outcomes: list[bool] = []
        barrier = threading.Barrier(6)

        def fire(idx: int) -> None:
            barrier.wait()
            entry, _ = self.quality.start_generation(rid, request_id=f"c{idx}")
            outcomes.append(entry is not None)

        threads = [threading.Thread(target=fire, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(outcomes), 6)
        self.assertTrue(all(outcomes))
        self.assertEqual(len(self.quality.get_entry(rid)["快照"]), 1)

    # ------------------------------------------------------------ 4. 确认冻结与覆盖率互斥
    def test_confirmed_snapshot_blocks_stale_coverage_write(self) -> None:
        rid = self._create_report()
        self.quality.start_generation(rid, request_id="g")
        confirmed, message = self.quality.confirm(rid)
        self.assertIsNotNone(confirmed, message)
        self.assertEqual(confirmed["status"], "已确认")

        blocked, message = self.content.update_coverage(9001, coverage=0.5, request_id="cov1")
        self.assertIsNone(blocked)
        self.assertIn("冻结", message)
        extractor = self.content.get_entry(9001)
        self.assertEqual(extractor["字段覆盖率"], 0.82)

        # 未被冻结的提取器允许写入，且 request_id 幂等。
        store.rows("contentextract").append({
            "id": 9002, "status": "草稿", "pending": True, "abnormal": False,
            "提取器编号": "CONT-UT-0002", "内容类型": "列表", "正文选择器": "ul",
            "关联作业编号": "CRAW-UT-X", "提取器版本": 1, "字段覆盖率": 0.6,
            "最新质量结论": "待生成", "最近质量回写": None, "质量回写流水": [],
        })
        ok, message = self.content.update_coverage(9002, coverage=0.77, request_id="cov2")
        self.assertIsNotNone(ok, message)
        ok2, _ = self.content.update_coverage(9002, coverage=0.99, request_id="cov2")
        self.assertIsNotNone(ok2)
        self.assertEqual(self.content.get_entry(9002)["字段覆盖率"], 0.77)
        bad, _ = self.content.update_coverage(9002, coverage=1.5)
        self.assertIsNone(bad)

    # ------------------------------------------------------------ 5. 回调幂等
    def test_callback_idempotent_and_rejected_before_generation(self) -> None:
        rid = self._create_report()
        early, message = self.quality.finish_callback(rid, callback_id="cb0")
        self.assertIsNone(early)
        self.assertIn("尚未生成完成", message)

        self.quality.start_generation(rid, request_id="g")
        first, _ = self.quality.finish_callback(rid, callback_id="cb1")
        self.assertIsNotNone(first)
        duplicate, message = self.quality.finish_callback(rid, callback_id="cb1")
        self.assertIsNotNone(duplicate)
        self.assertIn("幂等", message)
        stored = self.quality.get_entry(rid)
        self.assertEqual(stored["已处理回调"], ["cb1"])
        self.assertEqual(stored["成功URL数"], 8)

    # ------------------------------------------------------------ 6. 回写流水
    def test_writeback_ledger_no_duplicate(self) -> None:
        rid = self._create_report()
        self.quality.start_generation(rid, request_id="g")
        self.quality.confirm(rid)
        extractor = self.content.get_entry(9001)
        stages = {item["阶段"] for item in extractor["质量回写流水"]}
        self.assertGreaterEqual(stages, {"生成完成", "确认报告"})
        size = len(extractor["质量回写流水"])
        # 重复确认被状态机拒绝，不新增流水。
        self.quality.confirm(rid)
        self.assertEqual(len(self.content.get_entry(9001)["质量回写流水"]), size)

    # ------------------------------------------------------------ 7. 版本冲突与快照保留
    def test_archive_retry_keeps_prior_version_and_records_conflict(self) -> None:
        rid = self._create_report()
        self.quality.start_generation(rid, request_id="g1")
        self.quality.confirm(rid)
        self.quality.archive(rid)
        # 新一轮抓取（v2）：失败 URL 更正，10 条全部成功。
        for i in range(1, 11):
            self._ingest(i, "成功", 200, version=2)
        regen, message = self.quality.start_generation(rid, request_id="g2")
        self.assertIsNotNone(regen, message)
        stored = self.quality.get_entry(rid)
        self.assertEqual(len(stored["快照"]), 2)
        self.assertEqual({s["作业版本"] for s in stored["快照"]}, {1, 2})
        self.assertGreaterEqual(len(stored["版本冲突"]), 1)
        self.assertEqual(stored["失败URL数"], 0)
        # 历史确认快照仍冻结可复核。
        prior = self.quality.get_snapshot(rid, 1)
        self.assertTrue(prior["冻结"])
        self.assertEqual(prior["失败URL数"], 2)

    # ------------------------------------------------------------ 8. 漂移复核
    def test_snapshot_flags_extractor_version_drift(self) -> None:
        rid = self._create_report()
        self.quality.start_generation(rid, request_id="g")
        version = int(self.quality.get_entry(rid)["当前快照版本"])
        before = self.quality.get_snapshot(rid, version)
        self.assertEqual(before["漂移复核"], [])
        # 提取侧升版：报告引用的旧版本与当前版本产生漂移，快照仍以冻结值为准。
        extractor = store.find("contentextract", 9001)
        extractor["提取器版本"] = 2
        extractor["字段覆盖率"] = 0.95
        after = self.quality.get_snapshot(rid, version)
        self.assertTrue(any("版本漂移" in note for note in after["漂移复核"]))
        self.assertEqual(after["字段覆盖率"], 0.82)

    # ------------------------------------------------------------ 9. 结论文件与登记幂等
    def test_conclusion_file_and_ingest_idempotency(self) -> None:
        rid = self._create_report()
        payload, message = self.quality.build_conclusion_file(rid)
        self.assertIsNone(payload)
        self.assertIn("尚未生成", message)
        self.quality.start_generation(rid, request_id="g")
        payload, conclusion = self.quality.build_conclusion_file(rid)
        self.assertIsNotNone(payload)
        self.assertEqual(conclusion, "待改进")
        self.assertIn("质量结论：待改进", payload["text"])
        self.assertEqual(payload["json"]["指标"]["总URL数"], 10)

        duplicate, message = self.quality.ingest_url_result({
            "作业编号": self.job_no, "作业版本": 1,
            "URL": "https://ut.invalid/v1/1", "抓取结果": "成功",
        })
        self.assertIsNotNone(duplicate)
        self.assertIn("幂等", message)


if __name__ == "__main__":
    unittest.main()
