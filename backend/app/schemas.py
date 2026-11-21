"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None


class HandoffPackageRequest(BaseModel):
    databases: list[str] = Field(default_factory=list)
    table: str
    deleted_fields: list[str] = Field(default_factory=list)
    package_id: str | None = None


class HandoffInterruptRequest(BaseModel):
    databases: list[str] = Field(default_factory=list)
    table: str
    deleted_fields: list[str] = Field(default_factory=list)
    completed_steps: list[str] = Field(default_factory=list)
    extracted_relations: list[dict[str, Any]] = Field(default_factory=list)


class FieldDeletionRequest(BaseModel):
    table: str
    deleted_fields: list[str] = Field(default_factory=list)
    databases: list[str] = Field(default_factory=list)
    package_id: str | None = None
