<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJson } from '../api'

const domains = ref([])
const domainId = ref(null)
const rows = ref([])
const state = ref(null)
const err = ref('')

async function refresh() {
  const q = domainId.value === null ? '' : `?domain_id=${domainId.value}`
  const qState = domainId.value === null ? '' : `?domain_id=${domainId.value}`
  rows.value = await getJson(`/api/layers/汇总${q}`)
  state.value = domainId.value === null ? null : await getJson(`/api/layers/summary/state${qState}`)
}

onMounted(async () => {
  try {
    domains.value = await getJson('/api/domains')
    if (domains.value.length) domainId.value = domains.value[0].id
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function onDomainChange() {
  err.value = ''
  try {
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

function isConserved(drift, tolerance) {
  return Math.abs(Number(drift) || 0) <= (Number(tolerance) || 0.000001)
}

function fmt(n) {
  if (n === null || n === undefined) return '—'
  return Number(n).toFixed(6).replace(/\.?0+$/, '')
}

const bad = computed(() => state.value && !state.value.当前守恒)
const last = computed(() => state.value && state.value.最近任务)
</script>
<template>
  <div>
    <h2>汇总层</h2>
    <p class="lead">按业务日与门店编码聚合后的汇总表。汇总写入完成后校验金额守恒；失败的任务会把本层恢复为任务开始前快照。</p>
    <div class="toolbar">
      <label>
        主题域
        <select v-model.number="domainId" @change="onDomainChange">
          <option v-for="d in domains" :key="d.id" :value="d.id">{{ d.name }}</option>
        </select>
      </label>
    </div>
    <p v-if="err" class="err">{{ err }}</p>

    <div v-if="state" class="panel">
      <h3>守恒校验</h3>
      <table>
        <tbody>
          <tr>
            <th>当前守恒</th>
            <td :class="state.当前守恒 ? 'ok' : 'warn'">{{ state.当前守恒 ? '守恒成立' : '不守恒' }}</td>
            <th>当前漂移</th>
            <td :class="isConserved(state.当前漂移, state.容差) ? 'ok' : 'warn'">{{ fmt(state.当前漂移) }}</td>
          </tr>
          <tr>
            <th>明细金额合计</th><td>{{ fmt(state.明细层.金额合计) }}</td>
            <th>汇总金额合计</th><td>{{ fmt(state.汇总层.金额合计) }}</td>
          </tr>
          <tr>
            <th>明细行数</th><td>{{ state.明细层.行数 }}</td>
            <th>汇总行数合计</th><td>{{ state.汇总层.明细行数合计 }}</td>
          </tr>
          <tr>
            <th colspan="4" class="muted">容差：绝对差 ≤ {{ state.容差 }} 视为守恒，用于吸收二进制浮点舍入误差。</th>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="last && last.status === '失败'" class="panel failure-panel">
      <h3>最近一次守恒失败 <span class="warn">· 任务 #{{ last.id }}</span></h3>
      <p v-if="last.conservation && last.conservation.失败原因" class="err">守恒失败原因：{{ last.conservation.失败原因 }}</p>
      <p class="muted">{{ last.message }}</p>
      <table v-if="last.conservation">
        <tbody>
          <tr>
            <th>明细合计</th><td>{{ fmt(last.conservation.明细合计) }}</td>
            <th>校验时汇总合计</th><td>{{ fmt(last.conservation.汇总合计) }}</td>
          </tr>
          <tr>
            <th>校验时漂移</th><td class="warn">{{ fmt(last.conservation.漂移) }}</td>
            <th>容差</th><td>{{ last.conservation.容差 }}</td>
          </tr>
          <tr v-if="last.conservation.恢复">
            <th>恢复是否成功</th>
            <td :class="last.conservation.恢复.恢复成功 ? 'ok' : 'warn'">{{ last.conservation.恢复.恢复成功 ? '成功' : '失败' }}</td>
            <th>恢复方式</th><td>{{ last.conservation.恢复.恢复方式 || '—' }}</td>
          </tr>
          <tr>
            <th>恢复后汇总态</th>
            <td colspan="3">
              {{ last.conservation.任务结束时汇总行数 ?? '—' }} 行 /
              金额合计 {{ fmt(last.conservation.任务结束时汇总合计) }} /
              相对明细漂移 {{ fmt(last.conservation.任务结束时漂移) }}
              <span :class="last.restored ? 'ok' : 'warn'">
                {{ last.restored ? '（已回到任务开始前快照，无半截或混杂键）' : '（未恢复成功，需人工按快照恢复）' }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-else-if="last" class="panel">
      <h3>最近一次守恒校验 <span class="ok">· 任务 #{{ last.id }} {{ last.status }}</span></h3>
      <p class="muted">{{ last.message }}</p>
    </div>

    <div class="panel">
      <h3>汇总明细</h3>
      <table>
        <thead>
          <tr>
            <th>编号</th><th>主题域</th><th>业务日</th><th>门店编码</th>
            <th>金额合计</th><th>件数合计</th><th>明细行数</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.domain_id }}</td>
            <td>{{ r.biz_day }}</td>
            <td>{{ r.store_code }}</td>
            <td>{{ r.amount_sum }}</td>
            <td>{{ r.qty_sum }}</td>
            <td>{{ r.line_count }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!rows.length" class="muted">汇总层为空，请先跑明细到汇总。</p>
      <p v-if="bad" class="err">当前汇总层与明细层不守恒，请检查最近任务记录。</p>
    </div>
  </div>
</template>
