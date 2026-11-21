"""血缘交接包接口：生成、中断续做、下载和变更对比消费。

固定路径单独成一个路由文件，并在 main.py 里先于通用 /api/{module} 注册，
否则 `handoff/...` 会被动态模块路径抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from app.schemas import FieldDeletionRequest, HandoffInterruptRequest, HandoffPackageRequest
from app.services.lineage_graph import LineageGraphService
from app.services.metadata_diff import MetadataDiffService

router = APIRouter(prefix="/api/handoff", tags=["血缘交接包"])


def _lineage() -> LineageGraphService:
    return LineageGraphService()


@router.post("/packages")
def create_package(payload: HandoffPackageRequest) -> dict[str, Any]:
    """为指定库范围生成可回放的血缘交接包。"""
    try:
        return _lineage().build_handoff_package(
            databases=payload.databases,
            table=payload.table,
            deleted_fields=payload.deleted_fields,
            package_id=payload.package_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/packages/latest")
def latest_package(table: str | None = None) -> dict[str, Any]:
    package = _lineage().latest_handoff_package(table)
    if package is None:
        raise HTTPException(status_code=404, detail="尚无血缘交接包")
    return package


@router.get("/packages/{package_id}")
def get_package(package_id: str) -> dict[str, Any]:
    package = _lineage().get_handoff_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail=f"交接包 {package_id} 不存在")
    return package


@router.post("/packages/{package_id}/interrupt")
def interrupt_package(package_id: str, payload: HandoffInterruptRequest) -> dict[str, Any]:
    """记录一次导出中断：冻结已剔除的范围外依赖，重试时仍按「证据欠缺」保留。"""
    return _lineage().mark_handoff_interrupted(
        package_id,
        completed_steps=payload.completed_steps,
        extracted_relations=payload.extracted_relations,
        databases=payload.databases,
        table=payload.table,
        deleted_fields=payload.deleted_fields,
    )


@router.get("/packages/{package_id}/download")
def download_package(package_id: str):
    """下载交接包：JSON 为完整可回放材料，CSV 便于直接对照关系与影响清单。"""
    package = _lineage().get_handoff_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail=f"交接包 {package_id} 不存在")
    return JSONResponse(
        content=package,
        headers={
            "Content-Disposition": f'attachment; filename="{package_id}.json"',
            "X-Handoff-Package": package_id,
        },
    )


@router.get("/packages/{package_id}/download.csv")
def download_package_csv(package_id: str) -> PlainTextResponse:
    package = _lineage().get_handoff_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail=f"交接包 {package_id} 不存在")
    return PlainTextResponse(
        _lineage().export_handoff_csv(package),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{package_id}.csv"'},
    )


@router.post("/field-deletions")
def submit_field_deletion(payload: FieldDeletionRequest) -> dict[str, Any]:
    """提交一次字段删除：交接包 + 复核清单同步 + 影响清单一次返回。"""
    try:
        return MetadataDiffService().submit_field_deletion(
            table=payload.table,
            deleted_fields=payload.deleted_fields,
            databases=payload.databases,
            package_id=payload.package_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/packages/{package_id}/comparison")
def compare_package(package_id: str) -> dict[str, Any]:
    """变更对比视角：已确认关系与仍需跨库确认的边界分开列出。"""
    package = _lineage().get_handoff_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail=f"交接包 {package_id} 不存在")
    return MetadataDiffService().compare_handoff_package(package)


@router.get("/packages/{package_id}/replay")
def replay_package(package_id: str) -> dict[str, Any]:
    """按包内库范围与事实表重放，核对交接材料是否仍然可回放。"""
    package = _lineage().get_handoff_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail=f"交接包 {package_id} 不存在")
    return _lineage().replay_handoff_package(package)
