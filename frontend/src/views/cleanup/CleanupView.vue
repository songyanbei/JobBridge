<template>
  <div class="jb-page">
    <div class="jb-page-header"><div class="jb-page-title">清理运维</div></div>
    <el-alert v-if="!canOperate" type="info" :closable="false" title="当前账号仅可查询，处置操作需要超级管理员" />
    <el-tabs v-model="tab" @tab-change="load">
      <el-tab-pane label="清理任务" name="tasks">
        <el-table :data="tasks" v-loading="loading">
          <el-table-column prop="id" label="ID" width="90" />
          <el-table-column prop="target_type" label="类型" />
          <el-table-column prop="target_id" label="目标" />
          <el-table-column prop="status" label="状态" />
          <el-table-column prop="attempt_count" label="尝试" />
          <el-table-column label="操作" width="100">
            <template #default="{ row }">
              <CleanupActionButton :role="role" :status="row.status" required-status="dead_letter" @click="retry('target', row.id)">重驱</CleanupActionButton>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
      <el-tab-pane label="媒体死信" name="media-dead-letters">
        <el-table :data="mediaDeadLetters" v-loading="loading">
          <el-table-column prop="id" label="Asset ID" width="110" />
          <el-table-column prop="status" label="状态" />
          <el-table-column prop="attempt_count" label="尝试" />
          <el-table-column label="操作" width="100">
            <template #default="{ row }">
              <CleanupActionButton :role="role" :status="row.status" required-status="dead_letter" @click="retry('media', row.id)">重驱</CleanupActionButton>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
      <el-tab-pane label="媒体隔离" name="issues">
        <el-table :data="issues" v-loading="loading">
          <el-table-column prop="id" label="ID" width="90" />
          <el-table-column prop="resume_id" label="简历" />
          <el-table-column prop="issue_type" label="问题" />
          <el-table-column prop="status" label="状态" />
          <el-table-column prop="disposition" label="处置" />
          <el-table-column label="操作" width="180">
            <template #default="{ row }">
              <CleanupActionButton :role="role" :status="row.status" required-status="open" @click="approve(row.id)">审批</CleanupActionButton>
              <CleanupActionButton :role="role" :status="row.status" required-status="approved" :approved-by="row.approved_by" :username="username" four-eyes @click="execute(row.id)">执行</CleanupActionButton>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import CleanupActionButton from './CleanupActionButton.vue'
import { approveMediaIssue, executeMediaIssue, fetchCleanupTasks, fetchMediaDeadLetters, fetchMediaIssues, retryDeadLetters } from '@/api/cleanup'

const auth = useAuthStore()
const tab = ref('tasks')
const tasks = ref([])
const issues = ref([])
const mediaDeadLetters = ref([])
const loading = ref(false)
const canOperate = computed(() => auth.admin?.role === 'super_admin')
const role = computed(() => auth.admin?.role || '')
const username = computed(() => auth.admin?.username)

async function load() {
  loading.value = true
  try {
    if (tab.value === 'tasks') tasks.value = await fetchCleanupTasks({ limit: 100 })
    else if (tab.value === 'media-dead-letters') mediaDeadLetters.value = await fetchMediaDeadLetters({ limit: 100 })
    else issues.value = await fetchMediaIssues({ limit: 100 })
  } finally { loading.value = false }
}

async function retry(kind, id) {
  const { value: reason } = await ElMessageBox.prompt('请输入重驱理由', '重驱 dead letter', { inputValidator: (v) => !!v?.trim() || '理由必填' })
  await retryDeadLetters({ kind, ids: [id], reason: reason.trim() })
  ElMessage.success('已提交重驱')
  await load()
}

async function approve(id) {
  const { value: reason } = await ElMessageBox.prompt('请输入审批理由', '审批媒体处置', { inputValidator: (v) => !!v?.trim() || '理由必填' })
  await approveMediaIssue(id, { disposition: 'detach_reference', reason: reason.trim() })
  ElMessage.success('已审批，须由另一管理员执行')
  await load()
}

async function execute(id) {
  await ElMessageBox.confirm('确认执行已审批的媒体处置？', '二次确认', { type: 'warning' })
  await executeMediaIssue(id)
  ElMessage.success('处置已执行')
  await load()
}

load()
</script>
