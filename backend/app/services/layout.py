"""分区布局协同：分区演进与表统计共享同一份新分区布局的读取与核对口径。

布局对象只在分区演进模块落库一份（随迁移批次保存），表统计模块通过这里的
只读口径读取同一版本，再把核对结果写回统计模块自己的布局核对清单，避免布局
与统计口径分离成两份数据。
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from app.store import store

PARTITION_MODULE = "partition"
STATS_MODULE = "stats"

# 演进计划只允许沿这条序列单向推进
STATUS_DRAFT = "草稿"
STATUS_RUNNING = "执行中"
STATUS_DONE = "已完成"
STATUS_ROLLED_BACK = "已回滚"
STATUS_ORDER = [STATUS_DRAFT, STATUS_RUNNING, STATUS_DONE, STATUS_ROLLED_BACK]

# 动作 -> 允许执行该动作的起始状态（状态错位时直接拦下，不保存）
ACTION_FROM_STATUS = {
    "执行演进": STATUS_DRAFT,
    "归档计划": STATUS_RUNNING,
    "回滚演进": STATUS_RUNNING,
}
# 动作 -> 推进后的目标状态
ACTION_TO_STATUS = {
    "执行演进": STATUS_RUNNING,
    "归档计划": STATUS_DONE,
    "回滚演进": STATUS_ROLLED_BACK,
}

NEGATIVE_ACTIONS = ["回滚演进"]

# 统计模块对同一演进批次的两种口径
MODE_KEEP_OLD = "统计补齐前保留旧布局"
MODE_RECALC = "按新布局重算"

CHECK_MATCH = "一致"
CHECK_DIFF = "差异"
CHECK_KEEP_OLD = "保留旧布局"
CHECK_ROLLED_BACK = "已回滚"


def parse_row_count(value: Any) -> int | None:
    """把 18420000、'18420000'、'1842万'、'1842 万行' 统一解析成整数行数。

    解析不出来（缺失、非数字、负数）时返回 None，调用方据此停止保存。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    text = str(value).strip().replace(",", "").replace("，", "")
    if not text:
        return None
    wan = "万" in text
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group()) * (10000 if wan else 1)
    result = int(round(number))
    return result if result >= 0 else None


def next_batch_id() -> str:
    """生成当天不重复的迁移批次号：PART-YYYYMMDD-NN。"""
    prefix = f"PART-{date.today().strftime('%Y%m%d')}-"
    used = {
        str(batch.get("迁移批次", ""))
        for plan in store.rows(PARTITION_MODULE)
        for batch in plan.get("migration_batches", [])
    }
    seq = 1
    while f"{prefix}{seq:02d}" in used:
        seq += 1
    return f"{prefix}{seq:02d}"


def plan_layouts(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """读取一份演进计划下已落库的新分区布局（兼容没有布局字段的老明细）。"""
    return list(plan.get("layouts") or [])


def plan_batches(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """读取一份演进计划下的迁移批次。"""
    return list(plan.get("migration_batches") or [])


def latest_layout(plan: dict[str, Any]) -> dict[str, Any] | None:
    """一份计划同时只提交一个布局版本，取最后一个即当前有效版本。"""
    layouts = plan_layouts(plan)
    return layouts[-1] if layouts else None


def find_plans_for_table(table_name: str) -> list[dict[str, Any]]:
    """统计模块按表名读取同一份演进计划与新分区布局。"""
    return [
        plan
        for plan in store.rows(PARTITION_MODULE)
        if str(plan.get("表名称", "")) == str(table_name) and plan_layouts(plan)
    ]


def build_layout(
    plan: dict[str, Any],
    *,
    batch_id: str,
    target_partition: str,
    row_count: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成新分区布局。分区切换标准、影响范围、统计判定规则和允许条件
    共同构成布局的判定口径，缺一项都不允许落库。
    """
    old_partition = str(plan.get("当前分区", "")).strip()
    table_name = str(plan.get("表名称", "")).strip()
    extra = extra or {}
    layout = {
        "版本号": 1,
        "迁移批次": batch_id,
        "计划编号": plan.get("计划编号", ""),
        "表名称": table_name,
        "当前分区": old_partition,
        "旧分区布局": old_partition,
        "目标分区": target_partition,
        "新分区布局": target_partition,
        "影响行数": row_count,
        "分区切换标准": str(
            extra.get("分区切换标准")
            or f"当前分区「{old_partition}」在迁移批次 {batch_id} 内整体切换至「{target_partition}」，同一批次只允许一个布局版本落库"
        ),
        "影响范围": str(
            extra.get("影响范围")
            or f"表「{table_name}」迁移批次 {batch_id} 覆盖 {row_count} 行"
        ),
        "统计判定规则": str(
            extra.get("统计判定规则")
            or "执行中统计报告保留旧分区布局，归档完成后按新分区布局并以同一迁移批次重算影响行数"
        ),
        "允许条件": str(
            extra.get("允许条件")
            or "目标分区与影响行数随迁移批次完整保存，演进计划状态按草稿、执行中、已完成或已回滚单向推进"
        ),
        # 同一演进批次与旧统计分区发生时间冲突时，先保留旧布局，归档后再重算
        "统计口径": MODE_KEEP_OLD,
        "演进状态": STATUS_RUNNING,
        "生效时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "唯一版本": True,
    }
    return layout


def build_batch(
    *,
    batch_id: str,
    target_partition: str,
    row_count: int,
    layout_version: int,
    status: str,
) -> dict[str, Any]:
    """迁移批次必须同时带上目标分区与影响行数，两者缺一即停止保存。"""
    return {
        "迁移批次": batch_id,
        "目标分区": target_partition,
        "影响行数": row_count,
        "布局版本": layout_version,
        "批次状态": status,
        "落库时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def check_result_for(plan_status: str) -> tuple[str, str]:
    """根据演进计划状态给出统计核对结果与说明。"""
    if plan_status == STATUS_RUNNING:
        return CHECK_KEEP_OLD, "演进批次执行中，统计补齐前保留旧分区布局，暂不重算"
    if plan_status == STATUS_DONE:
        return CHECK_MATCH, "统计报告已按新分区布局重算，影响行数与迁移批次一致"
    if plan_status == STATUS_ROLLED_BACK:
        return CHECK_ROLLED_BACK, "演进计划已回滚，新分区布局作废，统计报告保留旧分区布局"
    return CHECK_KEEP_OLD, "演进计划尚未执行，统计报告沿用旧分区布局"
