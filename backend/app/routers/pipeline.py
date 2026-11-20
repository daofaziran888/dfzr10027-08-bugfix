"""数据管道接口：触发跨模块计算并查询产物。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult
from app.services.pipeline_orchestrator import (
    cube_query_result,
    cube_query_result_for,
    cube_report_view,
    cube_snapshot_by_version,
    get_cube_build,
    list_cube_builds,
    list_pipeline_results,
    run_all_pipelines,
    run_attribution_compare_pipeline,
    run_cohort_extract_pipeline,
    run_cube_pipeline,
    run_experiment_stat_pipeline,
    run_forecast_pipeline,
    run_metric_agg_pipeline,
    run_quality_scan,
)

router = APIRouter(prefix="/api/pipeline", tags=["数据管道"])


@router.post("/run-all", response_model=ActionResult)
def trigger_all() -> ActionResult:
    """顺序执行全部 7 条跨模块数据管道。"""
    summary = run_all_pipelines()
    return ActionResult(
        ok=True,
        message="全部管道已执行",
        entry={"pipelines": list(summary.keys()), "count": len(summary)},
    )


@router.post("/metric-agg", response_model=ActionResult)
def trigger_metric_agg(
    metric_code: str | None = Query(default=None, description="指标编号，如 METR-GMV"),
) -> ActionResult:
    result = run_metric_agg_pipeline(metric_code)
    return ActionResult(ok=True, message="指标聚合管道已执行", entry=result)


@router.post("/forecast", response_model=ActionResult)
def trigger_forecast(
    horizon: int = Query(default=7, ge=1, le=30, description="预测天数"),
) -> ActionResult:
    result = run_forecast_pipeline(horizon)
    return ActionResult(ok=True, message="预测管道已执行", entry=result)


@router.post("/cube", response_model=ActionResult)
def trigger_cube(
    dim_codes: list[str] | None = Query(default=None, description="维度编号列表"),
) -> ActionResult:
    result = run_cube_pipeline(dim_codes)
    if result.get("status") in ("缺维", "已保留", "无有效维度"):
        # 构建本身成功，但产物与请求维度不一致，调用方需要看到原因与缺失维度。
        return ActionResult(ok=True, message=result.get("message", "立方体快照未完整覆盖请求维度"), entry=result)
    return ActionResult(ok=True, message="立方体构建管道已执行", entry=result)


@router.get("/cube/result")
def get_cube_query_result(
    version: str | None = Query(default=None, description="快照版本；缺省返回当前已发布快照"),
    dim_codes: list[str] | None = Query(default=None, description="按请求维度查看覆盖情况"),
) -> dict[str, object]:
    """数据服务查询入口：按实际分组返回立方体结果及分组覆盖。"""
    if version:
        snapshot = cube_snapshot_by_version(version)
        if snapshot is None:
            raise HTTPException(status_code=404, detail=f"立方体快照 {version} 不存在")
        if dim_codes:
            return cube_report_view(dim_codes, version=version)
        return cube_query_result_for(snapshot)
    if dim_codes:
        return cube_report_view(dim_codes)
    return cube_query_result()


@router.get("/cube/snapshot")
def get_current_cube_snapshot() -> dict:
    """当前立方体快照详情：只包含实际参与聚合的维度与分组。"""
    snapshot = cube_snapshot_by_version()
    if snapshot is None:
        raise HTTPException(status_code=404, detail="尚无立方体快照，请先执行立方体构建")
    return snapshot


@router.get("/cube/snapshot/{version}")
def get_cube_snapshot(version: str) -> dict:
    snapshot = cube_snapshot_by_version(version)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"立方体快照 {version} 不存在")
    return snapshot


@router.get("/cube/builds")
def get_cube_builds() -> dict[str, object]:
    """立方体构建记录：每次请求的维度、实际分组与覆盖情况。"""
    items = list_cube_builds()
    return {"items": items, "total": len(items)}


@router.get("/cube/builds/{build_id}")
def get_cube_build_detail(build_id: int) -> dict:
    build = get_cube_build(build_id)
    if build is None:
        raise HTTPException(status_code=404, detail=f"立方体构建记录 {build_id} 不存在")
    return build


@router.post("/cohort-extract", response_model=ActionResult)
def trigger_cohort_extract() -> ActionResult:
    result = run_cohort_extract_pipeline()
    return ActionResult(ok=True, message="分群提取管道已执行", entry=result)


@router.post("/experiment-stat", response_model=ActionResult)
def trigger_experiment_stat(
    experiment_code: str | None = Query(default=None, description="实验编号"),
) -> ActionResult:
    result = run_experiment_stat_pipeline(experiment_code)
    return ActionResult(ok=True, message="实验统计管道已执行", entry=result)


@router.post("/quality-scan", response_model=ActionResult)
def trigger_quality_scan() -> ActionResult:
    result = run_quality_scan()
    return ActionResult(ok=True, message="质量扫描管道已执行", entry=result)


@router.post("/attribution-compare", response_model=ActionResult)
def trigger_attribution_compare(
    link_code: str | None = Query(default=None, description="链路编号"),
) -> ActionResult:
    result = run_attribution_compare_pipeline(link_code)
    return ActionResult(ok=True, message="归因对比管道已执行", entry=result)


@router.get("/results")
def get_results() -> dict[str, object]:
    """查询全部管道产物。"""
    return {"items": list_pipeline_results()}
