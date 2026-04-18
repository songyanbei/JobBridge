<template>
  <DetailDrawer
    v-model="visible"
    title="账号详情"
    :loading="loading"
    :dirty="dirty"
    size="520px"
  >
    <div v-if="detail">
      <el-descriptions :column="1" border>
        <el-descriptions-item label="UserID">{{ detail.userid }}</el-descriptions-item>
        <el-descriptions-item label="显示名称">{{ detail.display_name || '--' }}</el-descriptions-item>
        <el-descriptions-item label="公司">{{ detail.company || '--' }}</el-descriptions-item>
        <el-descriptions-item label="联系人">{{ detail.contact_person || '--' }}</el-descriptions-item>
        <el-descriptions-item label="电话">{{ detail.phone || '--' }}</el-descriptions-item>
        <el-descriptions-item label="角色">{{ detail.role || role }}</el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag :type="detail.status === 'blocked' ? 'danger' : 'success'" size="small">
            {{ detail.status === 'blocked' ? '已禁用' : '正常' }}
          </el-tag>
        </el-descriptions-item>
        <template v-if="role === 'broker'">
          <el-descriptions-item label="可检索岗位">
            <el-switch
              v-model="capJobs"
              @change="markDirty"
            />
          </el-descriptions-item>
          <el-descriptions-item label="可检索工人">
            <el-switch
              v-model="capWorkers"
              @change="markDirty"
            />
          </el-descriptions-item>
        </template>
        <el-descriptions-item label="创建时间">
          {{ formatDateTime(detail.created_at) }}
        </el-descriptions-item>
      </el-descriptions>
    </div>

    <template v-if="role !== 'worker' && detail" #footer>
      <el-button @click="visible = false">关闭</el-button>
      <el-button type="primary" :loading="saving" :disabled="!dirty" @click="save">
        保存修改
      </el-button>
    </template>
  </DetailDrawer>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import DetailDrawer from '@/components/DetailDrawer.vue'
import { fetchFactoryDetail, fetchBrokerDetail, updateFactory, updateBroker } from '@/api/accounts'
import { formatDateTime } from '@/utils/format'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  userid: { type: String, default: '' },
  role: { type: String, default: 'factory' },
})
const emit = defineEmits(['update:modelValue', 'updated'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const detail = ref(null)
const loading = ref(false)
const saving = ref(false)
const dirty = ref(false)
const capJobs = ref(false)
const capWorkers = ref(false)

watch(
  () => [props.modelValue, props.userid],
  async ([v, uid]) => {
    if (!v || !uid) return
    dirty.value = false
    loading.value = true
    detail.value = null
    try {
      const fetcher = props.role === 'broker' ? fetchBrokerDetail : fetchFactoryDetail
      const data = await fetcher(uid)
      detail.value = data
      capJobs.value = !!data.can_search_jobs
      capWorkers.value = !!data.can_search_workers
    } finally {
      loading.value = false
    }
  },
  { immediate: true },
)

function markDirty() {
  dirty.value = true
}

async function save() {
  if (!detail.value) return
  saving.value = true
  try {
    if (props.role === 'broker') {
      await updateBroker(detail.value.userid, {
        version: detail.value.version,
        fields: {
          can_search_jobs: capJobs.value,
          can_search_workers: capWorkers.value,
        },
      })
    } else {
      await updateFactory(detail.value.userid, {
        version: detail.value.version,
        fields: {},
      })
    }
    ElMessage.success('已保存')
    dirty.value = false
    visible.value = false
    emit('updated')
  } finally {
    saving.value = false
  }
}
</script>
