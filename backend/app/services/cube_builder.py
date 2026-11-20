"""多维立方体 DAG 图服务：生成节点、边示例结构与流转规则。"""
from __future__ import annotations

from typing import Any

from app.services.cube_snapshot import (
    build_cube_snapshot,
    cube_snapshot_by_version,
    latest_cube_build,
    normalize_dim_codes,
    snapshot_from_build,
    validate_dimensions,
)
from app.store import store

MODULE = "cube_builder"
REQUIRED_FIELDS = ["立方体编号", "事实来源", "维度列表"]
STATUS_ORDER = ["草稿", "构建中", "可用", "已过期"]
ACTION_RULES = {"启动构建": "构建中", "标记可用": "可用", "标记过期": "已过期"}
NEGATIVE_ACTIONS = []


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


class CubeBuilderService:
    def get_graph(self) -> dict[str, Any]:
        rows = store.rows(MODULE)
        nodes: list[dict] = []
        edges: list[dict] = []
        for i, row in enumerate(rows):
            nodes.append({"id": int(row.get("id", i+1)), "name": str(row.get(list(row.keys())[1] if len(row)>1 else "name", "")), "status": str(row.get("status", STATUS_ORDER[0])),
                          **{k: row[k] for k in row if k not in ("id","status","pending","abnormal")}})
        for i in range(len(nodes)-1):
            edges.append({"from": nodes[i]["id"], "to": nodes[i+1]["id"], "label": "依赖"})
        return {"nodes": nodes, "edges": edges}

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return self._with_snapshot(entry)

    def _with_snapshot(self, entry: dict[str, Any]) -> dict[str, Any]:
        """详情与构建使用同一份快照，避免入口声明与详情分组对不上。"""
        detail = dict(entry)
        build = latest_cube_build(int(entry.get("id", 0)))
        if build is not None:
            detail["snapshot"] = snapshot_from_build(build)
            detail["snapshot_build_id"] = build.get("build_id")
            return detail
        version = detail.get("snapshot_version")
        if version:
            snapshot = cube_snapshot_by_version(str(version))
            if snapshot is not None:
                detail["snapshot"] = snapshot
        return detail

    def build_snapshot(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """校验立方体声明的维度并构建快照，供「启动构建」与构建入口复用。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"数据立方体 {entry_id} 不存在或已归档"

        requested = normalize_dim_codes(entry.get("维度列表"))
        _valid, missing = validate_dimensions(requested)
        outcome = build_cube_snapshot(requested, source_entry_id=entry_id)
        result = outcome["result"]
        build = outcome["build_record"]

        entry["snapshot_version"] = build.get("snapshot_version")
        entry["实际维度"] = build.get("actual_dimensions") or []
        entry["缺失维度"] = build.get("missing_dimensions") or []
        entry["分组覆盖"] = {
            "complete": bool(build.get("complete")),
            "status": build.get("status"),
            "coverage": build.get("coverage") or {},
        }
        entry["构建记录"] = build.get("build_id")
        entry["构建提示"] = build.get("message") or result.get("message")
        # 只有声明维度全部参与聚合时才算构建正常；缺维或保留旧快照要能被发现。
        entry["abnormal"] = bool(missing) or bool(build.get("retained"))
        return self._with_snapshot(entry), str(entry["构建提示"] or "")

    def snapshot_of(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        build = latest_cube_build(int(entry.get("id", 0)))
        if build is not None:
            return snapshot_from_build(build)
        return cube_snapshot_by_version(entry.get("snapshot_version"))

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(r.get("id",0)) for r in rows), default=0) + 1}
        entry.update({f: values.get(f) for f in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"数据立方体 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于多维立方体可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if action == "启动构建":
            built, message = self.build_snapshot(entry_id)
            if built is not None:
                return built, message or f"数据立方体已{action}"
        return entry, f"数据立方体已{action}"

    def raw_data(self) -> tuple[list[dict], list[dict]]:
        g = self.get_graph()
        return g["nodes"], g["edges"]
