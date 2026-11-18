"""分区演进管理业务规则：状态流转、迁移批次、新分区布局与统计核对都收在这里。

修复要点：
1. 演进计划只能沿 草稿 → 执行中 → 已完成 / 已回滚 单向推进，动作从错误状态
   发起时直接拒绝，不再把状态写回起始状态；
2. 执行演进时生成新分区布局，并把目标分区、影响行数随迁移批次一起保存，
   目标分区缺失、影响行数对不上或计划状态错位时停止保存；
3. 一次提交只允许一个新分区布局版本落库，并兼容已有明细；
4. 推进后通知表统计模块按同一版本布局核对，差异写回布局核对清单。
"""
from __future__ import annotations

from typing import Any

from app.services import layout as layout_rules
from app.services.layout import ACTION_FROM_STATUS
from app.store import store

MODULE = "partition"
REQUIRED_FIELDS = ["计划编号", "表名称", "当前分区"]
STATUS_ORDER = layout_rules.STATUS_ORDER

# 迁移批次保存时必须同时具备的两项，缺一项布局与统计口径就会分离
BATCH_REQUIRED_FIELDS = ["目标分区", "影响行数"]
# 新分区布局必须携带的判定口径
LAYOUT_RULE_FIELDS = ["分区切换标准", "影响范围", "统计判定规则", "允许条件"]


class PartitionService:
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
            rows = [row for row in rows if keyword in str(row.get("计划编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        """读取演进计划明细，返回迁移批次与新分区布局的完整结果。"""
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        # 兼容已有明细：业务字段能存就存，不强制
        optional_fields = ["目标分区", "演化类型", "预计耗时", "影响行数"]
        for field in REQUIRED_FIELDS + optional_fields:
            if values.get(field) is not None:
                entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["计划状态"] = entry["status"]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["migration_batches"] = []
        entry["layouts"] = []
        rows.append(entry)
        return entry, []

    # ------------------------------------------------------------------ 动作
    def run_action(self, entry_id: int, action: str, values: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"演进计划 {entry_id} 不存在或已归档"
        if action not in ACTION_FROM_STATUS:
            return None, f"动作「{action}」不属于分区演进管理可执行范围"
        values = values or {}
        current = str(entry.get("status") or layout_rules.STATUS_DRAFT)
        expected = ACTION_FROM_STATUS[action]
        # 状态错位：只允许按草稿 → 执行中 → 已完成 / 已回滚 单向推进
        if current != expected:
            return None, f"演进计划当前为「{current}」，动作「{action}」只允许在「{expected}」状态执行，已停止保存"

        if action == "执行演进":
            ok, message = self._start_evolution(entry, values)
        elif action == "归档计划":
            ok, message = self._complete(entry)
        else:  # 回滚演进
            ok, message = self._rollback(entry)

        if not ok:
            return None, message

        # 状态推进后让表统计模块消费同一份新分区布局，核对结果写回核对清单
        self._sync_stats(entry)
        return entry, message

    # ------------------------------------------------------------- 执行演进
    def _start_evolution(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[bool, str]:
        target_partition = str(values.get("目标分区") or entry.get("目标分区") or "").strip()
        row_count = layout_rules.parse_row_count(values.get("影响行数", entry.get("影响行数")))

        # 目标分区与影响行数要随迁移批次保存，缺一项就停止保存
        if not target_partition:
            return False, "缺少目标分区，无法生成新分区布局，已停止保存迁移批次"
        if row_count is None:
            return False, "影响行数缺失或无法解析，无法与目标分区核对，已停止保存迁移批次"
        current_partition = str(entry.get("当前分区", "")).strip()
        if target_partition == current_partition:
            return False, "目标分区与当前分区一致，分区演进不会产生新分区布局，已停止保存"

        batch_id = str(values.get("迁移批次") or "").strip() or layout_rules.next_batch_id()
        # 同时提交时只让一个新分区布局版本落库：同批次号只接受一次
        if any(batch.get("迁移批次") == batch_id for batch in layout_rules.plan_batches(entry)):
            return False, f"迁移批次 {batch_id} 已落库一份新分区布局，同一批次不允许重复提交"

        extra = {field: values.get(field) for field in LAYOUT_RULE_FIELDS if values.get(field)}
        new_layout = layout_rules.build_layout(
            entry,
            batch_id=batch_id,
            target_partition=target_partition,
            row_count=row_count,
            extra=extra,
        )
        # 版本号只在已有布局的兼容明细上递增，保证一份计划的布局版本唯一有序
        new_layout["版本号"] = len(layout_rules.plan_layouts(entry)) + 1
        batch = layout_rules.build_batch(
            batch_id=batch_id,
            target_partition=target_partition,
            row_count=row_count,
            layout_version=new_layout["版本号"],
            status=layout_rules.STATUS_RUNNING,
        )

        # 落库前最后一道核对：影响行数与目标分区对不上就不保存
        if not all(str(batch.get(field) or "").strip() for field in BATCH_REQUIRED_FIELDS):
            return False, "迁移批次缺少目标分区或影响行数，已停止保存"
        if batch["影响行数"] != new_layout["影响行数"] or batch["目标分区"] != new_layout["目标分区"]:
            return False, "迁移批次的影响行数或目标分区与新分区布局对不上，已停止保存"

        entry.setdefault("migration_batches", []).append(batch)
        entry.setdefault("layouts", []).append(new_layout)
        entry["目标分区"] = target_partition
        entry["影响行数"] = row_count
        entry["迁移批次"] = batch_id
        entry["新分区布局"] = target_partition
        entry["布局版本"] = new_layout["版本号"]
        self._advance(entry, layout_rules.STATUS_RUNNING)
        return True, f"演进计划已执行演进，迁移批次 {batch_id} 已保存目标分区与影响行数 {row_count}"

    # ----------------------------------------------------------------- 归档
    def _complete(self, entry: dict[str, Any]) -> tuple[bool, str]:
        layout = layout_rules.latest_layout(entry)
        batch = layout_rules.plan_batches(entry)[-1] if layout_rules.plan_batches(entry) else None
        if layout is None or batch is None:
            return False, "尚未保存迁移批次与新分区布局，不能归档演进计划"
        if not all(str(batch.get(field) or "").strip() for field in BATCH_REQUIRED_FIELDS):
            return False, "迁移批次缺少目标分区或影响行数，已停止归档"
        # 归档即新布局正式生效：统计报告在同一批次按新布局重算
        layout["演进状态"] = layout_rules.STATUS_DONE
        layout["统计口径"] = layout_rules.MODE_RECALC
        batch["批次状态"] = layout_rules.STATUS_DONE
        self._advance(entry, layout_rules.STATUS_DONE)
        return True, f"演进计划已归档，新分区布局「{layout['目标分区']}」生效，统计报告按同一迁移批次重算"

    # ----------------------------------------------------------------- 回滚
    def _rollback(self, entry: dict[str, Any]) -> tuple[bool, str]:
        layout = layout_rules.latest_layout(entry)
        batch = layout_rules.plan_batches(entry)[-1] if layout_rules.plan_batches(entry) else None
        if layout is None or batch is None:
            return False, "尚未执行演进、没有可回滚的迁移批次"
        layout["演进状态"] = layout_rules.STATUS_ROLLED_BACK
        layout["统计口径"] = layout_rules.MODE_KEEP_OLD
        batch["批次状态"] = layout_rules.STATUS_ROLLED_BACK
        self._advance(entry, layout_rules.STATUS_ROLLED_BACK, negative=True)
        return True, f"演进计划已回滚，迁移批次 {batch['迁移批次']} 的新分区布局作废，统计报告保留旧分区布局"

    def _advance(self, entry: dict[str, Any], status: str, *, negative: bool = False) -> None:
        entry["status"] = status
        entry["计划状态"] = status
        entry["pending"] = status not in (layout_rules.STATUS_DONE, layout_rules.STATUS_ROLLED_BACK)
        entry["abnormal"] = negative

    # ------------------------------------------------------------ 统计侧协同
    def _sync_stats(self, entry: dict[str, Any]) -> None:
        # 延迟导入避免模块加载循环；统计侧只读同一份布局、只写自己的核对清单
        from app.services.stats import StatsService

        StatsService().sync_partition_layouts(str(entry.get("表名称", "")))

    # ------------------------------------------------------------- 布局核对
    def layout_checklist(self) -> list[dict[str, Any]]:
        """分区侧视角的布局核对清单：围绕新分区布局核对旧批次结果。"""
        checklist: list[dict[str, Any]] = []
        for plan in store.rows(MODULE):
            for batch in layout_rules.plan_batches(plan):
                layout = next(
                    (item for item in layout_rules.plan_layouts(plan) if item.get("迁移批次") == batch.get("迁移批次")),
                    None,
                )
                consistent = (
                    layout is not None
                    and batch.get("目标分区") == layout.get("目标分区")
                    and batch.get("影响行数") == layout.get("影响行数")
                )
                checklist.append({
                    "计划编号": plan.get("计划编号", ""),
                    "表名称": plan.get("表名称", ""),
                    "迁移批次": batch.get("迁移批次", ""),
                    "目标分区": batch.get("目标分区", ""),
                    "影响行数": batch.get("影响行数", ""),
                    "布局版本": batch.get("布局版本", ""),
                    "演进状态": plan.get("status", ""),
                    "核对结果": layout_rules.CHECK_MATCH if consistent else layout_rules.CHECK_DIFF,
                })
        return checklist
