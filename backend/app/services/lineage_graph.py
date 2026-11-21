"""血缘图谱服务：解析和关联数据表上下游依赖关系，驱动元数据变更对比和质量事件追踪。

domain_chain: 血缘图谱 (lineage_graph)
  └─ 关联 → 元数据变更对比 (metadata_diff)
  └─ 查询 → 质量事件追踪 (dq_incident)
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 交接包里的证据状态：接收人必须能区分「没有权限看到」和「确认没有依赖」。
EVIDENCE_CONFIRMED = "已确认"
EVIDENCE_MISSING = "证据欠缺"
EVIDENCE_NONE = "无依赖"

# 范围外的字段级明细与转换语句不允许出包，用固定占位替代。
REDACTED_VALUE = "已剔除（超出交接库范围）"
HANDOFF_FORMAT_VERSION = "1.0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_field_list(value: Any) -> list[str]:
    """把血缘关系里逗号分隔的字段清单拆成 token；通配符不展开。"""
    text = str(value or "").strip()
    if not text or text == "*":
        return []
    for separator in ("，", "、", ";", "；", "|"):
        text = text.replace(separator, ",")
    return [item.strip() for item in text.split(",") if item.strip()]


def _canonical_fingerprint(core: dict[str, Any]) -> str:
    payload = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# 交接包任务：中断后按同一包编号重试即可续做，已剔除的依赖不会被当成「无依赖」。
_HANDOFF_JOBS: dict[str, dict[str, Any]] = {}
# 已生成的交接包：下载接口按包编号取回同一份可回放材料。
_HANDOFF_PACKAGES: dict[str, dict[str, Any]] = {}


class LineageGraphService(BaseModuleService):
    MODULE = "lineage_graph"
    ENTITY = "血缘关系"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["关系编号", "上游表名", "下游表名", "来源字段", "目标字段", "转换逻辑", "采集批次", "关系版本"]
    REQUIRED_FIELDS = ["关系编号", "上游表名", "下游表名"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用关系": "已停用"}
    NEGATIVE_ACTIONS = ["停用关系"]

    def parse_lineage_from_sql(self, sql_text: str, target_table: str) -> list[dict[str, Any]]:
        """解析SQL语句提取上下游血缘关系并关联入图谱。

        从SQL解析FROM/JOIN子句，识别上游源表，建立数据血缘关系。
        解析结果关联写入血缘图谱，同时触发变更对比流程。
        """
        import re
        from_pattern = re.findall(r'(?:FROM|JOIN)\s+(\w+(?:\.\w+)?)', sql_text, re.IGNORECASE)
        upstream_tables = list(set(from_pattern))
        if target_table in upstream_tables:
            upstream_tables.remove(target_table)

        lineages = []
        lineage_rows = store.rows(self.MODULE)
        for upstream in upstream_tables:
            new_lid = max((int(r.get("id", 0)) for r in lineage_rows), default=0) + 1
            entry = {
                "id": new_lid,
                "关系编号": f"LINE-{new_lid:04d}",
                "上游表名": upstream,
                "下游表名": target_table,
                "来源字段": "*",
                "目标字段": "*",
                "转换逻辑": "SQL解析",
                "采集批次": datetime.now(timezone.utc).strftime("%Y%m%d%H"),
                "关系版本": "v1",
                "status": "已生效",
                "pending": False,
                "abnormal": False,
            }
            lineage_rows.append(entry)
            lineages.append(entry)
        return lineages

    def build_lineage_graph(self, table_name: str, depth: int = 3) -> dict[str, Any]:
        """构建指定表的血缘关系图谱，向上递归探索上游，向下递归探索下游。

        查询关联到该表的所有血缘记录，构建双向血缘图并聚合为图谱快照。
        """
        all_rows = store.rows(self.MODULE)
        # 查询上下游关系
        upstream = [r for r in all_rows if r.get("下游表名") == table_name and r.get("status") == "已生效"]
        downstream = [r for r in all_rows if r.get("上游表名") == table_name and r.get("status") == "已生效"]

        # 递归展开上游
        expanded_upstream = self._expand_lineage(upstream, direction="up", depth=depth - 1)

        return {
            "中心表": table_name,
            "上游表数": len(expanded_upstream),
            "下游表数": len(downstream),
            "上游血缘": [
                {"表名": u.get("上游表名"), "关系编号": u.get("关系编号"),
                 "转换逻辑": u.get("转换逻辑"), "关系版本": u.get("关系版本")}
                for u in expanded_upstream
            ],
            "下游血缘": [
                {"表名": d.get("下游表名"), "关系编号": d.get("关系编号"),
                 "转换逻辑": d.get("转换逻辑")}
                for d in downstream
            ],
            "图谱生成时间": datetime.now(timezone.utc).isoformat(),
        }

    def _expand_lineage(self, entries: list[dict], direction: str, depth: int) -> list[dict]:
        """递归展开血缘关系，深度遍历上下游关联路径。"""
        if depth <= 0 or not entries:
            return entries
        all_rows = store.rows(self.MODULE)
        expanded = list(entries)
        for entry in entries:
            if direction == "up":
                parents = [r for r in all_rows
                           if r.get("下游表名") == entry.get("上游表名")
                           and r.get("status") == "已生效"
                           and r not in expanded]
            else:
                children = [r for r in all_rows
                            if r.get("上游表名") == entry.get("下游表名")
                            and r.get("status") == "已生效"
                            and r not in expanded]
            expanded.extend(parents if direction == "up" else children)
        return expanded

    def detect_lineage_breakage(self, table_name: str) -> list[dict]:
        """检测血缘断链：查询上游表是否仍存在于表目录中。

        关联血缘图与表目录，对比校验血缘上下游的完整性，识别断链的表依赖。
        """
        catalog_rows = store.rows("table_catalog")
        all_rows = store.rows(self.MODULE)
        upstream_entries = [r for r in all_rows if r.get("下游表名") == table_name]
        breakages = []
        for entry in upstream_entries:
            upstream_name = entry.get("上游表名", "")
            found = any(c.get("表名称") == upstream_name for c in catalog_rows)
            if not found:
                breakages.append({
                    "关系编号": entry.get("关系编号"),
                    "断链上游": upstream_name,
                    "下游": table_name,
                    "检测到断链": True,
                    "建议操作": "重新采集上游元数据或标记为已停用",
                })
        return breakages

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "关联"
        snapshot["desc"] = "解析SQL提取血缘关系并关联入图谱"
        snapshot["artifact"] = "血缘图谱数据集"
        snapshot["artifact_desc"] = "上下游血缘关系图谱数据集"
        return snapshot

    # ------------------------------------------------------------------
    # 血缘交接包：为指定库范围生成可回放、可续做的交接材料
    # ------------------------------------------------------------------
    def _table_db_map(self) -> dict[str, str]:
        """从表目录取表名→所属库名；目录里没有的表返回不了库，只能标为未知。"""
        mapping: dict[str, str] = {}
        for row in store.rows("table_catalog"):
            name = str(row.get("表名称") or "").strip()
            if name:
                mapping.setdefault(name, str(row.get("所属库名") or "").strip())
        return mapping

    @staticmethod
    def _table_in_scope(table: str, db_by_table: dict[str, str], scope: set[str]) -> bool:
        db = db_by_table.get(table)
        return bool(db) and db in scope

    def _relation_view(
        self,
        relation: dict[str, Any],
        db_by_table: dict[str, str],
        scope: set[str],
    ) -> dict[str, Any]:
        """单条关系的交接视图：范围内给全量，范围外只给边界，字段与转换语句一律不出现。"""
        upstream = str(relation.get("上游表名") or "")
        downstream = str(relation.get("下游表名") or "")
        rel_id = relation.get("关系编号") or f"LINE-{relation.get('id')}"
        up_db = db_by_table.get(upstream, "")
        down_db = db_by_table.get(downstream, "")
        up_in = self._table_in_scope(upstream, db_by_table, scope)
        down_in = self._table_in_scope(downstream, db_by_table, scope)

        boundary_reason = ""
        if not up_in and not down_in:
            boundary_reason = "上下游表都不在交接库范围，仅保留关系边界，需跨库确认"
        elif not up_in:
            boundary_reason = f"上游表位于范围外库「{up_db or '未知库'}」，仅保留关系边界，需跨库确认"
        elif not down_in:
            boundary_reason = f"下游表位于范围外库「{down_db or '未知库'}」，仅保留关系边界，需跨库确认"

        exported = up_in and down_in
        view: dict[str, Any] = {
            "关系编号": rel_id,
            "上游表名": upstream,
            "下游表名": downstream,
            "上游库名": up_db or "未知库",
            "下游库名": down_db or "未知库",
            "关系版本": relation.get("关系版本"),
            "是否在交接范围": exported,
            "边界说明": boundary_reason or "上下游均在交接库范围，字段与转换逻辑已随包导出",
        }
        if exported:
            view["来源字段"] = relation.get("来源字段")
            view["目标字段"] = relation.get("目标字段")
            view["转换逻辑"] = relation.get("转换逻辑")
            view["采集批次"] = relation.get("采集批次")
            view["证据状态"] = EVIDENCE_CONFIRMED
        else:
            # 只读权限也不能越权出包：越界字段与转换语句给脱敏占位，同时留证据标记。
            view["来源字段"] = REDACTED_VALUE
            view["目标字段"] = REDACTED_VALUE
            view["转换逻辑"] = REDACTED_VALUE
            view["采集批次"] = REDACTED_VALUE
            view["证据状态"] = EVIDENCE_MISSING
        return view

    def _field_impact(
        self,
        table: str,
        deleted_fields: list[str],
        relations: list[dict[str, Any]],
        db_by_table: dict[str, str],
        scope: set[str],
    ) -> list[dict[str, Any]]:
        """核对删除字段的影响，来源和下游两个方向都看。

        结论只有三种：已确认（范围内有字段级证据）、无依赖（范围内证据表明不重叠）、
        证据欠缺（对端在交接范围外，禁止凭猜测下结论）。
        """
        impacts: list[dict[str, Any]] = []
        wanted = set(deleted_fields)
        for relation in relations:
            upstream = str(relation.get("上游表名") or "")
            downstream = str(relation.get("下游表名") or "")
            rel_id = relation.get("关系编号") or f"LINE-{relation.get('id')}"
            if downstream == table:
                direction = "来源->事实表"
                peer = upstream
                peer_db = db_by_table.get(upstream) or "未知库"
                peer_in_scope = self._table_in_scope(upstream, db_by_table, scope)
                mapped_fields = _normalize_field_list(relation.get("目标字段"))
                position = "目标字段"
            elif upstream == table:
                direction = "事实表->下游"
                peer = downstream
                peer_db = db_by_table.get(downstream) or "未知库"
                peer_in_scope = self._table_in_scope(downstream, db_by_table, scope)
                mapped_fields = _normalize_field_list(relation.get("来源字段"))
                position = "来源字段"
            else:
                continue

            if not peer_in_scope:
                # 对端在交接范围外：不能凭脱敏后的占位下结论，必须标成待跨库确认。
                impacts.append({
                    "关系编号": rel_id,
                    "方向": direction,
                    "对端表名": peer,
                    "对端库名": peer_db,
                    "匹配字段": [],
                    "结论": EVIDENCE_MISSING,
                    "说明": f"{position}所在表「{peer}」不在交接库范围，缺少字段级证据，需跨库补采后确认是否断链",
                    "需补采": True,
                })
                continue
            if not mapped_fields:
                impacts.append({
                    "关系编号": rel_id,
                    "方向": direction,
                    "对端表名": peer,
                    "对端库名": peer_db,
                    "匹配字段": [],
                    "结论": EVIDENCE_NONE,
                    "说明": "关系为表级通配（*），范围内未登记字段级依赖，按无字段依赖处理",
                    "需补采": False,
                })
                continue
            matched = [field for field in mapped_fields if field in wanted]
            if matched:
                impacts.append({
                    "关系编号": rel_id,
                    "方向": direction,
                    "对端表名": peer,
                    "对端库名": peer_db,
                    "匹配字段": matched,
                    "结论": EVIDENCE_CONFIRMED,
                    "说明": f"{position}「{'、'.join(matched)}」覆盖待删字段，删除后可能断链，需先改依赖逻辑",
                    "需补采": False,
                })
            else:
                impacts.append({
                    "关系编号": rel_id,
                    "方向": direction,
                    "对端表名": peer,
                    "对端库名": peer_db,
                    "匹配字段": [],
                    "结论": EVIDENCE_NONE,
                    "说明": "范围内字段级证据表明与待删字段不重叠，确认不因本次删除断链",
                    "需补采": False,
                })
        return impacts

    def build_handoff_package(
        self,
        *,
        databases: list[str],
        table: str,
        deleted_fields: list[str] | None = None,
        package_id: str | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        """生成血缘交接包（JSON 可回放结构）。

        - 只导出指定库范围内的事实及其字段/转换语句；
        - 范围外的来源关系保留为「边界」，字段与转换语句脱敏，证据状态为「证据欠缺」；
        - 删除字段影响按「已确认 / 无依赖 / 证据欠缺」三态结论给出；
        - 调用方可用同一 package_id 重试，续做未完成的采集步骤；
        - persist=False 用于回放自检，不覆盖已有任务检查点。
        """
        scope = {str(item).strip() for item in databases if str(item).strip()}
        if not scope:
            raise ValueError("交接库范围不能为空")
        table = str(table or "").strip()
        if not table:
            raise ValueError("待交接的事实表不能为空")
        deleted = [str(item).strip() for item in (deleted_fields or []) if str(item).strip()]

        # 范围指纹只由「库范围 + 事实表 + 待删字段」决定，重试时才能对上同一份交接任务。
        scope_core = {
            "交接库范围": sorted(scope),
            "事实表": table,
            "待删字段": deleted,
        }
        fingerprint = _canonical_fingerprint(scope_core)
        key = package_id or f"HANDOFF-{fingerprint[:12]}"

        db_by_table = self._table_db_map()
        relations = [
            dict(row) for row in store.rows(self.MODULE)
            if str(row.get("status") or "") == "已生效"
        ]
        # 交接包围绕「待交接的事实表」展开：只保留直接连接到该表的关系，
        # 避免把无关库的整张图搬进包里，也避免扩大范围外明细的暴露面。
        relations = [
            row for row in relations
            if str(row.get("上游表名") or "") == table
            or str(row.get("下游表名") or "") == table
        ]
        involved = {
            str(row.get("上游表名") or "") for row in relations
        } | {str(row.get("下游表名") or "") for row in relations}
        relation_views = [self._relation_view(row, db_by_table, scope) for row in relations]
        in_scope = [view for view in relation_views if view["是否在交接范围"]]
        boundary = [view for view in relation_views if not view["是否在交接范围"]]

        # 中断续做：上一次已判定的范围外边界必须保留为「证据欠缺」，
        # 不能因为本轮没重新抽到就降级成「确实无依赖」。
        job = _HANDOFF_JOBS.get(key)
        resumed = bool(job and job.get("fingerprint") == fingerprint and job.get("interrupted"))
        checkpoint = dict(job.get("checkpoint") or {}) if resumed else {}
        current_boundary_ids = {view["关系编号"] for view in boundary}
        retained = []
        for view in checkpoint.get("关系边界", []):
            if view.get("关系编号") in current_boundary_ids:
                continue
            if view.get("是否在交接范围"):
                continue
            retained.append({
                **view,
                "证据状态": EVIDENCE_MISSING,
                "边界说明": f"{view.get('边界说明', '')}（上次导出已剔除，重试时仍按欠缺证据保留）",
            })
        if retained:
            boundary = [*boundary, *retained]

        central_in_scope = self._table_in_scope(table, db_by_table, scope)
        impacts = self._field_impact(table, deleted, relations, db_by_table, scope)
        # 中断遗留的边界同样进入删除影响核对，结论只能是「证据欠缺」。
        impact_ids = {item["关系编号"] for item in impacts}
        for view in retained:
            if view["关系编号"] in impact_ids:
                continue
            impacts.append({
                "关系编号": view["关系编号"],
                "方向": "中断遗留边界",
                "对端表名": view.get("上游表名") if view.get("下游表名") == table else view.get("下游表名"),
                "对端库名": view.get("上游库名") if view.get("下游表名") == table else view.get("下游库名"),
                "匹配字段": [],
                "结论": EVIDENCE_MISSING,
                "说明": "上次导出中断时已剔除该依赖，重试未重新取得字段级证据，不能视为无依赖",
                "需补采": True,
            })
        confirmed = [item for item in impacts if item["结论"] == EVIDENCE_CONFIRMED]
        missing = [item for item in impacts if item["结论"] == EVIDENCE_MISSING]
        none_dependency = [item for item in impacts if item["结论"] == EVIDENCE_NONE]

        approval = {
            "可处理对象": [
                {"关系编号": item["关系编号"], "方向": item["方向"], "对端表名": item["对端表名"],
                 "待改字段": item["匹配字段"], "结论": item["结论"]}
                for item in confirmed
            ],
            "需补采边界": [
                {"关系编号": item["关系编号"], "方向": item["方向"], "对端表名": item["对端表名"],
                 "对端库名": item["对端库名"], "结论": item["结论"]}
                for item in missing
            ],
            "确认无依赖": [
                {"关系编号": item["关系编号"], "方向": item["方向"], "对端表名": item["对端表名"],
                 "结论": item["结论"]}
                for item in none_dependency
            ],
        }

        core = {
            **scope_core,
            "事实表所属库": db_by_table.get(table) or "未知库",
            "事实表在交接范围": central_in_scope,
            "关系中转": in_scope,
            "关系边界": boundary,
            "删除影响": impacts,
            "审批清单": approval,
            "涉及表数": len(involved),
        }

        steps = ["读取表目录", "抽取范围内关系", "标注范围外边界", "核对删除影响"]
        done = list(job.get("completed_steps") or []) if resumed else []
        remaining = [step for step in steps if step not in done]
        if persist:
            _HANDOFF_JOBS[key] = {
                "fingerprint": fingerprint,
                "interrupted": False,
                "completed_steps": steps,
                "checkpoint": {
                    "关系边界": boundary,
                    "已剔除依赖编号": [view["关系编号"] for view in boundary],
                },
                "last_run_at": _now(),
            }

        package = {
            "包编号": key,
            "格式版本": HANDOFF_FORMAT_VERSION,
            "生成时间": _now(),
            "范围指纹": fingerprint,
            "是否续做": resumed,
            "已完成步骤": done if resumed else [],
            "本轮执行步骤": remaining,
            "中断遗留边界数": len(retained),
            **core,
            "统计": {
                "范围内关系数": len(in_scope),
                "范围外边界数": len(boundary),
                "中断遗留边界数": len(retained),
                "可确认影响数": len(confirmed),
                "证据欠缺数": len(missing),
                "确认无依赖数": len(none_dependency),
            },
        }
        package["问题清单"] = self.validate_handoff_package(package)
        if persist:
            _HANDOFF_PACKAGES[key] = package
        return package

    def get_handoff_package(self, package_id: str) -> dict[str, Any] | None:
        return _HANDOFF_PACKAGES.get(package_id)

    def replay_handoff_package(self, package: dict[str, Any]) -> dict[str, Any]:
        """按包内的库范围与事实表重放一次，核对材料是否仍然自洽可交接。"""
        problems = self.validate_handoff_package(package)
        try:
            replay = self.build_handoff_package(
                databases=list(package.get("交接库范围") or []),
                table=str(package.get("事实表") or ""),
                deleted_fields=list(package.get("待删字段") or []),
                persist=False,
            )
        except ValueError as exc:
            return {
                "包编号": package.get("包编号"),
                "可回放": False,
                "重放包编号": None,
                "范围指纹": package.get("范围指纹"),
                "重放范围指纹": None,
                "问题清单": [*problems, str(exc)],
                "漂移提示": ["交接包缺少库范围或事实表，无法重放"],
                "已确认关系": [view["关系编号"] for view in package.get("关系中转", [])],
                "仍需跨库确认": [view["关系编号"] for view in package.get("关系边界", [])],
                "统计": package.get("统计", {}),
            }
        same_fingerprint = replay.get("范围指纹") == package.get("范围指纹")
        # 重放不得泄漏范围外明细；范围指纹一致说明库范围和字段口径没有漂移。
        drift = [
            "重放结果与原包的范围指纹不一致，说明库范围或待删字段已变化"
        ] if not same_fingerprint else []
        return {
            "包编号": package.get("包编号"),
            "可回放": not problems and same_fingerprint,
            "重放包编号": replay.get("包编号"),
            "范围指纹": package.get("范围指纹"),
            "重放范围指纹": replay.get("范围指纹"),
            "问题清单": problems,
            "漂移提示": drift,
            "已确认关系": [view["关系编号"] for view in package.get("关系中转", [])],
            "仍需跨库确认": [view["关系编号"] for view in package.get("关系边界", [])],
            "统计": package.get("统计", {}),
        }

    def latest_handoff_package(self, table: str | None = None) -> dict[str, Any] | None:
        candidates = list(_HANDOFF_PACKAGES.values())
        if table:
            candidates = [item for item in candidates if item.get("事实表") == table]
        if not candidates:
            return None
        return max(candidates, key=lambda item: str(item.get("生成时间", "")))

    def sync_handoff_to_checklist(self, package: dict[str, Any]) -> int:
        """把交接包的复核清单写进元数据变更对比模块，接收人可在同一处跟进。"""
        diff_rows = store.rows("metadata_diff")
        existing = {
            str(row.get("交接备注") or "")
            for row in diff_rows
        }
        created = 0
        for item in self.handoff_review_checklist(package):
            if item["清单类型"] == "确认无依赖":
                # 无依赖只是留证，不进入需要审批的变更清单，避免噪声。
                continue
            remark = f"{item['包编号']}::{item['关系编号']}"
            if remark in existing:
                continue
            new_id = max((int(r.get("id", 0)) for r in diff_rows), default=0) + 1
            diff_rows.append({
                "id": new_id,
                "变更编号": f"META-{new_id:04d}",
                "变更对象": item["对象"],
                "变更类型": "血缘交接复核",
                "旧值": "未确认",
                "新值": item["结论"],
                "变更批次": item["包编号"],
                "影响评估": (
                    f"高风险：{item['待办']}" if item["清单类型"] == "可处理对象"
                    else f"待补采：{item['待办']}"
                ),
                "变更时间": _now(),
                "交接备注": remark,
                "关系编号": item["关系编号"],
                "复核类别": item["清单类型"],
                "结论": item["结论"],
                "status": "待审核",
                "pending": True,
                "abnormal": item["清单类型"] == "需补采边界",
            })
            existing.add(remark)
            created += 1
        return created

    def mark_handoff_interrupted(
        self,
        package_id: str,
        *,
        completed_steps: list[str],
        extracted_relations: list[dict[str, Any]],
        databases: list[str],
        table: str,
        deleted_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """记录一次中断的导出：把已抽到的范围外关系冻结成检查点，供重试续做。"""
        scope = {str(item).strip() for item in databases if str(item).strip()}
        deleted = [str(item).strip() for item in (deleted_fields or []) if str(item).strip()]
        fingerprint = _canonical_fingerprint({
            "交接库范围": sorted(scope), "事实表": table, "待删字段": deleted,
        })
        db_by_table = self._table_db_map()
        boundary = [
            self._relation_view(dict(row), db_by_table, scope)
            for row in extracted_relations
            if not (
                self._table_in_scope(str(row.get("上游表名") or ""), db_by_table, scope)
                and self._table_in_scope(str(row.get("下游表名") or ""), db_by_table, scope)
            )
        ]
        _HANDOFF_JOBS[package_id] = {
            "fingerprint": fingerprint,
            "interrupted": True,
            "completed_steps": list(completed_steps),
            "checkpoint": {
                "关系边界": boundary,
                "已剔除依赖编号": [view["关系编号"] for view in boundary],
            },
            "last_run_at": _now(),
        }
        return {"包编号": package_id, "已剔除依赖": [view["关系编号"] for view in boundary]}

    def export_handoff_csv(self, package: dict[str, Any]) -> str:
        """把交接包摊平成 CSV：一行一条关系，缺少证据的边界仍以「证据欠缺」出现。"""
        import csv
        import io

        output = io.StringIO()
        output.write("﻿")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow([
            "包编号", "关系编号", "上游表名", "上游库名", "下游表名", "下游库名",
            "是否在交接范围", "来源字段", "目标字段", "转换逻辑", "证据状态", "边界说明",
        ])
        for view in [*package.get("关系中转", []), *package.get("关系边界", [])]:
            writer.writerow([
                package.get("包编号"),
                view.get("关系编号"),
                view.get("上游表名"),
                view.get("上游库名"),
                view.get("下游表名"),
                view.get("下游库名"),
                "是" if view.get("是否在交接范围") else "否",
                view.get("来源字段", ""),
                view.get("目标字段", ""),
                view.get("转换逻辑", ""),
                view.get("证据状态", EVIDENCE_CONFIRMED),
                view.get("边界说明", ""),
            ])
        writer.writerow([])
        writer.writerow(["删除影响核对", "关系编号", "方向", "对端表名", "对端库名", "匹配字段", "结论", "是否需补采", "说明"])
        for item in package.get("删除影响", []):
            writer.writerow([
                "删除影响",
                item.get("关系编号"),
                item.get("方向"),
                item.get("对端表名"),
                item.get("对端库名"),
                "、".join(item.get("匹配字段") or []),
                item.get("结论"),
                "是" if item.get("需补采") else "否",
                item.get("说明"),
            ])
        return output.getvalue()

    def handoff_review_checklist(self, package: dict[str, Any]) -> list[dict[str, Any]]:
        """把交接包结论同步成元数据变更复核清单条目，供变更对比模块消费。"""
        approval = package.get("审批清单", {})
        batch = package.get("包编号")
        checklist: list[dict[str, Any]] = []
        for item in approval.get("可处理对象", []):
            checklist.append({
                "清单类型": "可处理对象",
                "包编号": batch,
                "关系编号": item["关系编号"],
                "方向": item.get("方向"),
                "对象": f"{package.get('事实表')}.{item['对端表名']}",
                "结论": item["结论"],
                "待办": f"审批后处理字段依赖：{'、'.join(item.get('待改字段') or [])}",
                "优先级": "高",
            })
        for item in approval.get("需补采边界", []):
            checklist.append({
                "清单类型": "需补采边界",
                "包编号": batch,
                "关系编号": item["关系编号"],
                "方向": item.get("方向"),
                "对象": f"{package.get('事实表')}.{item['对端表名']}",
                "结论": item["结论"],
                "待办": f"跨库「{item['对端库名']}」补采字段级血缘后再判定是否断链",
                "优先级": "高",
            })
        for item in approval.get("确认无依赖", []):
            checklist.append({
                "清单类型": "确认无依赖",
                "包编号": batch,
                "关系编号": item["关系编号"],
                "方向": item.get("方向"),
                "对象": f"{package.get('事实表')}.{item['对端表名']}",
                "结论": item["结论"],
                "待办": "无需处理，仅留证备查",
                "优先级": "低",
            })
        return checklist

    def validate_handoff_package(self, package: dict[str, Any]) -> list[str]:
        """回放前自检：范围外明细不得泄漏，三态证据不得混淆。"""
        problems: list[str] = []
        scope = {str(item) for item in package.get("交接库范围", [])}
        if not scope:
            problems.append("交接库范围为空")
        for view in package.get("关系边界", []):
            if view.get("是否在交接范围"):
                problems.append(f"边界关系 {view.get('关系编号')} 被误标为范围内")
            if view.get("来源字段") not in (None, "", REDACTED_VALUE):
                problems.append(f"边界关系 {view.get('关系编号')} 泄漏了来源字段")
            if view.get("转换逻辑") not in (None, "", REDACTED_VALUE):
                problems.append(f"边界关系 {view.get('关系编号')} 泄漏了转换逻辑")
            if view.get("证据状态") != EVIDENCE_MISSING:
                problems.append(f"边界关系 {view.get('关系编号')} 未标记证据欠缺")
        for item in package.get("删除影响", []):
            if item.get("结论") not in (EVIDENCE_CONFIRMED, EVIDENCE_MISSING, EVIDENCE_NONE):
                problems.append(f"删除影响 {item.get('关系编号')} 结论不在三态之内")
            if item.get("需补采") and item.get("结论") != EVIDENCE_MISSING:
                problems.append(f"删除影响 {item.get('关系编号')} 需补采却给了确定结论")
        return problems