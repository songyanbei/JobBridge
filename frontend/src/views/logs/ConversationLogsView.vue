<template>
  <div class="conv-log-view">
    <h2>对话日志查询</h2>
    <p class="hint">查询某用户在指定时间范围内的对话流水（mock 测试用 userid 形如 <code>wm_mock_worker_001</code>）</p>

    <el-form :model="form" inline class="form" @submit.prevent>
      <el-form-item label="userid" required>
        <el-input v-model="form.userid" placeholder="必填，如 wm_mock_worker_001" style="width: 260px" clearable />
      </el-form-item>
      <el-form-item label="时间范围" required>
        <el-date-picker
          v-model="form.range"
          type="datetimerange"
          range-separator="~"
          start-placeholder="开始时间"
          end-placeholder="结束时间"
          format="YYYY-MM-DD HH:mm:ss"
          value-format="YYYY-MM-DDTHH:mm:ss"
          :default-time="defaultTime"
        />
      </el-form-item>
      <el-form-item label="方向">
        <el-select v-model="form.direction" placeholder="不限" style="width: 110px" clearable>
          <el-option label="入站" value="in" />
          <el-option label="出站" value="out" />
        </el-select>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="loading" @click="search">查询</el-button>
        <el-button @click="reset">重置</el-button>
      </el-form-item>
    </el-form>

    <el-alert v-if="errorMsg" :title="errorMsg" type="error" show-icon closable @close="errorMsg=''" />

    <el-table :data="rows" v-loading="loading" border stripe class="table">
      <el-table-column label="时间" prop="created_at" width="180" />
      <el-table-column label="方向" prop="direction" width="80">
        <template #default="{ row }">
          <el-tag :type="row.direction === 'in' ? 'info' : 'success'" size="small">
            {{ row.direction === 'in' ? '入' : '出' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="意图" prop="intent" width="140">
        <template #default="{ row }">
          <span v-if="row.intent">{{ row.intent }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="消息内容" prop="content" min-width="320">
        <template #default="{ row }">
          <div class="content-cell">{{ row.content }}</div>
        </template>
      </el-table-column>
      <el-table-column label="criteria" prop="criteria_snapshot" width="280">
        <template #default="{ row }">
          <code v-if="row.criteria_snapshot" class="json">{{ formatCriteria(row.criteria_snapshot) }}</code>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-if="total > 0"
      :total="total"
      :page-size="form.size"
      :current-page="form.page"
      :page-sizes="[20, 50, 100]"
      layout="total, sizes, prev, pager, next"
      @current-change="onPageChange"
      @size-change="onSizeChange"
      class="pager"
    />
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue'
import request from '@/api/request'
import { ElMessage } from 'element-plus'

const loading = ref(false)
const errorMsg = ref('')
const rows = ref([])
const total = ref(0)

const defaultTime = [
  new Date(2000, 0, 1, 0, 0, 0),
  new Date(2000, 0, 1, 23, 59, 59),
]

const form = reactive({
  userid: '',
  range: [],
  direction: '',
  page: 1,
  size: 50,
})

async function search() {
  if (!form.userid) {
    ElMessage.warning('请填写 userid')
    return
  }
  if (!form.range || form.range.length !== 2) {
    ElMessage.warning('请选择时间范围')
    return
  }
  loading.value = true
  errorMsg.value = ''
  try {
    const params = {
      userid: form.userid,
      start: form.range[0],
      end: form.range[1],
      page: form.page,
      size: form.size,
    }
    if (form.direction) params.direction = form.direction
    const data = await request.get('/admin/logs/conversations', { params })
    // request.js 的拦截器：成功时返回 data 字段
    rows.value = data.items || []
    total.value = data.total || 0
  } catch (err) {
    errorMsg.value = err?.response?.data?.message || err.message || '查询失败'
  } finally {
    loading.value = false
  }
}

function reset() {
  form.userid = ''
  form.range = []
  form.direction = ''
  form.page = 1
  rows.value = []
  total.value = 0
  errorMsg.value = ''
}

function onPageChange(p) {
  form.page = p
  search()
}

function onSizeChange(s) {
  form.size = s
  form.page = 1
  search()
}

function formatCriteria(c) {
  try {
    const obj = typeof c === 'string' ? JSON.parse(c) : c
    return JSON.stringify(obj, null, 2)
  } catch {
    return String(c)
  }
}
</script>

<style scoped>
.conv-log-view {
  padding: 20px;
}

.hint {
  color: #909399;
  font-size: 13px;
  margin: 4px 0 16px;
}

.hint code {
  padding: 1px 6px;
  background: #f5f7fa;
  border-radius: 3px;
  font-family: "SFMono-Regular", Menlo, Consolas, monospace;
  font-size: 12px;
  color: #e6a23c;
}

.form {
  margin-bottom: 16px;
}

.table {
  width: 100%;
  margin-top: 12px;
}

.content-cell {
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.5;
}

.json {
  display: block;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 11px;
  background: #f5f7fa;
  padding: 4px 6px;
  border-radius: 3px;
  max-height: 120px;
  overflow: auto;
}

.muted {
  color: #c0c4cc;
}

.pager {
  margin-top: 16px;
  text-align: right;
}
</style>
