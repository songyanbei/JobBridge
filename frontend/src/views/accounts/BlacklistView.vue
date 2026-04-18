<template>
  <div class="jb-page">
    <div class="jb-page-header">
      <div class="jb-page-title">黑名单</div>
    </div>

    <PageTable
      :rows="state.rows"
      :loading="state.loading"
      :total="state.total"
      :page="state.page"
      :size="state.size"
      :exportable="false"
      @update:page="setPage"
      @update:size="setSize"
      @refresh="load()"
    >
      <template #filter>
        <el-input
          v-model="filters.q"
          placeholder="搜索 userid / 姓名"
          clearable
          style="width: 240px"
          @keyup.enter="applyFilters"
        />
        <el-select v-model="filters.role" placeholder="角色" clearable style="width: 120px">
          <el-option label="厂家" value="factory" />
          <el-option label="中介" value="broker" />
          <el-option label="工人" value="worker" />
        </el-select>
        <el-button type="primary" @click="applyFilters">查询</el-button>
        <el-button @click="resetFilters">重置</el-button>
      </template>

      <el-table-column prop="userid" label="UserID" width="160" />
      <el-table-column prop="display_name" label="名称" />
      <el-table-column prop="role" label="角色" width="100" />
      <el-table-column prop="block_reason" label="封禁原因" show-overflow-tooltip />
      <el-table-column prop="blocked_at" label="封禁时间" width="160">
        <template #default="{ row }">{{ formatDateTime(row.blocked_at) }}</template>
      </el-table-column>
      <el-table-column prop="blocked_by" label="操作人" width="120" />
      <el-table-column label="操作" width="100" fixed="right">
        <template #default="{ row }">
          <el-button link type="warning" size="small" @click="openUnblock(row)">解封</el-button>
        </template>
      </el-table-column>
    </PageTable>

    <ConfirmAction
      v-model="unblockVisible"
      title="解封账号"
      :require-reason="true"
      :submitting="submitting"
      :message="`确认解封账号 ${currentUserid} ？`"
      @confirm="onUnblock"
    />
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import PageTable from '@/components/PageTable.vue'
import ConfirmAction from '@/components/ConfirmAction.vue'
import { fetchBlacklist, unblockUser } from '@/api/accounts'
import { usePageTable } from '@/composables/usePageTable'
import { formatDateTime } from '@/utils/format'

const { state, filters, load, setPage, setSize, applyFilters, resetFilters } = usePageTable({
  fetcher: fetchBlacklist,
  initialFilters: { q: '', role: '' },
})

const unblockVisible = ref(false)
const currentUserid = ref('')
const submitting = ref(false)

load()

function openUnblock(row) {
  currentUserid.value = row.userid
  unblockVisible.value = true
}

async function onUnblock({ reason }) {
  submitting.value = true
  try {
    await unblockUser(currentUserid.value, { reason })
    ElMessage.success('已解封')
    unblockVisible.value = false
    load()
  } finally {
    submitting.value = false
  }
}
</script>
