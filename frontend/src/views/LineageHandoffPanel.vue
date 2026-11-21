<template>
  <section class="handoff-panel" data-testid="handoff-panel">
    <header class="handoff-head">
      <div>
        <h3>血缘交接包</h3>
        <p class="handoff-desc">
          指定库范围生成可回放材料。范围外的字段与转换语句不出包，只留关系边界并标记「证据欠缺」。
        </p>
      </div>
      <div class="handoff-actions">
        <button class="btn" data-testid="handoff-generate" type="button" :disabled="busy" @click="generate">
          生成交接包
        </button>
        <button class="btn primary" data-testid="handoff-submit-deletion" type="button" :disabled="busy" @click="submitDeletion">
          提交字段删除
        </button>
      </div>
    </header>

    <form class="handoff-form" @submit.prevent="generate">
      <label>
        <span>交接库范围（逗号分隔）</span>
        <input v-model="scopeText" data-testid="handoff-scope" placeholder="dw_dwd,dw_ods" />
      </label>
      <label>
        <span>待交接的事实表</span>
        <input v-model="table" data-testid="handoff-table" placeholder="dwd_trade_detail" />
      </label>
      <label>
        <span>待删字段（逗号分隔）</span>
        <input v-model="fieldsText" data-testid="handoff-fields" placeholder="order_id" />
      </label>
    </form>

    <p v-if="errorMessage" class="error-text" data-testid="handoff-error">{{ errorMessage }}</p>

    <template v-if="current">
      <div class="handoff-meta">
        <span data-testid="handoff-package-id">包编号：{{ current.包编号 }}</span>
        <span data-testid="handoff-resume">续做：{{ current.是否续做 ? '是（复用中断检查点）' : '否（首次导出）' }}</span>
        <span>交接库范围：{{ current.交接库范围.join('、') || '—' }}</span>
        <span>事实表：{{ current.事实表 }}（{{ current.事实表所属库 }}）</span>
      </div>
      <p class="handoff-stats" data-testid="handoff-stats">
        范围内关系 {{ current.统计.范围内关系数 }} 条 · 范围外边界 {{ current.统计.范围外边界数 }} 条
        <template v-if="current.中断遗留边界数"> · 中断遗留 {{ current.中断遗留边界数 }} 条</template>
        · 可确认影响 {{ current.统计.可确认影响数 }} 条 · 证据欠缺 {{ current.统计.证据欠缺数 }} 条
      </p>
      <p v-if="current.问题清单.length" class="error-text">
        交接包自检：{{ current.问题清单.join('；') }}
      </p>

      <div class="handoff-grid">
        <article class="handoff-card" data-testid="handoff-confirmed">
          <h4>已确认关系</h4>
          <p v-if="!current.关系中转.length" class="handoff-empty">没有落在交接范围内的完整关系</p>
          <ul>
            <li v-for="item in current.关系中转" :key="item.关系编号">
              <code>{{ item.关系编号 }}</code>
              {{ item.上游表名 }} → {{ item.下游表名 }}
              <small>{{ item.来源字段 }} → {{ item.目标字段 }}</small>
            </li>
          </ul>
        </article>

        <article class="handoff-card boundary" data-testid="handoff-boundary">
          <h4>仍需跨库确认的边界</h4>
          <p v-if="!current.关系边界.length" class="handoff-empty">没有超出范围的来源关系</p>
          <ul>
            <li v-for="item in current.关系边界" :key="item.关系编号">
              <code>{{ item.关系编号 }}</code>
              {{ item.上游表名 }}（{{ item.上游库名 }}） → {{ item.下游表名 }}（{{ item.下游库名 }}）
              <small data-testid="handoff-boundary-evidence">{{ item.证据状态 || '证据欠缺' }}：{{ item.边界说明 }}</small>
            </li>
          </ul>
        </article>
      </div>

      <div class="handoff-downloads">
        <button class="btn" data-testid="handoff-download-json" type="button" @click="download('json')">
          下载交接包 JSON
        </button>
        <button class="btn" data-testid="handoff-download-csv" type="button" @click="download('csv')">
          下载交接包 CSV
        </button>
      </div>

      <div class="handoff-grid">
        <article class="handoff-card" data-testid="handoff-impact-confirmed">
          <h4>审批者可以处理的对象</h4>
          <p v-if="!current.审批清单.可处理对象.length" class="handoff-empty">暂无可直接审批的依赖</p>
          <ul>
            <li v-for="item in current.审批清单.可处理对象" :key="item.关系编号">
              <code>{{ item.关系编号 }}</code> {{ item.对端表名 }}
              <small>待改字段：{{ (item.待改字段 || []).join('、') || '—' }}</small>
            </li>
          </ul>
        </article>
        <article class="handoff-card boundary" data-testid="handoff-impact-missing">
          <h4>需要补采的边界</h4>
          <p v-if="!current.审批清单.需补采边界.length" class="handoff-empty">没有需要跨库补采的边界</p>
          <ul>
            <li v-for="item in current.审批清单.需补采边界" :key="item.关系编号">
              <code>{{ item.关系编号 }}</code> {{ item.对端表名 }}（{{ item.对端库名 }}）
              <small>{{ item.结论 }}：需跨库补采字段级血缘</small>
            </li>
          </ul>
        </article>
      </div>
    </template>

    <template v-if="deletion">
      <p class="handoff-stats" data-testid="handoff-deletion-result">
        字段删除 {{ deletion.变更编号 }} 已提交（交接包 {{ deletion.包编号 }}），
        同步复核清单 {{ deletion.同步复核清单条数 }} 条。
      </p>
      <div class="handoff-grid">
        <article class="handoff-card" data-testid="handoff-deletion-actionable">
          <h4>影响清单：审批者可以处理的对象</h4>
          <p v-if="!deletion.影响清单.length" class="handoff-empty">没有审批者可直接处理的依赖</p>
          <ul>
            <li v-for="item in deletion.影响清单" :key="item.关系编号">
              <code>{{ item.关系编号 }}</code> {{ item.对端表名 }}
              <small>待改字段：{{ (item.待改字段 || []).join('、') || '—' }} · {{ item.结论 }}</small>
            </li>
          </ul>
        </article>
        <article class="handoff-card boundary" data-testid="handoff-deletion-missing">
          <h4>影响清单：需要补采的边界</h4>
          <p v-if="!deletion.需补采的边界.length" class="handoff-empty">没有需要跨库补采的边界</p>
          <ul>
            <li v-for="item in deletion.需补采的边界" :key="item.关系编号">
              <code>{{ item.关系编号 }}</code> {{ item.对端表名 }}（{{ item.对端库名 }}）
              <small>{{ item.结论 }}：需跨库补采后再判定是否断链</small>
            </li>
          </ul>
        </article>
      </div>
      <p v-if="deletion.确认无依赖.length" class="handoff-stats">
        确认无依赖：{{ deletion.确认无依赖.map((item) => item.对端表名).join('、') }}（仅留证，不进入审批）
      </p>
      <p v-if="deletion.交接包问题.length" class="error-text">
        交接包自检：{{ deletion.交接包问题.join('；') }}
      </p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  createHandoffPackage,
  downloadHandoffPackage,
  loadLatestHandoffPackage,
  submitFieldDeletion,
} from '@/api/backend-service'
import type { FieldDeletionResult, HandoffPackage } from '@/data/types'

const current = ref<HandoffPackage | null>(null)
const deletion = ref<FieldDeletionResult | null>(null)
const errorMessage = ref('')
const busy = ref(false)

const table = ref('dwd_trade_detail')
const scopeText = ref('dw_dwd,dw_ods')
const fieldsText = ref('order_id')

function splitValues(text: string): string[] {
  return text
    .split(/[,，、\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

async function generate() {
  busy.value = true
  errorMessage.value = ''
  try {
    current.value = await createHandoffPackage(
      table.value.trim(),
      splitValues(scopeText.value),
      splitValues(fieldsText.value),
    )
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '血缘交接包生成失败'
  } finally {
    busy.value = false
  }
}

async function submitDeletion() {
  busy.value = true
  errorMessage.value = ''
  try {
    deletion.value = await submitFieldDeletion(
      table.value.trim(),
      splitValues(scopeText.value),
      splitValues(fieldsText.value),
    )
    current.value = await loadLatestHandoffPackage(table.value.trim())
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '字段删除影响核对失败'
  } finally {
    busy.value = false
  }
}

async function download(format: 'json' | 'csv') {
  if (!current.value) return
  errorMessage.value = ''
  try {
    await downloadHandoffPackage(current.value.包编号, format)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交接包下载失败'
  }
}

onMounted(async () => {
  try {
    const latest = await loadLatestHandoffPackage(table.value)
    if (latest) current.value = latest
  } catch {
    // 首次进入没有历史包是正常情况，静默即可。
  }
})
</script>

<style scoped>
.handoff-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
  margin: 12px 0;
}
.handoff-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.handoff-head h3 { margin: 0; font-size: 15px; }
.handoff-desc { color: var(--muted); font-size: 12px; margin: 4px 0 0; max-width: 720px; }
.handoff-actions { display: flex; gap: 8px; flex-shrink: 0; }
.handoff-form { display: flex; flex-wrap: wrap; gap: 10px; margin: 12px 0; }
.handoff-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.handoff-form input { border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; font-size: 13px; min-width: 180px; }
.handoff-meta { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: #334155; }
.handoff-stats { font-size: 12px; color: var(--muted); margin: 8px 0; }
.handoff-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 8px; }
.handoff-card { border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; background: #fafbfc; }
.handoff-card.boundary { background: #fff8ed; border-color: #f0c48a; }
.handoff-card h4 { margin: 0 0 6px; font-size: 13px; }
.handoff-card ul { list-style: none; padding: 0; margin: 0; font-size: 12px; }
.handoff-card li { padding: 4px 0; border-bottom: 1px dashed #e5e7eb; }
.handoff-card li:last-child { border-bottom: none; }
.handoff-card small { display: block; color: var(--muted); }
.handoff-empty { color: var(--muted); font-size: 12px; margin: 0; }
.handoff-downloads { display: flex; gap: 8px; margin-top: 12px; }
</style>
