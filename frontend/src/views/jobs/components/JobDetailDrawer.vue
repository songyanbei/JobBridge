<template>
  <DetailDrawer
    v-model="visible"
    title="岗位详情"
    :loading="loading"
    :dirty="dirty"
    size="640px"
  >
    <div v-if="detail">
      <el-alert
        v-if="detail.audit_status === 'rejected'"
        :title="`该岗位已驳回：${detail.reject_reason || ''}`"
        type="error"
        :closable="false"
        style="margin-bottom: 10px"
      />
      <el-alert
        v-if="!detail.activated_at"
        :title="detail.replacement_lifecycle_status === 'conflict' ? '候选激活冲突，旧岗位保持原状态' : '该记录是未激活候选，不属于在线岗位'"
        type="warning"
        :closable="false"
        style="margin-bottom: 10px"
      />
      <el-descriptions :column="2" border>
        <el-descriptions-item label="ID">{{ detail.id }}</el-descriptions-item>
        <el-descriptions-item label="版本">v{{ detail.version }}</el-descriptions-item>
        <el-descriptions-item label="城市">
          <el-input v-if="editing" v-model="form.city" @input="markDirty" />
          <span v-else>{{ detail.city }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="区县">
          <el-input v-if="editing" v-model="form.district" @input="markDirty" />
          <span v-else>{{ detail.district || '--' }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="详细地址" :span="2">
          <el-input v-if="editing" v-model="form.address" @input="markDirty" placeholder="街道+门牌（区县另填）" />
          <span v-else>{{ detail.address || '--' }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="工种">
          <el-input v-if="editing" v-model="form.job_category" @input="markDirty" />
          <span v-else>{{ detail.job_category }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="支付方式">
          <el-select v-if="editing" v-model="form.pay_type" @change="markDirty">
            <el-option label="月薪" value="月薪" />
            <el-option label="时薪" value="时薪" />
            <el-option label="计件" value="计件" />
          </el-select>
          <span v-else>{{ detail.pay_type || '--' }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="薪资下限">
          <el-input-number
            v-if="editing"
            v-model="form.salary_floor_monthly"
            :min="0"
            @change="markDirty"
          />
          <span v-else>{{ detail.salary_floor_monthly }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="薪资上限">
          <el-input-number
            v-if="editing"
            v-model="form.salary_ceiling_monthly"
            :min="0"
            @change="markDirty"
          />
          <span v-else>{{ detail.salary_ceiling_monthly }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="审核状态">
          <el-tag
            :type="{ passed: 'success', rejected: 'danger', pending: 'warning' }[detail.audit_status] || 'info'"
            size="small"
          >
            {{ { passed: '已通过', rejected: '已驳回', pending: '待审' }[detail.audit_status] || '--' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="下架原因">
          {{ detail.delist_reason || '--' }}
        </el-descriptions-item>
        <el-descriptions-item label="生命周期状态">
          {{ lifecycleLabel(detail) }}
        </el-descriptions-item>
        <el-descriptions-item label="激活时间">
          {{ formatDateTime(detail.activated_at) }}
        </el-descriptions-item>
        <el-descriptions-item label="到期时间">
          <span :class="`jb-${ttlLevel(detail.expires_at)}-text`">
            {{ formatDateTime(detail.expires_at) }}
          </span>
        </el-descriptions-item>
        <el-descriptions-item label="候选到期时间">
          {{ formatDateTime(detail.candidate_expires_at) }}
        </el-descriptions-item>
        <el-descriptions-item v-if="detail.replaces_job_id" label="替换旧岗位">
          #{{ detail.replaces_job_id }}
        </el-descriptions-item>
        <el-descriptions-item v-if="detail.replaced_by_job_id" label="关联新版本">
          #{{ detail.replaced_by_job_id }}
        </el-descriptions-item>
        <el-descriptions-item v-if="detail.replacement_id" label="替换关系">
          #{{ detail.replacement_id }} · {{ detail.replacement_review_outcome || '--' }} / {{ detail.replacement_lifecycle_status || '--' }}
        </el-descriptions-item>
        <el-descriptions-item label="发布人 ID">{{ detail.owner_userid }}</el-descriptions-item>

        <!-- ---- 发布方信息 ---- -->
        <el-descriptions-item label="发布方角色">
          {{ { factory: '厂家', broker: '中介', worker: '工人' }[detail.owner_role] || '--' }}
        </el-descriptions-item>
        <el-descriptions-item label="联系人">{{ detail.owner_contact_person || detail.owner_display_name || '--' }}</el-descriptions-item>
        <el-descriptions-item label="联系电话">{{ detail.owner_phone || '--' }}</el-descriptions-item>
        <el-descriptions-item label="所属公司" :span="2">{{ detail.owner_company || '--' }}</el-descriptions-item>
        <el-descriptions-item label="公司地址" :span="2">{{ detail.owner_address || '--' }}</el-descriptions-item>
      </el-descriptions>

      <ImagePreview
        v-if="detail.images && detail.images.length"
        :images="detail.images"
        style="margin-top: 12px"
      />
    </div>

    <template #footer>
      <template v-if="editing">
        <el-button @click="cancelEdit">取消</el-button>
        <el-button type="primary" :loading="saving" :disabled="!dirty" @click="save">保存</el-button>
      </template>
      <template v-else-if="detail">
        <el-button @click="visible = false">关闭</el-button>
        <el-button :disabled="!canManage" @click="startEdit">编辑</el-button>
        <el-button
          v-if="detail.replacement_lifecycle_status === 'conflict'"
          type="primary"
          @click="retryActivation"
        >
          重试激活
        </el-button>
        <el-button
          v-if="['awaiting_review', 'conflict'].includes(detail.replacement_lifecycle_status)"
          type="danger"
          plain
          @click="cancelCandidate"
        >
          取消候选
        </el-button>
        <el-dropdown @command="onCommand">
          <el-button>
            更多操作
            <el-icon style="margin-left: 4px"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="delist" :disabled="!canManage || !!detail.delist_reason">
                下架
              </el-dropdown-item>
              <el-dropdown-item command="filled" :disabled="!canManage || !!detail.delist_reason">
                标记招满
              </el-dropdown-item>
              <el-dropdown-item command="extend-15" :disabled="!canManage">延期 15 天</el-dropdown-item>
              <el-dropdown-item command="extend-30" :disabled="!canManage">延期 30 天</el-dropdown-item>
              <el-dropdown-item command="restore" :disabled="!canManage || !detail.delist_reason">
                取消下架
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </template>
    </template>
  </DetailDrawer>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown } from '@element-plus/icons-vue'
import DetailDrawer from '@/components/DetailDrawer.vue'
import ImagePreview from '@/components/ImagePreview.vue'
import {
  fetchJobDetail,
  updateJob,
  delistJob,
  extendJob,
  restoreJob,
  retryReplacement,
  cancelReplacement,
} from '@/api/jobs'
import { formatDateTime, ttlLevel } from '@/utils/format'
import { ERROR_CODES } from '@/utils/constants'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  jobId: { type: [Number, String], default: null },
})
const emit = defineEmits(['update:modelValue', 'updated'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const detail = ref(null)
const loading = ref(false)
const saving = ref(false)
const editing = ref(false)
const dirty = ref(false)
const form = reactive({})
const canManage = computed(() => Boolean(
  detail.value?.activated_at && detail.value?.expires_at && !detail.value?.deleted_at,
))

watch(
  () => [props.modelValue, props.jobId],
  async ([v, id]) => {
    if (!v || !id) return
    await reload()
  },
  { immediate: true },
)

async function reload() {
  loading.value = true
  editing.value = false
  dirty.value = false
  try {
    detail.value = await fetchJobDetail(props.jobId)
  } finally {
    loading.value = false
  }
}

function startEdit() {
  if (!detail.value) return
  for (const k of Object.keys(form)) delete form[k]
  Object.assign(form, {
    city: detail.value.city,
    district: detail.value.district,
    address: detail.value.address,
    job_category: detail.value.job_category,
    pay_type: detail.value.pay_type,
    salary_floor_monthly: detail.value.salary_floor_monthly,
    salary_ceiling_monthly: detail.value.salary_ceiling_monthly,
  })
  editing.value = true
  dirty.value = false
}

function cancelEdit() {
  editing.value = false
  dirty.value = false
}

function markDirty() {
  dirty.value = true
}

function lifecycleLabel(job) {
  if (job.replaces_job_id && job.replacement_lifecycle_status === 'conflict') return '激活冲突'
  if (job.delist_reason === 'replaced') return '已被新版本替换'
  if (!job.activated_at && job.audit_status === 'pending') return '候选待审'
  if (!job.activated_at && job.audit_status === 'rejected') return '候选已驳回'
  if (job.deleted_at) return '历史记录'
  return '在线岗位'
}

async function retryActivation() {
  if (!detail.value?.replacement_id || !detail.value?.replaces_job_id) return
  const oldJob = await fetchJobDetail(detail.value.replaces_job_id)
  let prompt
  try {
    prompt = await ElMessageBox.prompt('请填写本次重试原因', '重试激活', {
      inputValidator: (value) => Boolean(value?.trim()) || '请输入原因',
    })
  } catch (_e) {
    return
  }
  await retryReplacement(detail.value.replacement_id, {
    old_job_version: oldJob.version,
    reason: prompt.value.trim(),
  })
  ElMessage.success('已重试激活')
  await reload()
  emit('updated')
}

async function cancelCandidate() {
  if (!detail.value?.replacement_id) return
  try {
    await ElMessageBox.confirm('确认取消该候选版本？', '取消候选', { type: 'warning' })
  } catch (_e) {
    return
  }
  await cancelReplacement(detail.value.replacement_id, { reason: 'operator_cancelled' })
  ElMessage.success('候选已取消')
  await reload()
  emit('updated')
}

async function save() {
  if (!detail.value) return
  saving.value = true
  try {
    await updateJob(detail.value.id, {
      version: detail.value.version,
      fields: { ...form },
    })
    ElMessage.success('已保存')
    await reload()
    emit('updated')
  } catch (err) {
    await handleConflict(err)
  } finally {
    saving.value = false
  }
}

async function handleConflict(err) {
  if (err && err.code === ERROR_CODES.VERSION_CONFLICT) {
    await ElMessageBox.alert('此岗位已被其他管理员修改，将刷新最新数据', '版本冲突', {
      type: 'warning',
    })
    await reload()
  }
}

async function onCommand(cmd) {
  if (!detail.value) return
  if (cmd === 'delist' || cmd === 'filled') {
    try {
      await ElMessageBox.confirm(
        cmd === 'filled' ? '确认标记该岗位为已招满？' : '确认下架该岗位？',
        '二次确认',
        { type: 'warning' },
      )
    } catch (_e) {
      return
    }
    try {
      await delistJob(detail.value.id, {
        version: detail.value.version,
        reason: cmd === 'filled' ? 'filled' : 'manual_delist',
      })
      ElMessage.success('操作成功')
      await reload()
      emit('updated')
    } catch (err) {
      await handleConflict(err)
    }
    return
  }
  if (cmd.startsWith('extend-')) {
    const days = Number(cmd.split('-')[1])
    try {
      await extendJob(detail.value.id, { version: detail.value.version, days })
      ElMessage.success(`已延期 ${days} 天`)
      await reload()
      emit('updated')
    } catch (err) {
      await handleConflict(err)
    }
    return
  }
  if (cmd === 'restore') {
    try {
      await ElMessageBox.confirm('确认取消下架？仅未过期岗位可取消', '二次确认', {
        type: 'warning',
      })
    } catch (_e) {
      return
    }
    try {
      await restoreJob(detail.value.id, { version: detail.value.version })
      ElMessage.success('已取消下架')
      await reload()
      emit('updated')
    } catch (err) {
      await handleConflict(err)
    }
  }
}
</script>
