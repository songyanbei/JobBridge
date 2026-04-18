<template>
  <div class="jb-page">
    <div class="jb-page-header">
      <div class="jb-page-title">中介管理</div>
      <div>
        <el-button type="primary" @click="preRegVisible = true">预注册中介</el-button>
        <el-button @click="importVisible = true">Excel 批量导入</el-button>
      </div>
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
          placeholder="搜索机构 / 联系人 / 手机号"
          clearable
          style="width: 260px"
          @keyup.enter="applyFilters"
        />
        <el-select
          v-model="filters.status"
          placeholder="状态"
          clearable
          style="width: 140px"
          @change="applyFilters"
        >
          <el-option label="正常" value="active" />
          <el-option label="已禁用" value="blocked" />
        </el-select>
        <el-button type="primary" @click="applyFilters">查询</el-button>
        <el-button @click="resetFilters">重置</el-button>
      </template>

      <el-table-column prop="userid" label="UserID" width="160" />
      <el-table-column prop="display_name" label="名称" />
      <el-table-column prop="company" label="机构" />
      <el-table-column prop="contact_person" label="联系人" />
      <el-table-column prop="phone" label="电话" width="120" />
      <el-table-column label="能力" width="160">
        <template #default="{ row }">
          <el-tag v-if="row.can_search_jobs" size="small" type="success" style="margin-right: 4px">
            岗位
          </el-tag>
          <el-tag v-if="row.can_search_workers" size="small" type="success">工人</el-tag>
          <span v-if="!row.can_search_jobs && !row.can_search_workers" class="jb-muted">--</span>
        </template>
      </el-table-column>
      <el-table-column prop="status" label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.status === 'blocked' ? 'danger' : 'success'" size="small">
            {{ row.status === 'blocked' ? '已禁用' : '正常' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="180" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openDetail(row)">详情</el-button>
          <el-button
            v-if="row.status !== 'blocked'"
            link
            type="danger"
            size="small"
            @click="openBlock(row)"
          >
            封禁
          </el-button>
          <el-button
            v-else
            link
            type="warning"
            size="small"
            @click="openUnblock(row)"
          >
            解封
          </el-button>
        </template>
      </el-table-column>
    </PageTable>

    <PreRegisterDialog
      v-model="preRegVisible"
      role="broker"
      :submitting="preRegSubmitting"
      @submit="onCreate"
    />

    <UploadImportDialog
      v-model="importVisible"
      :uploader="importBrokers"
      @success="() => load()"
    />

    <AccountDetailDrawer
      v-model="detailVisible"
      :userid="currentUserid"
      role="broker"
      @updated="() => load()"
    />

    <ConfirmAction
      v-model="blockVisible"
      title="封禁账号"
      :require-reason="true"
      :submitting="blockSubmitting"
      :message="`确认封禁账号 ${currentUserid} ？`"
      @confirm="onBlock"
    />
    <ConfirmAction
      v-model="unblockVisible"
      title="解封账号"
      :require-reason="true"
      :submitting="blockSubmitting"
      :message="`确认解封账号 ${currentUserid} ？`"
      @confirm="onUnblock"
    />
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import PageTable from '@/components/PageTable.vue'
import UploadImportDialog from '@/components/UploadImportDialog.vue'
import ConfirmAction from '@/components/ConfirmAction.vue'
import PreRegisterDialog from './components/PreRegisterDialog.vue'
import AccountDetailDrawer from './components/AccountDetailDrawer.vue'
import {
  fetchBrokers,
  createBroker,
  importBrokers,
  blockUser,
  unblockUser,
} from '@/api/accounts'
import { usePageTable } from '@/composables/usePageTable'

const { state, filters, load, setPage, setSize, applyFilters, resetFilters } = usePageTable({
  fetcher: fetchBrokers,
  initialFilters: { q: '', status: '' },
})

const preRegVisible = ref(false)
const preRegSubmitting = ref(false)
const importVisible = ref(false)

const detailVisible = ref(false)
const currentUserid = ref('')

const blockVisible = ref(false)
const unblockVisible = ref(false)
const blockSubmitting = ref(false)

load()

async function onCreate(payload) {
  preRegSubmitting.value = true
  try {
    await createBroker(payload)
    ElMessage.success('中介预注册成功')
    preRegVisible.value = false
    load()
  } finally {
    preRegSubmitting.value = false
  }
}

function openDetail(row) {
  currentUserid.value = row.userid
  detailVisible.value = true
}
function openBlock(row) {
  currentUserid.value = row.userid
  blockVisible.value = true
}
function openUnblock(row) {
  currentUserid.value = row.userid
  unblockVisible.value = true
}

async function onBlock({ reason }) {
  blockSubmitting.value = true
  try {
    await blockUser(currentUserid.value, { reason })
    ElMessage.success('已封禁')
    blockVisible.value = false
    load()
  } finally {
    blockSubmitting.value = false
  }
}

async function onUnblock({ reason }) {
  blockSubmitting.value = true
  try {
    await unblockUser(currentUserid.value, { reason })
    ElMessage.success('已解封')
    unblockVisible.value = false
    load()
  } finally {
    blockSubmitting.value = false
  }
}
</script>
