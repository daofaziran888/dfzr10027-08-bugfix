"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self.reset()

    def reset(self) -> None:
        """原地恢复种子数据。

        各业务模块在导入时就绑定了 ``store`` 实例，这里必须保持同一个对象、
        只重置内部表，否则重置后的写入会落到旧实例里。
        """
        self._tables = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._tables.setdefault("_pipeline_results", [])
        # 立方体快照与构建记录的生命周期跟随管道产物，重置时一并清空。
        self._tables.setdefault("_cube_snapshots", [])
        self._tables.setdefault("_cube_builds", [])

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            # 下划线开头的是管道/快照等内部产物表，不算业务模块。
            if name.startswith("_"):
                continue
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()


def reset_store() -> None:
    """重新恢复全局 store 的种子数据，用于测试隔离。"""
    store.reset()
