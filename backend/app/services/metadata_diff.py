"""元数据变更对比服务：检测和记录表结构变更，驱动血缘图谱更新。

domain_chain: 元数据变更对比 (metadata_diff)
  └─ 校验 → 血缘图谱更新 (lineage_graph)
  └─ 关联 → 质量事件 (dq_incident)
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.lineage_graph import (
    EVIDENCE_CONFIRMED,
    EVIDENCE_MISSING,
    EVIDENCE_NONE,
)
from app.store import store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _split_csv(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text or text == "*":
        return []
    return [item.strip() for item in text.replace("，", ",").split(",") if item.strip()]


class MetadataDiffService(BaseModuleService):
    MODULE = "metadata_diff"
    ENTITY = "变更记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["变更编号", "变更对象", "变更类型", "旧值", "新值", "变更批次", "影响评估", "变更时间"]
    REQUIRED_FIELDS = ["变更编号", "变更对象", "变更类型"]
    STATUS_ORDER = ["草稿", "待审核", "已确认", "已回滚"]
    ACTION_RULES = {"提交审核": "待审核", "确认变更": "已确认", "执行回滚": "已回滚"}
    NEGATIVE_ACTIONS = []

    def compare_schemas(self, old_columns: list[dict], new_columns: list[dict],
                         table_name: str) -> dict[str, Any]:
        """对比两次元数据采集的表结构差异，生成变更记录。

        解析新老字段列表，识别新增、删除、类型变更等差异，
        校验变更影响范围并写入变更记录，触发血缘图谱的更新。
        """
        old_map = {c.get("字段名称"): c for c in old_columns if c.get("字段名称")}
        new_map = {c.get("字段名称"): c for c in new_columns if c.get("字段名称")}
        old_names = set(old_map.keys())
        new_names = set(new_map.keys())

        additions = new_names - old_names
        deletions = old_names - new_names
        common = old_names & new_names

        changes = []
        # 识别新增字段
        for col_name in additions:
            changes.append({"字段": col_name, "变更类型": "新增",
                            "旧值": "N/A",
                            "新值": f"类型:{new_map[col_name].get('数据类型', 'unknown')}"})
        # 识别删除字段
        for col_name in deletions:
            changes.append({"字段": col_name, "变更类型": "删除",
                            "旧值": f"类型:{old_map[col_name].get('数据类型', 'unknown')}",
                            "新值": "N/A"})
        # 识别类型变更
        for col_name in common:
            old_type = old_map[col_name].get("数据类型", "")
            new_type = new_map[col_name].get("数据类型", "")
            if old_type != new_type:
                changes.append({"字段": col_name, "变更类型": "类型变更",
                                "旧值": old_type, "新值": new_type})

        batch_hash = hashlib.md5(f"{table_name}:{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:8]

        # 写入变更记录
        diff_rows = store.rows(self.MODULE)
        for change in changes:
            new_did = max((int(r.get("id", 0)) for r in diff_rows), default=0) + 1
            diff_rows.append({
                "id": new_did,
                "变更编号": f"META-{new_did:04d}",
                "变更对象": f"{table_name}.{change['字段']}",
                "变更类型": change["变更类型"],
                "旧值": change["旧值"],
                "新值": change["新值"],
                "变更批次": batch_hash,
                "影响评估": self._assess_impact(change["变更类型"], table_name, change["字段"]),
                "变更时间": datetime.now(timezone.utc).isoformat(),
                "status": "已确认" if change["变更类型"] != "删除" else "待审核",
                "pending": change["变更类型"] == "删除",
                "abnormal": change["变更类型"] == "删除",
            })

        # 检测到字段删除：校验血缘完整性并触发更新
        if deletions:
            self._validate_lineage_impact(table_name, list(deletions))

        return {
            "表名称": table_name,
            "旧字段数": len(old_columns),
            "新字段数": len(new_columns),
            "变更总数": len(changes),
            "新增字段": list(additions),
            "删除字段": list(deletions),
            "类型变更": [c for c in changes if c["变更类型"] == "类型变更"],
            "变更批次": batch_hash,
        }

    def _assess_impact(self, change_type: str, table_name: str, field_name: str) -> str:
        """评估变更影响范围。"""
        if change_type == "删除":
            return f"高风险：可能影响下游依赖{table_name}的表，需校验血缘完整性"
        elif change_type == "类型变更":
            return f"中风险：可能影响ETL转换逻辑和数据写入"
        else:
            return "低风险：新增字段不影响已有逻辑"

    def _validate_lineage_impact(self, table_name: str, deleted_fields: list[str]) -> None:
        """校验血缘完整性：只对交接范围内有字段级证据的依赖标记待校验。

        范围外的来源关系不能仅凭表名就升级为异常——那会把「没权限看」当成「确认断链」。
        """
        from app.services.lineage_graph import LineageGraphService

        lineage_service = LineageGraphService()
        package = lineage_service.latest_handoff_package(table_name)
        if package is None:
            # 没有交接包：范围外的字段明细不可见，不做确定判断，只保留变更记录。
            return
        if set(package.get("待删字段") or []) != set(deleted_fields):
            # 交接包针对的是另一批删除字段，不能拿它的结论套用到本次变更。
            return
        confirmed_ids = {item["关系编号"] for item in package.get("删除影响", [])
                         if item.get("结论") == EVIDENCE_CONFIRMED}
        for lr in store.rows("lineage_graph"):
            if lr.get("关系编号") in confirmed_ids:
                lr["abnormal"] = True
                lr["status"] = "待校验"

    def compare_handoff_package(self, package: dict[str, Any] | None) -> dict[str, Any]:
        """变更对比消费血缘交接包：分开「已确认关系」与「仍需跨库确认的边界」。

        下载文件里的关系行会带上同一套分组，接收人打开即可对照，
        不会把证据欠缺的边界当成已确认依赖，也不会把真无依赖误报成待办。
        """
        if not package:
            return {
                "包编号": None,
                "已确认关系": [],
                "仍需跨库确认": [],
                "确认无依赖": [],
                "字段清单": [],
                "可处理对象": [],
                "需补采边界": [],
                "下载文件名": "",
            }
        confirmed = [view for view in package.get("关系中转", [])]
        boundaries = [view for view in package.get("关系边界", [])]
        no_dependency = [
            item for item in package.get("删除影响", [])
            if item.get("结论") == EVIDENCE_NONE
        ]
        approval = package.get("审批清单", {})
        fields = sorted({
            field
            for view in confirmed
            for key in ("来源字段", "目标字段")
            for field in _split_csv(view.get(key))
        })
        return {
            "包编号": package.get("包编号"),
            "交接库范围": package.get("交接库范围", []),
            "事实表": package.get("事实表"),
            "已确认关系": [
                {
                    "关系编号": view.get("关系编号"),
                    "上游表名": view.get("上游表名"),
                    "下游表名": view.get("下游表名"),
                    "来源字段": view.get("来源字段"),
                    "目标字段": view.get("目标字段"),
                    "转换逻辑": view.get("转换逻辑"),
                    "证据状态": EVIDENCE_CONFIRMED,
                }
                for view in confirmed
            ],
            "仍需跨库确认": [
                {
                    "关系编号": view.get("关系编号"),
                    "上游表名": view.get("上游表名"),
                    "上游库名": view.get("上游库名"),
                    "下游表名": view.get("下游表名"),
                    "下游库名": view.get("下游库名"),
                    "证据状态": EVIDENCE_MISSING,
                    "边界说明": view.get("边界说明"),
                    # 范围外字段与转换语句不出包，这里显式留空，避免界面误展示。
                    "来源字段": "",
                    "转换逻辑": "",
                }
                for view in boundaries
            ],
            "确认无依赖": [
                {"关系编号": item.get("关系编号"), "对端表名": item.get("对端表名"),
                 "结论": item.get("结论")}
                for item in no_dependency
            ],
            "字段清单": fields,
            "可处理对象": approval.get("可处理对象", []),
            "需补采边界": approval.get("需补采边界", []),
            "下载文件名": f"{package.get('包编号')}.json",
        }

    def submit_field_deletion(
        self,
        *,
        table: str,
        deleted_fields: list[str],
        databases: list[str],
        package_id: str | None = None,
    ) -> dict[str, Any]:
        """提交一次字段删除：先生成交接包，再同步复核清单，最后给出影响清单。

        返回的影响清单里，「审批者可以处理的对象」只包含交接范围内有字段级证据的依赖，
        「需要补采的边界」单列范围外来源，接收人不必在两条来源里猜哪条能拍板。
        """
        from app.services.lineage_graph import LineageGraphService

        lineage_service = LineageGraphService()
        package = lineage_service.build_handoff_package(
            databases=databases,
            table=table,
            deleted_fields=deleted_fields,
            package_id=package_id,
        )
        synced = lineage_service.sync_handoff_to_checklist(package)

        # 变更记录本身也落一条，保证删除动作在变更对比列表里可见。
        batch = package.get("包编号") or ""
        diff_rows = store.rows(self.MODULE)
        new_id = max((int(r.get("id", 0)) for r in diff_rows), default=0) + 1
        record = {
            "id": new_id,
            "变更编号": f"META-{new_id:04d}",
            "变更对象": f"{table}.{'、'.join(deleted_fields)}",
            "变更类型": "删除",
            "旧值": f"字段在表 {table} 中",
            "新值": "N/A",
            "变更批次": batch,
            "影响评估": (
                f"高风险：可确认依赖{len(package.get('审批清单', {}).get('可处理对象', []))}条，"
                f"待跨库确认{len(package.get('审批清单', {}).get('需补采边界', []))}条"
            ),
            "变更时间": _now(),
            "交接包编号": batch,
            "status": "待审核",
            "pending": True,
            "abnormal": bool(package.get("审批清单", {}).get("需补采边界")),
        }
        diff_rows.append(record)

        comparison = self.compare_handoff_package(package)
        return {
            "变更编号": record["变更编号"],
            "包编号": batch,
            "生成时间": package.get("生成时间"),
            "是否续做": package.get("是否续做"),
            "影响清单": comparison["可处理对象"],
            "需补采的边界": comparison["需补采边界"],
            "确认无依赖": comparison["确认无依赖"],
            "同步复核清单条数": synced,
            "交接包问题": package.get("问题清单", []),
            "下载文件名": comparison["下载文件名"],
        }

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "校验"
        snapshot["desc"] = "对比元数据schema差异并校验血缘影响"
        snapshot["artifact"] = "变更对比记录数据集"
        snapshot["artifact_desc"] = "含影响评估的变更对比记录数据集"
        return snapshot