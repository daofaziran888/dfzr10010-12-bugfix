<template>
  <section class="page" data-module="partition">
    <header class="page-head">
      <div>
        <h2>分区演进管理管理</h2>
        <p class="page-desc">维护分区演进的当前状态、目标规划与影响评估，为分区演进管理提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记演进计划</button>
        <button class="btn" type="button" @click="exportRows">导出分区演进管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <!-- 执行演进表单：目标分区与影响行数随迁移批次一起提交，缺失会被服务层拒绝 -->
    <form v-if="formRow" class="inline-form" @submit.prevent="submitEvolution">
      <header class="inline-form-head">
        <strong>执行演进：{{ formRow['计划编号'] }}（{{ formRow['表名称'] }}）</strong>
        <button class="link" type="button" @click="closeForm">取消</button>
      </header>
      <div class="inline-form-body">
        <label class="filter-item">
          <span>目标分区 *</span>
          <input v-model="formValues['目标分区']" placeholder="例如 dt/hour" />
        </label>
        <label class="filter-item">
          <span>影响行数 *</span>
          <input v-model="formValues['影响行数']" placeholder="例如 1842万 或 18420000" />
        </label>
        <label class="filter-item">
          <span>迁移批次（留空自动生成）</span>
          <input v-model="formValues['迁移批次']" placeholder="PART-YYYYMMDD-NN" />
        </label>
        <button class="btn primary" type="submit">生成新分区布局并执行</button>
      </div>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in availableActions(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <span v-if="!availableActions(row).length" class="muted-text">终态，无可用动作</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无分区演进管理数据，可先登记演进计划</td>
        </tr>
      </tbody>
    </table>

    <section class="checklist-block">
      <header class="checklist-head">
        <h3>新分区布局核对清单</h3>
        <button class="btn" type="button" @click="loadChecklist">刷新核对清单</button>
      </header>
      <p class="page-desc">围绕新分区布局核对迁移批次：目标分区、影响行数随批次落库，刷新或切换页面后批次结果保持完整。</p>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in checklistColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(item, index) in checklist" :key="`${item['迁移批次']}-${index}`">
            <td v-for="column in checklistColumns" :key="column">{{ item[column] ?? '—' }}</td>
          </tr>
          <tr v-if="!checklist.length">
            <td :colspan="checklistColumns.length" class="empty-state">暂无已落库的迁移批次，执行演进后这里会展示新分区布局核对结果</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条分区演进管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="okMessage" class="ok-text">{{ okMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/partition'
const columns = ["计划编号", "表名称", "当前分区", "目标分区", "迁移批次", "影响行数", "布局版本", "计划状态"]
const checklistColumns = ["计划编号", "表名称", "迁移批次", "目标分区", "影响行数", "布局版本", "演进状态", "核对结果"]

const rows = ref<Row[]>([])
const total = ref(0)
const checklist = ref<Row[]>([])
const errorMessage = ref('')
const okMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const formRow = ref<Row | null>(null)
const formValues = ref<Record<string, string>>({ 目标分区: '', 影响行数: '', 迁移批次: '' })

const stats = computed(() => [
  { label: '演进计划总数', value: rows.value.length },
  { label: '执行中计划', value: rows.value.filter((row) => row.status === '执行中').length },
  { label: '已完成计划', value: rows.value.filter((row) => row.status === '已完成').length },
])

function availableActions(row: Row): string[] {
  // 与后端状态机保持一致：草稿 → 执行中 → 已完成 / 已回滚，单向推进
  if (row.status === '草稿') return ['执行演进']
  if (row.status === '执行中') return ['归档计划', '回滚演进']
  return []
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '演进计划登记入口尚未接入审批流'
}

function closeForm() {
  formRow.value = null
}

async function submitEvolution() {
  if (!formRow.value) return
  await dispatchAction('执行演进', formRow.value, { ...formValues.value })
  closeForm()
}

async function runAction(action: string, row: Row) {
  if (action === '执行演进') {
    formRow.value = row
    formValues.value = {
      目标分区: String(row['目标分区'] ?? ''),
      影响行数: String(row['影响行数'] ?? ''),
      迁移批次: String(row['迁移批次'] ?? ''),
    }
    return
  }
  await dispatchAction(action, row, {})
}

async function dispatchAction(action: string, row: Row, extra: Record<string, string>) {
  errorMessage.value = ''
  okMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action, ...extra } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '分区演进管理动作未生效，请稍后重试')
    }
    okMessage.value = payload.message
    await Promise.all([reload(), loadChecklist()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分区演进管理操作失败'
  }
}

async function loadChecklist() {
  try {
    const response = await request(`${ENDPOINT}/layout-checklist`)
    if (!response.ok) {
      throw new Error('布局核对清单读取失败')
    }
    const payload = await response.json()
    checklist.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '布局核对清单读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('演进计划列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分区演进管理列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadChecklist()
})
</script>
