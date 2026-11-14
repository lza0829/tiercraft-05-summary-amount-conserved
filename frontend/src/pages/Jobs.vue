<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const domains = ref([])
const domainId = ref(1)
const jobs = ref([])
const state = ref(null)
const err = ref('')
const notice = ref('')
const busy = ref(false)

async function refresh() {
  jobs.value = await getJson(`/api/jobs?domain_id=${domainId.value}`)
  state.value = await getJson(`/api/layers/summary/state?domain_id=${domainId.value}`)
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
  notice.value = ''
  err.value = ''
  try {
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function run(step) {
  busy.value = true
  err.value = ''
  notice.value = ''
  try {
    const url = step === '明细'
      ? '/api/jobs/paste-to-detail'
      : '/api/jobs/detail-to-summary'
    const job = await postJson(url, { domain_id: domainId.value })
    if (job.step === 'detail_to_summary') {
      if (job.status === '成功') {
        notice.value = `守恒校验通过：明细合计 ${fmt(job.amount_detail_total)}，汇总合计 ${fmt(job.amount_summary_total)}，漂移 ${fmt(job.amount_drift)}`
      } else {
        notice.value = `守恒校验失败，任务 #${job.id} 已记为失败：${job.message}`
      }
    }
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

function fmt(n) {
  if (n === null || n === undefined) return '—'
  return Number(n).toFixed(6).replace(/\.?0+$/, '')
}

function isConserved(drift, tolerance) {
  return Math.abs(Number(drift) || 0) <= (Number(tolerance) || 0.000001)
}

function isSummaryJob(j) {
  return j.step === 'detail_to_summary'
}

const lastSummaryJob = computed(() => {
  if (!state.value || !state.value.最近任务) return null
  const j = state.value.最近任务
  return isSummaryJob(j) ? j : null
})
</script>
<template>
  <div>
    <h2>分层任务</h2>
    <p class="lead">按主题域推进：先贴源到明细，再明细到汇总。明细到汇总写入后会校验金额守恒，不一致则整次任务失败并恢复汇总层快照。</p>
    <div class="toolbar">
      <label>
        主题域
        <select v-model.number="domainId" @change="onDomainChange">
          <option v-for="d in domains" :key="d.id" :value="d.id">{{ d.name }}</option>
        </select>
      </label>
      <button :disabled="busy" @click="run('明细')">跑贴源到明细</button>
      <button class="secondary" :disabled="busy" @click="run('汇总')">跑明细到汇总</button>
    </div>
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="notice" :class="lastSummaryJob && lastSummaryJob.status === '失败' ? 'err' : 'ok-note'">{{ notice }}</p>

    <div class="panel" v-if="state">
      <h3>汇总层守恒状态</h3>
      <table>
        <tbody>
          <tr>
            <th>当前守恒</th>
            <td :class="state.当前守恒 ? 'ok' : 'warn'">{{ state.当前守恒 ? '守恒成立' : '不守恒' }}</td>
            <th>当前漂移</th>
            <td :class="isConserved(state.当前漂移, state.容差) ? 'ok' : 'warn'">{{ fmt(state.当前漂移) }}</td>
          </tr>
          <tr>
            <th>明细金额合计</th><td>{{ fmt(state.明细层.金额合计) }}（{{ state.明细层.行数 }} 行）</td>
            <th>汇总金额合计</th><td>{{ fmt(state.汇总层.金额合计) }}（{{ state.汇总层.行数 }} 行）</td>
          </tr>
          <tr>
            <th>守恒容差</th><td>{{ state.容差 }}（{{ state.容差说明 }}）</td>
            <th>汇总行数合计</th><td>{{ state.汇总层.明细行数合计 }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="lastSummaryJob" class="conservation-note">
        <p>
          最近一次明细到汇总任务：
          <span :class="lastSummaryJob.status === '成功' ? 'ok' : 'warn'">#{{ lastSummaryJob.id }} {{ lastSummaryJob.status }}</span>
          <span v-if="lastSummaryJob.restored" class="warn"> · 汇总层已恢复快照</span>
        </p>
        <p class="muted">{{ lastSummaryJob.message }}</p>
        <ul v-if="lastSummaryJob.conservation && lastSummaryJob.status === '失败'">
          <li v-if="lastSummaryJob.conservation.失败原因" class="err-inline">守恒失败原因：{{ lastSummaryJob.conservation.失败原因 }}</li>
          <li v-if="lastSummaryJob.conservation.恢复">
            恢复后汇总态：
            行数 {{ lastSummaryJob.conservation.任务结束时汇总行数 ?? '—' }}，
            金额合计 {{ fmt(lastSummaryJob.conservation.任务结束时汇总合计) }}，
            相对明细漂移 {{ fmt(lastSummaryJob.conservation.任务结束时漂移) }}，
            恢复{{ lastSummaryJob.conservation.恢复.恢复成功 ? '成功' : '失败' }}
          </li>
        </ul>
      </div>
      <p v-else class="muted">该主题域还没有明细到汇总任务。</p>
    </div>

    <div class="panel">
      <h3>任务记录</h3>
      <table>
        <thead>
          <tr>
            <th>编号</th><th>步骤</th><th>状态</th><th>说明</th>
            <th>明细合计</th><th>汇总合计</th><th>漂移</th>
            <th>输入</th><th>写入</th><th>剔除</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="j in jobs" :key="j.id">
            <td>{{ j.id }}</td>
            <td>{{ j.step_label || j.step }}</td>
            <td :class="j.status === '成功' ? 'ok' : 'warn'">
              {{ j.status }}<span v-if="j.restored"> · 已恢复快照</span>
            </td>
            <td class="msg">{{ j.message }}</td>
            <td>{{ j.step === 'detail_to_summary' ? fmt(j.amount_detail_total) : '—' }}</td>
            <td>{{ j.step === 'detail_to_summary' ? fmt(j.amount_summary_total) : '—' }}</td>
            <td>{{ j.step === 'detail_to_summary' ? fmt(j.amount_drift) : '—' }}</td>
            <td>{{ j.in_count }}</td>
            <td>{{ j.out_count }}</td>
            <td>{{ j.drop_count }}</td>
            <td>{{ j.created_at }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!jobs.length" class="muted">还没有任务记录。</p>
    </div>
  </div>
</template>
