<template>
  <section class="page" data-module="stats">
    <header class="page-head">
      <div>
        <h2>表统计信息管理</h2>
        <p class="page-desc">维护表统计的行数、文件数与容量分析，为表统计信息提供可审计的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记统计报告</button>
        <button class="btn" type="button" @click="exportRows">导出表统计信息清单</button>
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
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无表统计信息数据，可先登记统计报告</td>
        </tr>
      </tbody>
    </table>

    <section class="checklist-block">
      <header class="checklist-head">
        <h3>新分区布局核对清单</h3>
        <button class="btn primary" type="button" @click="syncLayout">比对当前新分区布局</button>
      </header>
      <p class="page-desc">
        统计模块消费分区演进同一份新分区布局，按同一迁移批次重算影响行数；
        执行中批次在统计补齐前保留旧布局，差异记入本清单，刷新后批次结果保持完整。
      </p>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in checklistColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(item, index) in checklist" :key="`${item['报告编号']}-${item['迁移批次']}-${index}`">
            <td v-for="column in checklistColumns" :key="column">{{ item[column] ?? '—' }}</td>
          </tr>
          <tr v-if="!checklist.length">
            <td :colspan="checklistColumns.length" class="empty-state">暂无布局核对结果，分区演进执行后点击「比对当前新分区布局」生成</td>
          </tr>
        </tbody>
      </table>
      <p v-for="(item, index) in checklistNotes" :key="index" class="check-note">
        {{ item['报告编号'] }} / {{ item['迁移批次'] }}：{{ item['核对说明'] }}
      </p>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条表统计信息记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="okMessage" class="ok-text">{{ okMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/stats'
const columns = ["报告编号", "表名称", "行总数", "文件总数", "总容量", "列统计", "统计时间", "报告状态"]
const checklistColumns = ["报告编号", "迁移批次", "当前分区", "新分区布局", "演进状态", "统计口径", "影响行数", "重算影响行数", "核对结果"]
const actions = ["采集统计", "标记过期", "清理报告"]

const rows = ref<Row[]>([])
const total = ref(0)
const checklist = ref<Row[]>([])
const errorMessage = ref('')
const okMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const stats = computed(() => [
  { label: '统计报告总数', value: rows.value.length },
  { label: '已采集报告', value: rows.value.filter((row) => row.status === '已采集').length },
  { label: '已过期报告', value: rows.value.filter((row) => row.status === '已过期').length },
])

const checklistNotes = computed(() => checklist.value.filter((item) => item['核对说明']))

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '统计报告登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  okMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '表统计信息动作未生效，请稍后重试')
    }
    okMessage.value = payload.message
    await Promise.all([reload(), loadChecklist()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '表统计信息操作失败'
  }
}

async function syncLayout() {
  errorMessage.value = ''
  okMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/layout-sync`, {
      method: 'POST',
      body: JSON.stringify({ values: {} }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '布局核对未生效')
    }
    okMessage.value = payload.message
    await loadChecklist()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '布局核对失败'
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
      throw new Error('统计报告列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '表统计信息列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadChecklist()
})
</script>
