"""多维立方体快照构建：维度校验、分组聚合、覆盖说明与可复算版本。

立方体构建入口原先只按“已映射到的字段”静默聚合：无效维度编号被丢掉，
但快照仍然声明了请求里的全部维度，导致详情看起来像多维结果，实际却是
地区小计甚至全表合计。这里把维度校验、实际分组和版本计算收在一处，
构建入口与快照详情都读取同一份结果，避免两处口径再次分叉。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any, Iterable

from app.fixtures import DATA_MART_ROWS, DIMENSIONS
from app.store import store

PIPELINE_ID = "cube_api"
SNAPSHOT_SCHEMA = "cube-snapshot-v1"
BUILD_TABLE = "_cube_builds"
SNAPSHOT_TABLE = "_cube_snapshots"

# 维度编号 -> 数据集市行里的字段名。没有映射或数据集里没有该字段的维度
# 不能参与分组，否则只会在每个分组上挂一个空值，等同于没有按该维度聚合。
DIM_FIELD_MAP: dict[str, str] = {
    "DIM-REGION": "region",
    "DIM-CHANNEL": "channel",
    "DIM-USER-TAG": "tag",
    "DIM-PRODUCT": "category_id",
}

RETIRED_STATUSES = {"已失效", "已退役", "已下线", "停用", "废弃"}
INACTIVE_FLAGS = {"N", "NO", "FALSE", "0", "否", "无效", "禁用"}

_DIM_SPLIT = re.compile(r"[,;，；\s]+")


def _to_float(val: Any) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _to_int(val: Any) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _text(val: Any) -> str:
    return "" if val is None else str(val).strip()


# ---------------------------------------------------------------------------
# 维度编号归一化与校验
# ---------------------------------------------------------------------------

def normalize_dim_codes(dim_codes: Iterable[str] | str | None) -> list[str]:
    """把请求里的维度组合归一化为去重且保持请求顺序的编号列表。

    兼容列表、逗号/分号/空白分隔的字符串；None 仍沿用默认的前两个维度。
    """
    if dim_codes is None:
        return [str(d.get("维度编号")) for d in DIMENSIONS[:2] if d.get("维度编号")]

    if isinstance(dim_codes, str):
        raw_parts: list[str] = _DIM_SPLIT.split(dim_codes)
    else:
        raw_parts = []
        for item in dim_codes:
            if item is None:
                continue
            if isinstance(item, str):
                raw_parts.extend(_DIM_SPLIT.split(item))
            else:
                raw_parts.append(str(item))

    result: list[str] = []
    seen: set[str] = set()
    for part in raw_parts:
        code = part.strip()
        if code and code not in seen:
            seen.add(code)
            result.append(code)
    return result


def _registry() -> dict[str, dict[str, Any]]:
    """合并 fixtures 与 store 的维度注册信息，store 中的状态优先。"""
    registry: dict[str, dict[str, Any]] = {}
    for row in DIMENSIONS:
        code = _text(row.get("维度编号"))
        if code:
            registry[code] = dict(row)
    for row in store.rows("dim_registry"):
        code = _text(row.get("维度编号"))
        if not code:
            continue
        merged = dict(registry.get(code, {}))
        merged.update(row)
        registry[code] = merged
    return registry


def _is_retired(row: dict[str, Any]) -> bool:
    status = _text(row.get("status"))
    if status in RETIRED_STATUSES:
        return True
    flag = _text(row.get("生效标记")).upper()
    if flag in INACTIVE_FLAGS:
        return True
    return row.get("enabled") is False or row.get("active") is False


def _field_available(field: str, rows: list[dict[str, Any]]) -> bool:
    return any(field in row for row in rows)


def validate_dimensions(
    dim_codes: list[str],
    rows: list[dict[str, Any]] | None = None,
) -> tuple[list[tuple[str, str]], dict[str, dict[str, Any]]]:
    """校验维度编号是否真的能参与聚合。

    返回 (有效维度及字段, 无效维度明细)。只有“已注册、未退役、数据集里
    确实存在分组字段”的维度才算有效；其余维度必须被排除出快照声明。
    """
    registry = _registry()
    dataset = list(DATA_MART_ROWS) if rows is None else rows
    valid: list[tuple[str, str]] = []
    missing: dict[str, dict[str, Any]] = {}

    for code in dim_codes:
        row = registry.get(code)
        reason: str | None = None
        category = "invalid"

        if row is None:
            reason = f"维度编号 {code} 未注册，属于无效编号"
        elif _is_retired(row):
            category = "retired"
            reason = f"维度 {code} 已退役，不能参与当前聚合"
        else:
            field = DIM_FIELD_MAP.get(code)
            if not field:
                reason = f"维度 {code} 未配置可聚合的数据字段"
            elif not _field_available(field, dataset):
                reason = f"维度 {code} 对应的数据字段 {field} 在事实数据中不存在"

        if reason is not None:
            missing[code] = {
                "维度编号": code,
                "维度名称": row.get("维度名称") if row else None,
                "原因": reason,
                "分类": category,
            }
        else:
            valid.append((code, DIM_FIELD_MAP[code]))

    return valid, missing


# ---------------------------------------------------------------------------
# 分组聚合与版本
# ---------------------------------------------------------------------------

def aggregate_cells(
    fields: list[str],
    rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """严格按给定字段分组聚合，生成的每个分组都包含全部声明字段。"""
    dataset = list(DATA_MART_ROWS) if rows is None else rows
    groups: dict[tuple, dict[str, Any]] = {}

    for row in dataset:
        key = tuple(row.get(field) for field in fields)
        cell = groups.get(key)
        if cell is None:
            cell = {field: row.get(field) for field in fields}
            cell["订单数"] = 0
            cell["gmv"] = 0.0
            cell["uv"] = 0
            groups[key] = cell
        cell["订单数"] += _to_int(row.get("订单数"))
        cell["gmv"] += _to_float(row.get("gmv"))
        cell["uv"] += _to_int(row.get("uv"))

    cells = []
    for cell in groups.values():
        cells.append({
            key: round(value, 2) if isinstance(value, float) else value
            for key, value in cell.items()
        })
    # 固定顺序，保证同一份数据与维度组合得到可复算的同一版本。
    cells.sort(key=lambda c: tuple(str(c.get(field, "")) for field in fields))
    return cells


def rollup_cells(
    cells: list[dict[str, Any]],
    fields: list[str],
) -> list[dict[str, Any]]:
    """把已有分组上卷到更少的维度，得到真正的维度小计。

    只有每个分组都带齐目标字段时才上卷；缺字段时原样返回，避免拼出
    看起来像小计、实际口径不明的数据。
    """
    if not fields or not cells:
        return copy.deepcopy(cells)
    if not all(all(field in cell for field in fields) for cell in cells):
        return copy.deepcopy(cells)

    groups: dict[tuple, dict[str, Any]] = {}
    for cell in cells:
        key = tuple(cell.get(field) for field in fields)
        target = groups.get(key)
        if target is None:
            target = {field: cell.get(field) for field in fields}
            target["订单数"] = 0
            target["gmv"] = 0.0
            target["uv"] = 0
            groups[key] = target
        target["订单数"] += _to_int(cell.get("订单数"))
        target["gmv"] += _to_float(cell.get("gmv"))
        target["uv"] += _to_int(cell.get("uv"))

    rolled = []
    for cell in groups.values():
        rolled.append({
            key: round(value, 2) if isinstance(value, float) else value
            for key, value in cell.items()
        })
    rolled.sort(key=lambda c: tuple(str(c.get(field, "")) for field in fields))
    return rolled


def compute_snapshot_version(
    dimensions: list[str],
    dim_fields: list[str],
    cells: list[dict[str, Any]],
) -> str:
    """按实际分组内容计算内容版本，同一请求复算应得到同一版本。"""
    body = {
        "schema": SNAPSHOT_SCHEMA,
        "dimensions": dimensions,
        "dim_fields": dim_fields,
        "cells": cells,
    }
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), default=str)
    return "cube-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _dimension_names(codes: list[str]) -> list[str | None]:
    registry = _registry()
    return [registry.get(code, {}).get("维度名称") for code in codes]


def _coverage(
    requested: list[str],
    actual: list[str],
    missing: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    missing_codes = [code for code in requested if code in missing]
    return {
        "requested_dimensions": list(requested),
        "actual_dimensions": list(actual),
        "missing_dimensions": missing_codes,
        "complete": bool(actual) and not missing_codes,
        # 请求维度与实际分组维度不一致时，统计范围发生冲突，调用方不能把
        # 部分分组当成完整的多维结果。
        "conflict": bool(missing_codes),
        "dimension_coverage": round(len(actual) / len(requested), 4) if requested else 0.0,
        "missing_details": copy.deepcopy(list(missing.values())),
    }


def _metric_totals(cells: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "订单数": sum(_to_int(cell.get("订单数")) for cell in cells),
        "gmv": round(sum(_to_float(cell.get("gmv")) for cell in cells), 2),
        "uv": sum(_to_int(cell.get("uv")) for cell in cells),
    }


def _split_missing(missing: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    invalid = [code for code, detail in missing.items() if detail.get("分类") != "retired"]
    retired = [code for code, detail in missing.items() if detail.get("分类") == "retired"]
    return invalid, retired


# ---------------------------------------------------------------------------
# 快照内容
# ---------------------------------------------------------------------------

def _snapshot_payload(
    *,
    requested: list[str],
    dimensions: list[str],
    dim_fields: list[str],
    cells: list[dict[str, Any]],
    missing: dict[str, dict[str, Any]],
    version: str,
) -> dict[str, Any]:
    invalid, retired = _split_missing(missing)
    missing_codes = [code for code in requested if code in missing]
    coverage = _coverage(requested, dimensions, missing)
    complete = coverage["complete"]
    snapshot = {
        "snapshot_version": version,
        "version": version,
        "dimensions": list(dimensions),
        "actual_dimensions": list(dimensions),
        "group_fields": list(dim_fields),
        "dim_fields": list(dim_fields),
        "dimension_names": _dimension_names(dimensions),
        "requested_dimensions": list(requested),
        "missing_dimensions": missing_codes,
        "invalid_dimensions": invalid,
        "retired_dimensions": retired,
        "ignored_dimensions": list(missing_codes),
        "missing_dimension_details": copy.deepcopy(list(missing.values())),
        "complete": complete,
        "status": "完整" if complete else "缺维",
        # 计算结果与快照版本一起可见：状态、实际分组和版本在同一个产物里。
        "snapshot_status": "完整" if complete else "缺维",
        "coverage": coverage,
        "group_coverage": copy.deepcopy(coverage),
        "cell_count": len(cells),
        "group_count": len(cells),
        "cells": copy.deepcopy(cells),
        "totals": _metric_totals(cells),
        "retained": False,
    }
    return snapshot


def _empty_payload(requested: list[str], missing: dict[str, dict[str, Any]]) -> dict[str, Any]:
    payload = _snapshot_payload(
        requested=requested,
        dimensions=[],
        dim_fields=[],
        cells=[],
        missing=missing,
        version="",
    )
    payload.update({
        "status": "无有效维度",
        "snapshot_status": "无有效维度",
        "retained": False,
        # 没有任何有效维度时连合计都不产出，避免被当成全表合计使用。
        "totals": {},
        "message": "请求没有任何有效维度，且没有历史快照可保留，未生成全表合计",
    })
    return payload


# ---------------------------------------------------------------------------
# 持久化：快照、构建记录与当前可查询结果
# ---------------------------------------------------------------------------

def _current_pipeline_entry() -> dict[str, Any] | None:
    for row in store.rows("_pipeline_results"):
        if row.get("pipeline_id") == PIPELINE_ID:
            return row
    return None


def current_cube_snapshot() -> dict[str, Any] | None:
    """返回当前已发布快照的完整详情（数据服务与快照详情共用这一份）。"""
    row = _current_pipeline_entry()
    if row is None:
        return None
    payload = copy.deepcopy(row.get("payload") or {})
    if not payload:
        return None
    payload.setdefault("snapshot_version", payload.get("version", ""))
    payload.setdefault("version", payload.get("snapshot_version", ""))
    return payload


def _find_snapshot(version: str) -> dict[str, Any] | None:
    # 当前已发布快照保留了本次请求的覆盖信息，同一版本优先返回它，
    # 避免同一版本从“缺维”读成“完整”。
    current = current_cube_snapshot()
    if current and current.get("snapshot_version") == version:
        return current
    for row in store.rows(SNAPSHOT_TABLE):
        if row.get("snapshot_version") == version:
            return copy.deepcopy(row)
    return None


def cube_snapshot_by_version(version: str | None = None) -> dict[str, Any] | None:
    if not version:
        return current_cube_snapshot()
    return _find_snapshot(version)


def _remember_snapshot(payload: dict[str, Any]) -> None:
    """按版本保存可复算的分组内容。

    版本内容只由维度与分组决定，因此同一版本无论由哪次请求产生，读回来的
    实际分组都一致；请求维度的差异保留在构建记录里。
    """
    version = payload.get("snapshot_version")
    if not version:
        return
    dimensions = list(payload.get("dimensions") or [])
    content = {
        "snapshot_version": version,
        "version": version,
        "dimensions": dimensions,
        "dim_fields": list(payload.get("dim_fields") or []),
        "dimension_names": list(payload.get("dimension_names") or []),
        "requested_dimensions": dimensions,
        "missing_dimensions": [],
        "invalid_dimensions": [],
        "retired_dimensions": [],
        "missing_dimension_details": [],
        "complete": True,
        "status": "完整",
        "cells": copy.deepcopy(payload.get("cells") or []),
        "cell_count": payload.get("cell_count", 0),
        "totals": copy.deepcopy(payload.get("totals") or {}),
        "coverage": {
            "requested_dimensions": dimensions,
            "actual_dimensions": dimensions,
            "missing_dimensions": [],
            "complete": True,
            "conflict": False,
            "dimension_coverage": 1.0,
            "missing_details": [],
        },
        "retained": False,
    }
    rows = store.rows(SNAPSHOT_TABLE)
    for row in rows:
        if row.get("snapshot_version") == version:
            row.update(content)
            return
    rows.append(content)


def _next_build_id() -> int:
    rows = store.rows(BUILD_TABLE)
    return max((_to_int(row.get("build_id")) for row in rows), default=0) + 1


def _append_build(
    *,
    requested: list[str],
    payload: dict[str, Any],
    missing: dict[str, dict[str, Any]],
    retained: bool,
    status: str,
    message: str,
    source_entry_id: int | None = None,
) -> dict[str, Any]:
    invalid, retired = _split_missing(missing)
    build_id = _next_build_id()
    record = {
        "id": build_id,
        "build_id": build_id,
        "pipeline_id": PIPELINE_ID,
        "source_entry_id": source_entry_id,
        "requested_dimensions": list(requested),
        "actual_dimensions": list(payload.get("dimensions") or []),
        "dimensions": list(payload.get("dimensions") or []),
        "dim_fields": list(payload.get("dim_fields") or []),
        "missing_dimensions": [code for code in requested if code in missing],
        "invalid_dimensions": invalid,
        "retired_dimensions": retired,
        "missing_dimension_details": copy.deepcopy(list(missing.values())),
        "complete": bool(payload.get("complete")),
        "status": status,
        "retained": retained,
        "snapshot_version": payload.get("snapshot_version"),
        "cell_count": payload.get("cell_count", 0),
        "cells": copy.deepcopy(payload.get("cells") or []),
        "coverage": copy.deepcopy(payload.get("coverage") or {}),
        "message": message,
        "created_at": datetime.now().isoformat(),
    }
    store.rows(BUILD_TABLE).append(record)
    return copy.deepcopy(record)


def build_cube_snapshot(
    dim_codes: Iterable[str] | str | None = None,
    *,
    source_entry_id: int | None = None,
) -> dict[str, Any]:
    """构建（或保留）一份立方体快照。

    返回 ``result``（构建入口响应 / 快照详情）、``publish``（需要写回数据
    服务的快照，保留旧快照时为 None）、``build_record`` 与 ``retained``。
    """
    requested = normalize_dim_codes(dim_codes)
    valid, missing = validate_dimensions(requested)
    previous = current_cube_snapshot()

    if not valid:
        actual_previous = list(previous.get("dimensions") or []) if previous else []
        if previous is not None and actual_previous:
            # 没有有效维度时不重新聚合成全表合计，原快照与已有可用分组保持
            # 可查询；响应仍然说明本次请求缺失了哪些维度。
            result = copy.deepcopy(previous)
            coverage = _coverage(requested, actual_previous, missing)
            invalid, retired = _split_missing(missing)
            missing_codes = [code for code in requested if code in missing]
            result.update({
                "requested_dimensions": requested,
                "missing_dimensions": missing_codes,
                "invalid_dimensions": invalid,
                "retired_dimensions": retired,
                "ignored_dimensions": list(missing_codes),
                "missing_dimension_details": copy.deepcopy(list(missing.values())),
                "complete": False,
                "status": "已保留",
                "coverage": coverage,
                "group_coverage": copy.deepcopy(coverage),
                "retained": True,
                "message": (
                    "请求没有任何有效维度，已保留上一份快照 "
                    f"{result.get('snapshot_version')}，未生成全表合计"
                ),
            })
            build = _append_build(
                requested=requested,
                payload=result,
                missing=missing,
                retained=True,
                status="已保留",
                message=result["message"],
                source_entry_id=source_entry_id,
            )
            result["build_record"] = copy.deepcopy(build)
            result["build"] = copy.deepcopy(build)
            result["snapshot"] = _snapshot_view(result, retained=True)
            result["snapshot_detail"] = result["snapshot"]
            return {
                "result": result,
                "publish": None,
                "build_record": build,
                "retained": True,
            }

        result = _empty_payload(requested, missing)
        build = _append_build(
            requested=requested,
            payload=result,
            missing=missing,
            retained=False,
            status="无有效维度",
            message=result["message"],
            source_entry_id=source_entry_id,
        )
        result["build_record"] = copy.deepcopy(build)
        result["build"] = copy.deepcopy(build)
        result["snapshot"] = _snapshot_view(result, retained=False)
        result["snapshot_detail"] = result["snapshot"]
        return {
            "result": result,
            "publish": None,
            "build_record": build,
            "retained": False,
        }

    dimensions = [code for code, _ in valid]
    dim_fields = [field for _, field in valid]
    cells = aggregate_cells(dim_fields)
    version = compute_snapshot_version(dimensions, dim_fields, cells)
    result = _snapshot_payload(
        requested=requested,
        dimensions=dimensions,
        dim_fields=dim_fields,
        cells=cells,
        missing=missing,
        version=version,
    )

    missing_codes = [code for code in requested if code in missing]
    if missing_codes:
        message = (
            "维度 " + "、".join(missing_codes)
            + " 不可用，已按实际可见维度 " + "、".join(dimensions)
            + " 生成缺维快照，不能当作按 " + "、".join(requested) + " 聚合的结果"
        )
    else:
        message = f"立方体快照已生成：{'、'.join(dimensions)}，共 {len(cells)} 个分组"

    result["message"] = message
    result["snapshot"] = _snapshot_view(result, retained=False)
    result["snapshot_detail"] = result["snapshot"]

    build = _append_build(
        requested=requested,
        payload=result,
        missing=missing,
        retained=False,
        status=result["status"],
        message=message,
        source_entry_id=source_entry_id,
    )
    result["build_record"] = copy.deepcopy(build)
    result["build"] = copy.deepcopy(build)
    _remember_snapshot(result)

    return {
        "result": result,
        "publish": result,
        "build_record": build,
        "retained": False,
    }


def _snapshot_view(payload: dict[str, Any], *, retained: bool) -> dict[str, Any]:
    """构建记录与详情页面共用的快照视图，只包含实际分组。"""
    return {
        "snapshot_version": payload.get("snapshot_version"),
        "version": payload.get("version"),
        "requested_dimensions": list(payload.get("requested_dimensions") or []),
        "dimensions": list(payload.get("dimensions") or []),
        "dim_fields": list(payload.get("dim_fields") or []),
        "dimension_names": list(payload.get("dimension_names") or []),
        "cell_count": payload.get("cell_count", 0),
        "cells": copy.deepcopy(payload.get("cells") or []),
        "totals": copy.deepcopy(payload.get("totals") or {}),
        "complete": bool(payload.get("complete")),
        "status": payload.get("status"),
        "coverage": copy.deepcopy(payload.get("coverage") or {}),
        "group_coverage": copy.deepcopy(payload.get("group_coverage") or payload.get("coverage") or {}),
        "missing_dimensions": list(payload.get("missing_dimensions") or []),
        "invalid_dimensions": list(payload.get("invalid_dimensions") or []),
        "retired_dimensions": list(payload.get("retired_dimensions") or []),
        "missing_dimension_details": copy.deepcopy(payload.get("missing_dimension_details") or []),
        "retained": retained,
    }


def snapshot_from_build(build: dict[str, Any] | None) -> dict[str, Any] | None:
    """由构建记录还原快照详情，避免同一版本被后续请求改写后串味。"""
    if not build:
        return None
    view = _snapshot_view(build, retained=bool(build.get("retained")))
    view["build_id"] = build.get("build_id")
    view["message"] = build.get("message", "")
    view["source_entry_id"] = build.get("source_entry_id")
    return view


# ---------------------------------------------------------------------------
# 查询接口
# ---------------------------------------------------------------------------

def list_cube_builds() -> list[dict[str, Any]]:
    builds = []
    for row in store.rows(BUILD_TABLE):
        record = copy.deepcopy(row)
        # 列表只带摘要，cells 走详情接口，避免同一版本被后续请求覆盖时误读。
        record.pop("cells", None)
        builds.append(record)
    return builds


def latest_cube_build(source_entry_id: int | None = None) -> dict[str, Any] | None:
    rows = [
        row for row in store.rows(BUILD_TABLE)
        if source_entry_id is None or _to_int(row.get("source_entry_id")) == _to_int(source_entry_id)
    ]
    if not rows:
        return None
    latest = copy.deepcopy(rows[-1])
    latest["snapshot"] = snapshot_from_build(latest)
    return latest


def get_cube_build(build_id: int) -> dict[str, Any] | None:
    for row in store.rows(BUILD_TABLE):
        if _to_int(row.get("build_id")) == _to_int(build_id):
            record = copy.deepcopy(row)
            record["snapshot"] = snapshot_from_build(record)
            return record
    return None


def cube_query_result() -> dict[str, Any]:
    """数据服务读取的立方体查询结果：实际分组 + 分组覆盖。"""
    snapshot = current_cube_snapshot()
    if snapshot is None:
        return cube_query_result_for(None)
    return cube_query_result_for(snapshot)


def cube_query_result_for(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """把一份快照整理成数据服务可查询的结果结构。"""
    if snapshot is None:
        return {
            "ok": False,
            "dataset": PIPELINE_ID,
            "status": "无快照",
            "complete": False,
            "dimensions": [],
            "dim_fields": [],
            "cells": [],
            "cell_count": 0,
            "coverage": {
                "requested_dimensions": [],
                "actual_dimensions": [],
                "missing_dimensions": [],
                "complete": False,
                "conflict": False,
                "dimension_coverage": 0.0,
                "missing_details": [],
            },
            "missing_dimensions": [],
            "message": "尚无立方体快照，请先执行立方体构建",
        }

    coverage = copy.deepcopy(snapshot.get("coverage") or {})
    result = {
        "ok": True,
        "dataset": PIPELINE_ID,
        "snapshot_version": snapshot.get("snapshot_version"),
        "version": snapshot.get("version"),
        "status": snapshot.get("status"),
        "complete": bool(snapshot.get("complete")),
        "requested_dimensions": list(snapshot.get("requested_dimensions") or []),
        "dimensions": list(snapshot.get("dimensions") or []),
        "dim_fields": list(snapshot.get("dim_fields") or []),
        "missing_dimensions": list(snapshot.get("missing_dimensions") or []),
        "invalid_dimensions": list(snapshot.get("invalid_dimensions") or []),
        "retired_dimensions": list(snapshot.get("retired_dimensions") or []),
        "coverage": coverage,
        "group_coverage": copy.deepcopy(coverage),
        "cell_count": snapshot.get("cell_count", 0),
        "cells": copy.deepcopy(snapshot.get("cells") or []),
        "retained": bool(snapshot.get("retained")),
        "message": snapshot.get("message", ""),
    }
    # 完整产物与缺维产物使用不同状态，调用方不必再解析 cells 猜测覆盖范围。
    if result["complete"]:
        result["result_status"] = "完整"
    elif result["retained"]:
        result["result_status"] = "已保留"
    else:
        result["result_status"] = "缺维"
    return result


# ---------------------------------------------------------------------------
# 报表口径：按请求维度取子集结果，并说明覆盖冲突
# ---------------------------------------------------------------------------

def cube_report_view(
    dim_codes: Iterable[str] | str | None = None,
    *,
    version: str | None = None,
) -> dict[str, Any]:
    """给报表/下游消费的立方体视图。

    报表可能同时请求一个可用维度与一个已退役维度：可用维度的小计仍然可以
    使用，但必须带 ``usable_as_requested=False`` 与覆盖冲突说明，避免把地区
    小计当成按两个维度聚合的结果。
    """
    snapshot = current_cube_snapshot() if version is None else cube_snapshot_by_version(version)
    query = cube_query_result_for(snapshot)
    actual = list(query.get("dimensions") or [])

    if dim_codes is not None:
        requested = normalize_dim_codes(dim_codes)
    else:
        # 未显式指定时按快照声明过的请求维度核对覆盖，避免把缺维产物
        # 直接当成完整结果。
        requested = list((snapshot or {}).get("requested_dimensions") or actual)

    if requested:
        _valid, missing = validate_dimensions(requested)
    else:
        missing = {}

    missing_codes: list[str] = []
    for code in requested:
        if (code in missing or code not in actual) and code not in missing_codes:
            missing_codes.append(code)
    conflict = bool(requested and missing_codes)
    coverage = {
        "requested_dimensions": requested,
        "actual_dimensions": actual,
        # 请求里没有真正参与聚合的维度，调用方可据此核对缺了哪些维度。
        "missing_dimensions": missing_codes,
        "complete": bool(requested) and not conflict and set(requested) <= set(actual),
        "conflict": conflict,
        "dimension_coverage": round(len(set(requested) & set(actual)) / len(requested), 4) if requested else 1.0,
        "missing_details": copy.deepcopy(list(missing.values())),
    }
    usable_as_requested = bool(coverage["complete"]) if requested else (
        bool(query.get("complete")) if query.get("ok") else False
    )
    if usable_as_requested and requested:
        message = query.get("message", "")
    elif requested:
        message = (
            f"请求维度 {'、'.join(requested)} 未全部参与聚合，当前分组只能按 "
            f"{'、'.join(actual) or '无'} 解读，不能当作完整多维结果"
        )
    else:
        message = query.get("message", "")

    # 快照按实际维度分组；这里按请求里仍然有效的维度上卷，得到真实小计，
    # 而不是拿实际分组冒充请求维度结果。
    cells = query.get("cells") or []
    dim_fields = list(query.get("dim_fields") or [])
    available = [
        (code, field)
        for code, field in zip(actual, dim_fields)
        if code in requested or not requested
    ]
    subtotal_fields = [field for _, field in available]
    subtotal_dimensions = [code for code, _ in available] or actual
    if len(subtotal_fields) < len(dim_fields):
        cells = rollup_cells(cells, subtotal_fields)
    subtotal_count = len(cells)
    is_subtotal = bool(requested) and not usable_as_requested and bool(subtotal_fields)

    return {
        "ok": bool(query.get("ok", False)),
        "dataset": PIPELINE_ID,
        "snapshot_version": query.get("snapshot_version"),
        "requested_dimensions": requested,
        "dimensions": actual,
        "dim_fields": query.get("dim_fields") or [],
        "cells": cells,
        "cell_count": subtotal_count,
        "snapshot_cell_count": query.get("cell_count", 0),
        "subtotal_dimensions": subtotal_dimensions,
        # 冲突时给出的是可用维度的小计；它不代表请求的全部维度。
        "subtotal_only": is_subtotal,
        "totals": copy.deepcopy((snapshot or {}).get("totals") or {}),
        "coverage": coverage,
        "group_coverage": copy.deepcopy(coverage),
        "missing_dimensions": missing_codes,
        "invalid_dimensions": [code for code in requested if code in missing and missing[code].get("分类") != "retired"],
        "retired_dimensions": [code for code in requested if code in missing and missing[code].get("分类") == "retired"],
        "status": query.get("status"),
        "complete": bool(coverage["complete"]),
        # 只有请求维度全部参与聚合时，才能把当前分组当作请求维度的结果。
        "usable_as_requested": usable_as_requested,
        "retained": query.get("retained", False),
        "message": message,
    }
