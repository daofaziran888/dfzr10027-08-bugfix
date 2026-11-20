"""多维立方体 DAG 拓扑接口：返回节点、连线图结构。"""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.cube_builder import CubeBuilderService

router = APIRouter(prefix="/api/cube_builder", tags=["多维立方体"])
service = CubeBuilderService()
LIST_FIELDS = ["立方体编号", "事实来源", "维度列表", "度量列表", "预计算粒度", "存储策略", "数据量级", "最后刷新"]
STATUSES = ["草稿", "构建中", "可用", "已过期"]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("")
def get_graph() -> dict[str, Any]:
    """返回节点与边组成的 DAG 图数据。"""
    return service.get_graph()


@router.get("/export")
def export_entries() -> StreamingResponse:
    nodes, edges = service.raw_data()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["type", "id", "name", "from", "to", "label", "status"])
    for node in nodes:
        writer.writerow(["node", node.get("id",""), csv_cell(node.get("name","")), "", "", "", node.get("status","")])
    for edge in edges:
        writer.writerow(["edge", "", "", edge.get("from",""), edge.get("to",""), edge.get("label",""), ""])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=cube_builder.csv"})


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"数据立方体 {entry_id} 不存在")
    return entry


@router.get("/{entry_id}/snapshot")
def get_snapshot(entry_id: int) -> dict:
    """立方体快照详情：返回实际参与聚合的维度与分组，而不是请求维度。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"数据立方体 {entry_id} 不存在")
    snapshot = service.snapshot_of(entry_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"数据立方体 {entry_id} 尚无可用快照")
    return snapshot


@router.post("/{entry_id}/build")
def build_entry(entry_id: int) -> ActionResult:
    """按声明的维度组合重建快照；无效编号会在构建前被拦下并说明缺失维度。"""
    entry, message = service.build_snapshot(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("")
def create_entry(payload: EntryPayload) -> ActionResult:
    entry_or_none, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="数据立方体已登记", entry=entry_or_none)


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    entry_or_none, message = service.run_action(entry_id, payload.action)
    if entry_or_none is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry_or_none)
