"""血缘交接包测试：范围出包、边界证据三态、中断续做和变更复核清单同步。"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import lineage_graph as lineage_module
from app.services.lineage_graph import (
    EVIDENCE_CONFIRMED,
    EVIDENCE_MISSING,
    EVIDENCE_NONE,
    REDACTED_VALUE,
    LineageGraphService,
)
from app.services.metadata_diff import MetadataDiffService
from app.store import store

FACT_TABLE = "dwd_trade_detail"


@pytest.fixture(autouse=True)
def clean_handoff_state():
    lineage_module._HANDOFF_JOBS.clear()
    lineage_module._HANDOFF_PACKAGES.clear()
    yield
    lineage_module._HANDOFF_JOBS.clear()
    lineage_module._HANDOFF_PACKAGES.clear()


def _relation(rel_no: str) -> dict:
    return next(row for row in store.rows("lineage_graph") if row.get("关系编号") == rel_no)


class TestHandoffScope:
    def test_out_of_scope_source_becomes_boundary_not_dependency(self):
        """同一张事实表两条来源关系，其中一条来自交接范围外的库。"""
        svc = LineageGraphService()
        package = svc.build_handoff_package(
            databases=["dw_dwd", "dw_ods"], table=FACT_TABLE, deleted_fields=["order_id"])

        boundary_ids = {view["关系编号"] for view in package["关系边界"]}
        assert "LINE-0006" in boundary_ids  # crm_customer 在 crm_core
        boundary = next(v for v in package["关系边界"] if v["关系编号"] == "LINE-0006")
        assert boundary["是否在交接范围"] is False
        assert boundary["证据状态"] == EVIDENCE_MISSING
        assert "crm_core" in boundary["边界说明"]

        confirmed_ids = {v["关系编号"] for v in package["关系中转"]}
        assert "LINE-0004" in confirmed_ids  # ods_order_info 在 dw_ods
        # 两条来源关系确实都指向同一张事实表，接收人要在同一张表上判断。
        assert "LINE-0004" in {v["关系编号"] for v in package["关系中转"] + package["关系边界"]}
        assert "LINE-0006" in {v["关系编号"] for v in package["关系中转"] + package["关系边界"]}

    def test_downstream_out_of_scope_is_boundary_too(self):
        svc = LineageGraphService()
        package = svc.build_handoff_package(
            databases=["dw_dwd", "dw_ods"], table=FACT_TABLE, deleted_fields=["order_id"])
        downstream = next(v for v in package["关系边界"] if v["关系编号"] == "LINE-0007")
        assert downstream["下游表名"] == "dws_trade_summary"
        assert downstream["下游库名"] == "dw_dws"
        assert downstream["证据状态"] == EVIDENCE_MISSING

    def test_out_of_scope_fields_and_sql_are_redacted(self):
        svc = LineageGraphService()
        package = svc.build_handoff_package(
            databases=["dw_dwd", "dw_ods"], table=FACT_TABLE, deleted_fields=["order_id"])
        for view in package["关系边界"]:
            assert view["来源字段"] == REDACTED_VALUE
            assert view["目标字段"] == REDACTED_VALUE
            assert view["转换逻辑"] == REDACTED_VALUE
            assert view["采集批次"] == REDACTED_VALUE
        dumped = str(package)
        assert "SELECT cust_code" not in dumped
        assert "cust_code,region_code" not in dumped
        assert svc.validate_handoff_package(package) == []

    def test_empty_scope_rejected(self):
        with pytest.raises(ValueError):
            LineageGraphService().build_handoff_package(
                databases=[], table=FACT_TABLE, deleted_fields=["order_id"])

    def test_deletion_impact_three_states(self):
        svc = LineageGraphService()
        package = svc.build_handoff_package(
            databases=["dw_dwd", "dw_ods"], table=FACT_TABLE, deleted_fields=["order_id"])
        impact = {item["关系编号"]: item for item in package["删除影响"]}
        # 范围内且有字段证据
        assert impact["LINE-0004"]["结论"] == EVIDENCE_CONFIRMED
        assert impact["LINE-0004"]["匹配字段"] == ["order_id"]
        # 范围外来源必须留证据欠缺，不能当成无依赖
        assert impact["LINE-0006"]["结论"] == EVIDENCE_MISSING
        assert impact["LINE-0006"]["需补采"] is True

        none_package = svc.build_handoff_package(
            databases=["dw_dwd", "dw_ods"], table=FACT_TABLE, deleted_fields=["region_code"])
        none_impact = {item["关系编号"]: item for item in none_package["删除影响"]}
        assert none_impact["LINE-0004"]["结论"] == EVIDENCE_NONE
        assert none_impact["LINE-0004"]["需补采"] is False


class TestHandoffInterruption:
    def test_retry_keeps_removed_dependency_as_missing_evidence(self):
        """导出中断后重试：已剔除的依赖仍以欠缺证据区别于确实无依赖。"""
        svc = LineageGraphService()
        relations = [
            dict(row) for row in store.rows("lineage_graph")
            if FACT_TABLE in (row.get("上游表名"), row.get("下游表名"))
        ]
        svc.mark_handoff_interrupted(
            "HANDOFF-retry",
            completed_steps=["读取表目录", "抽取范围内关系"],
            extracted_relations=relations,
            databases=["dw_dwd", "dw_ods"],
            table=FACT_TABLE,
            deleted_fields=["buyer_id"],
        )

        # 模拟中断后的重试里，这条范围外关系没有重新抽到（例如采集被跳过）。
        removed = _relation("LINE-0006")
        original_status = removed["status"]
        removed["status"] = "待校验"
        try:
            package = svc.build_handoff_package(
                databases=["dw_dwd", "dw_ods"],
                table=FACT_TABLE,
                deleted_fields=["buyer_id"],
                package_id="HANDOFF-retry",
            )
        finally:
            removed["status"] = original_status

        assert package["是否续做"] is True
        assert package["已完成步骤"] == ["读取表目录", "抽取范围内关系"]
        assert "接入范围外边界" not in package["本轮执行步骤"]
        assert package["中断遗留边界数"] >= 1

        boundary = next(v for v in package["关系边界"] if v["关系编号"] == "LINE-0006")
        assert boundary["证据状态"] == EVIDENCE_MISSING

        impact = next(i for i in package["删除影响"] if i["关系编号"] == "LINE-0006")
        assert impact["结论"] == EVIDENCE_MISSING
        assert impact["需补采"] is True

        no_dependency = next(i for i in package["删除影响"] if i["关系编号"] == "LINE-0004")
        assert no_dependency["结论"] == EVIDENCE_NONE
        assert svc.validate_handoff_package(package) == []


class TestHandoffDownload:
    def test_csv_keeps_boundary_evidence_without_leaking_details(self):
        svc = LineageGraphService()
        package = svc.build_handoff_package(
            databases=["dw_dwd", "dw_ods"], table=FACT_TABLE, deleted_fields=["order_id"])
        csv_text = svc.export_handoff_csv(package)

        assert package["包编号"] in csv_text
        assert "证据欠缺" in csv_text
        assert "已剔除（超出交接库范围）" in csv_text
        # 范围外来源的字段与转换语句不得出现在下载文件里。
        assert "cust_code" not in csv_text
        assert "SELECT cust_code" not in csv_text
        # 范围内关系的转换语句可以随包交接。
        assert "SELECT order_id, buyer_id, amount FROM ods_order_info" in csv_text

    def test_replay_matches_package_relations(self):
        svc = LineageGraphService()
        package = svc.build_handoff_package(
            databases=["dw_dwd", "dw_ods"], table=FACT_TABLE, deleted_fields=["order_id"])
        replay = svc.replay_handoff_package(package)
        assert replay["已确认关系"] == [v["关系编号"] for v in package["关系中转"]]
        assert set(replay["仍需跨库确认"]) == {v["关系编号"] for v in package["关系边界"]}

    def test_replay_does_not_advance_checkpoint(self):
        svc = LineageGraphService()
        package = svc.build_handoff_package(
            databases=["dw_dwd", "dw_ods"],
            table=FACT_TABLE,
            deleted_fields=["order_id"],
            package_id="HANDOFF-checkpoint",
        )
        before = dict(lineage_module._HANDOFF_JOBS["HANDOFF-checkpoint"])
        svc.replay_handoff_package(package)
        assert lineage_module._HANDOFF_JOBS["HANDOFF-checkpoint"] == before


class TestFieldDeletionAndChecklist:
    def test_submit_field_deletion_separates_actionable_and_boundary(self):
        result = MetadataDiffService().submit_field_deletion(
            table=FACT_TABLE,
            deleted_fields=["order_id"],
            databases=["dw_dwd", "dw_ods"],
        )
        assert result["包编号"]
        assert result["变更编号"].startswith("META-")
        assert {item["关系编号"] for item in result["影响清单"]} == {"LINE-0004"}
        assert "LINE-0006" in {item["关系编号"] for item in result["需补采的边界"]}
        assert all(item["结论"] == EVIDENCE_MISSING for item in result["需补采的边界"])
        assert result["同步复核清单条数"] >= 2
        assert result["交接包问题"] == []

    def test_checklist_rows_written_to_metadata_diff(self):
        result = MetadataDiffService().submit_field_deletion(
            table=FACT_TABLE,
            deleted_fields=["order_id"],
            databases=["dw_dwd", "dw_ods"],
        )
        rows = [
            row for row in store.rows("metadata_diff")
            if row.get("变更批次") == result["包编号"] and row.get("变更类型") == "血缘交接复核"
        ]
        assert rows
        assert {row["结论"] for row in rows} <= {EVIDENCE_CONFIRMED, EVIDENCE_MISSING}
        assert {row["复核类别"] for row in rows} == {"可处理对象", "需补采边界"}
        # 确认无依赖不进入需要审批的复核清单，避免噪声。
        assert "确认无依赖" not in {row["复核类别"] for row in rows}

    def test_checklist_sync_is_idempotent_on_retry(self):
        svc = LineageGraphService()
        package = svc.build_handoff_package(
            databases=["dw_dwd", "dw_ods"],
            table=FACT_TABLE,
            deleted_fields=["order_id"],
            package_id="HANDOFF-idempotent",
        )
        first = svc.sync_handoff_to_checklist(package)
        second = svc.sync_handoff_to_checklist(package)
        assert first >= 2
        assert second == 0

    def test_comparison_groups_confirmed_and_pending_boundary(self):
        svc = LineageGraphService()
        package = svc.build_handoff_package(
            databases=["dw_dwd", "dw_ods"], table=FACT_TABLE, deleted_fields=["order_id"])
        comparison = MetadataDiffService().compare_handoff_package(package)
        assert comparison["包编号"] == package["包编号"]
        assert [item["关系编号"] for item in comparison["已确认关系"]] == ["LINE-0004"]
        pending_ids = {item["关系编号"] for item in comparison["仍需跨库确认"]}
        assert "LINE-0006" in pending_ids
        for item in comparison["仍需跨库确认"]:
            assert item["证据状态"] == EVIDENCE_MISSING
            assert item["来源字段"] == ""
            assert item["转换逻辑"] == ""
        assert comparison["下载文件名"] == f"{package['包编号']}.json"

    def test_compare_without_package_is_empty_but_safe(self):
        comparison = MetadataDiffService().compare_handoff_package(None)
        assert comparison["包编号"] is None
        assert comparison["已确认关系"] == []
        assert comparison["仍需跨库确认"] == []

    def test_package_replays_with_same_scope(self):
        svc = LineageGraphService()
        package = svc.build_handoff_package(
            databases=["dw_dwd", "dw_ods"], table=FACT_TABLE, deleted_fields=["order_id"])
        replay = svc.replay_handoff_package(package)
        assert replay["可回放"] is True
        assert replay["范围指纹"] == replay["重放范围指纹"]
        assert replay["仍需跨库确认"]
        assert replay["问题清单"] == []

    def test_package_replay_detects_scope_drift(self):
        svc = LineageGraphService()
        package = svc.build_handoff_package(
            databases=["dw_dwd", "dw_ods"], table=FACT_TABLE, deleted_fields=["order_id"])
        tampered = {**package, "交接库范围": ["dw_dwd"]}
        replay = svc.replay_handoff_package(tampered)
        assert replay["可回放"] is False
        assert replay["漂移提示"]
