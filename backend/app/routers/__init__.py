"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import lineage_handoff, resource

# 固定路径路由必须排在通用 /api/{module} 之前，否则会被动态路径抢先匹配。
ROUTERS = [lineage_handoff, resource]
