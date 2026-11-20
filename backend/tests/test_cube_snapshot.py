"""立方体快照：维度校验、分组覆盖与保留策略。"""
from __future__ import annotations

from app.services.cube_builder import CubeBuilderService
from app.services.cube_snapshot import (
    cube_query_result,
    cube_report_view,
    cube_snapshot_by_version,
    get_cube_build,
    list_cube_builds,
)
from app.services.pipeline_orchestrator import run_cube_pipeline
from app.store import store


def _region_cells(result):
    return [{k: c[k] for k in ("region", "订单数", "gmv", "uv")} for c in result["cells"]]


class TestInvalidDimension:
    def test_invalid_code_does_not_return_full_table_total(self):
        result = run_cube_pipeline(["DIM-NOPE"])
        assert result["cells"] == []
        assert result["dimensions"] == []
        assert result["snapshot_version"] == ""
        assert result["missing_dimensions"] == ["DIM-NOPE"]
        assert result["status"] == "无有效维度"
        # 不能再出现“全表合计”这种单行结果
        assert result["totals"] == {}

    def test_mixed_codes_keep_only_actual_dimensions(self):
        result = run_cube_pipeline(["DIM-REGION", "DIM-NOPE"])
        assert result["dimensions"] == ["DIM-REGION"]
        assert result["requested_dimensions"] == ["DIM-REGION", "DIM-NOPE"]
        assert result["missing_dimensions"] == ["DIM-NOPE"]
        assert result["complete"] is False
        assert result["status"] == "缺维"
        for cell in result["cells"]:
            assert "region" in cell
            assert "channel" not in cell
            assert "DIM-NOPE" not in cell

    def test_every_declared_dimension_participates(self):
        result = run_cube_pipeline(["DIM-REGION", "DIM-CHANNEL"])
        assert result["dimensions"] == ["DIM-REGION", "DIM-CHANNEL"]
        for cell in result["cells"]:
            assert "region" in cell and "channel" in cell
        assert result["complete"] is True


class TestSnapshotDetail:
    def test_snapshot_detail_matches_actual_grouping(self):
        result = run_cube_pipeline(["DIM-REGION", "DIM-CHANNEL"])
        detail = cube_snapshot_by_version(result["snapshot_version"])
        assert detail is not None
        assert detail["dimensions"] == ["DIM-REGION", "DIM-CHANNEL"]
        assert detail["cells"] == result["cells"]
        assert detail["coverage"]["complete"] is True

    def test_builder_entry_detail_uses_built_snapshot(self):
        service = CubeBuilderService()
        # CUBE-ORDER 声明 DIM-PRODUCT，但事实数据没有 category_id 字段
        entry, message = service.build_snapshot(1)
        assert "DIM-PRODUCT" in entry["缺失维度"]
        assert "DIM-PRODUCT" in message
        assert entry["snapshot"]["dimensions"] == ["DIM-REGION", "DIM-CHANNEL"]
        detail = service.get_entry(1)
        assert detail["snapshot"]["dimensions"] == ["DIM-REGION", "DIM-CHANNEL"]
        assert detail["snapshot"]["cells"] == entry["snapshot"]["cells"]


class TestRetiredDimensionReport:
    def test_retired_dimension_conflicts_with_region(self):
        store.rows("dim_registry")[1]["status"] = "已失效"
        result = run_cube_pipeline(["DIM-REGION", "DIM-CHANNEL"])
        assert result["retired_dimensions"] == ["DIM-CHANNEL"]
        assert result["dimensions"] == ["DIM-REGION"]
        assert result["complete"] is False

        view = cube_report_view(["DIM-REGION", "DIM-CHANNEL"])
        assert view["usable_as_requested"] is False
        assert view["coverage"]["conflict"] is True
        assert view["missing_dimensions"] == ["DIM-CHANNEL"]
        # 地区小计仍然可取，只是不能被当成按两个维度聚合的结果
        assert view["subtotal_only"] is True
        assert view["subtotal_dimensions"] == ["DIM-REGION"]
        assert view["cells"] == result["cells"]
        assert all(set(cell) >= {"region", "订单数", "gmv", "uv"} for cell in view["cells"])
        assert all("channel" not in cell for cell in view["cells"])

    def test_region_only_remains_usable(self):
        store.rows("dim_registry")[1]["status"] = "已失效"
        run_cube_pipeline(["DIM-REGION", "DIM-CHANNEL"])
        view = cube_report_view(["DIM-REGION"])
        assert view["usable_as_requested"] is True
        assert view["coverage"]["complete"] is True


class TestRetention:
    def test_no_valid_dimension_keeps_previous_snapshot(self):
        first = run_cube_pipeline(["DIM-REGION"])
        first_cells = _region_cells(first)

        retained = run_cube_pipeline(["DIM-NOPE"])
        assert retained["status"] == "已保留"
        assert retained["retained"] is True
        assert retained["snapshot_version"] == first["snapshot_version"]
        assert retained["dimensions"] == ["DIM-REGION"]
        assert _region_cells(retained) == first_cells
        assert retained["missing_dimensions"] == ["DIM-NOPE"]

        query = cube_query_result()
        assert query["cells"] == first["cells"]
        assert query["snapshot_version"] == first["snapshot_version"]
        # 数据服务仍提供原快照的完整分组，不会被换成全表合计
        assert query["complete"] is True
        assert query["missing_dimensions"] == []

        build = get_cube_build(retained["build_record"]["build_id"])
        assert build["retained"] is True
        assert build["actual_dimensions"] == ["DIM-REGION"]
        assert build["missing_dimensions"] == ["DIM-NOPE"]


class TestRecomputableVersion:
    def test_region_subtotal_then_invalid_rebuild_keeps_values(self):
        region = run_cube_pipeline(["DIM-REGION"])
        values = {(c["region"], c["gmv"]) for c in region["cells"]}

        run_cube_pipeline(["DIM-XYZ"])
        query = cube_query_result()
        assert {(c["region"], c["gmv"]) for c in query["cells"]} == values
        assert query["snapshot_version"] == region["snapshot_version"]

    def test_region_plus_invalid_rebuilds_same_version(self):
        region = run_cube_pipeline(["DIM-REGION"])
        mixed = run_cube_pipeline(["DIM-REGION", "DIM-NOPE"])
        assert mixed["snapshot_version"] == region["snapshot_version"]
        assert mixed["cells"] == region["cells"]

        builds = list_cube_builds()
        assert len(builds) == 2
        assert builds[0]["actual_dimensions"] == ["DIM-REGION"]
        assert builds[1]["requested_dimensions"] == ["DIM-REGION", "DIM-NOPE"]
        assert builds[1]["actual_dimensions"] == ["DIM-REGION"]
        assert builds[1]["snapshot_version"] == region["snapshot_version"]

        detail = cube_snapshot_by_version(mixed["snapshot_version"])
        assert detail["dimensions"] == ["DIM-REGION"]
        assert detail["cells"] == region["cells"]


class TestDataService:
    def test_query_result_distinguishes_complete_and_missing(self):
        run_cube_pipeline(["DIM-REGION", "DIM-CHANNEL"])
        complete = cube_query_result()
        assert complete["complete"] is True
        assert complete["result_status"] == "完整"
        assert complete["missing_dimensions"] == []
        assert complete["coverage"]["actual_dimensions"] == ["DIM-REGION", "DIM-CHANNEL"]

        partial = run_cube_pipeline(["DIM-REGION", "DIM-NOPE"])
        query = cube_query_result()
        assert query["complete"] is False
        assert query["result_status"] == "缺维"
        assert query["missing_dimensions"] == ["DIM-NOPE"]
        assert query["coverage"]["requested_dimensions"] == ["DIM-REGION", "DIM-NOPE"]
        assert query["coverage"]["actual_dimensions"] == ["DIM-REGION"]
        assert query["cells"] == partial["cells"]

    def test_query_before_any_build(self):
        result = cube_query_result()
        assert result["ok"] is False
        assert result["cells"] == []
        assert result["result_status"] if "result_status" in result else True
