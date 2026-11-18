"""表统计信息业务规则：状态流转、字段校验、新分区布局核对都收在这里。

统计模块不自己造分区布局，而是通过 app.services.layout 读取分区演进模块已
落库的同一份新分区布局，按同一迁移批次重算影响行数，并把核对结果写回统计
模块自己的布局核对清单，保证计划详情与统计报告展示同一套分区和影响行数。
"""
from __future__ import annotations

from typing import Any

from app.services import layout as layout_rules
from app.store import store

MODULE = "stats"
REQUIRED_FIELDS = ["报告编号", "表名称", "行总数"]
STATUS_ORDER = ["草稿", "已采集", "已过期", "已清理"]
ACTION_RULES = {"采集统计": "草稿", "标记过期": "已采集", "清理报告": "已过期"}
NEGATIVE_ACTIONS: list[str] = []


class StatsService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("报告编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        optional_fields = ["文件总数", "总容量", "列统计", "统计时间"]
        for field in REQUIRED_FIELDS + optional_fields:
            if values.get(field) is not None:
                entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["报告状态"] = entry["status"]
        entry["pending"] = True
        entry["abnormal"] = False
        # 布局核对清单属于统计模块，只在这里落库
        entry["布局核对清单"] = []
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str, values: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"统计报告 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于表统计信息可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["报告状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        # 状态变化后重新比对当前新分区布局
        self.sync_partition_layouts(str(entry.get("表名称", "")))
        return entry, f"统计报告已{action}"

    # ----------------------------------------------------- 新分区布局核对
    def sync_partition_layouts(self, table_name: str | None = None) -> list[dict[str, Any]]:
        """读取分区演进模块同一份新分区布局，按同一迁移批次重算影响行数，
        并把最终核对结果写回布局核对清单。返回本次写入的核对条目。
        """
        written: list[dict[str, Any]] = []
        for report in store.rows(MODULE):
            if table_name is not None and str(report.get("表名称", "")) != str(table_name):
                continue
            written.extend(self._sync_one_report(report))
        return written

    def _sync_one_report(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        table_name = str(report.get("表名称", ""))
        plans = layout_rules.find_plans_for_table(table_name)
        checklist: list[dict[str, Any]] = []
        seen_batches: set[str] = set()

        for plan in plans:
            plan_status = str(plan.get("status", ""))
            for layout in layout_rules.plan_layouts(plan):
                batch_id = str(layout.get("迁移批次", ""))
                if batch_id in seen_batches:
                    # 同一迁移批次只保留一个布局版本的核对结果
                    continue
                seen_batches.add(batch_id)
                batch = next(
                    (item for item in layout_rules.plan_batches(plan) if item.get("迁移批次") == batch_id),
                    None,
                )
                if batch is None:
                    continue
                item = self._check_one(plan, layout, batch, plan_status, report)
                checklist.append(item)

        # 兼容已有明细：老报告没有核对字段时补上，已有清单按迁移批次刷新
        report["布局核对清单"] = checklist
        return checklist

    def _check_one(
        self,
        plan: dict[str, Any],
        layout: dict[str, Any],
        batch: dict[str, Any],
        plan_status: str,
        report: dict[str, Any],
    ) -> dict[str, Any]:
        result, note = layout_rules.check_result_for(plan_status)
        target_partition = str(layout.get("目标分区", ""))
        batch_rows = int(batch.get("影响行数", 0))

        item: dict[str, Any] = {
            "计划编号": plan.get("计划编号", ""),
            "迁移批次": batch.get("迁移批次", ""),
            "表名称": layout.get("表名称", ""),
            "当前分区": layout.get("旧分区布局", ""),
            "新分区布局": target_partition,
            "目标分区": target_partition,
            "布局版本": layout.get("版本号", ""),
            "演进状态": plan_status,
            "统计口径": layout.get("统计口径", ""),
            "影响行数": batch_rows,
            "重算影响行数": batch_rows,
            "核对结果": result,
            "核对说明": note,
        }

        if plan_status == layout_rules.STATUS_DONE:
            # 已完成：统计报告消费同一版本新分区布局，按同一迁移批次重算影响行数
            report_rows = layout_rules.parse_row_count(report.get("行总数"))
            item["重算影响行数"] = batch_rows
            if report_rows is not None and report_rows != batch_rows:
                item["核对结果"] = layout_rules.CHECK_DIFF
                item["核对说明"] = (
                    f"统计报告行总数 {report_rows} 与迁移批次 {batch['迁移批次']} "
                    f"重算影响行数 {batch_rows} 对不上，需补齐统计后再切换"
                )
        elif plan_status == layout_rules.STATUS_RUNNING:
            # 同一演进批次与旧统计分区时间冲突：统计补齐前保留旧分区布局
            item["新分区布局"] = layout.get("旧分区布局", "")
            item["重算影响行数"] = layout_rules.parse_row_count(report.get("行总数")) or 0
        elif plan_status == layout_rules.STATUS_ROLLED_BACK:
            item["新分区布局"] = layout.get("旧分区布局", "")
        return item

    def layout_checklist(self) -> list[dict[str, Any]]:
        """汇总所有统计报告的布局核对清单，供统计详情与刷新后展示，结果保持完整。"""
        items: list[dict[str, Any]] = []
        for report in store.rows(MODULE):
            for item in report.get("布局核对清单") or []:
                items.append({"报告编号": report.get("报告编号", ""), **item})
        return items
