<template>
  <DetailDrawer
    v-model="visible"
    title="简历详情"
    :loading="loading"
    :dirty="dirty"
    size="640px"
  >
    <div v-if="detail">
      <el-descriptions :column="2" border>
        <el-descriptions-item label="ID">{{ detail.id }}</el-descriptions-item>
        <el-descriptions-item label="版本">v{{ detail.version }}</el-descriptions-item>

        <!-- ---- 工人（owner）信息 ---- -->
        <el-descriptions-item label="姓名">{{ detail.owner_display_name || '--' }}</el-descriptions-item>
        <el-descriptions-item label="电话">{{ detail.owner_phone || '--' }}</el-descriptions-item>
        <el-descriptions-item label="工人 ID" :span="2">{{ detail.owner_userid }}</el-descriptions-item>

        <el-descriptions-item label="性别">
          <el-select v-if="editing" v-model="form.gender" @change="markDirty">
            <el-option label="男" value="男" />
            <el-option label="女" value="女" />
          </el-select>
          <span v-else>{{ detail.gender || '--' }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="年龄">
          <el-input-number
            v-if="editing"
            v-model="form.age"
            :min="16"
            :max="80"
            @change="markDirty"
          />
          <span v-else>{{ detail.age }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="期望薪资下限">
          <el-input-number
            v-if="editing"
            v-model="form.salary_expect_floor_monthly"
            :min="0"
            @change="markDirty"
          />
          <span v-else>{{ detail.salary_expect_floor_monthly ? detail.salary_expect_floor_monthly + ' 元/月' : '--' }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="工期偏好">
          <span v-if="detail.accept_long_term && detail.accept_short_term">长期 / 短期都接受</span>
          <span v-else-if="detail.accept_long_term">长期</span>
          <span v-else-if="detail.accept_short_term">短期</span>
          <span v-else>--</span>
        </el-descriptions-item>
        <el-descriptions-item label="期望城市" :span="2">
          <span>{{ Array.isArray(detail.expected_cities) ? detail.expected_cities.join('、') : (detail.expected_cities || '--') }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="期望工种" :span="2">
          <span>{{ Array.isArray(detail.expected_job_categories) ? detail.expected_job_categories.join('、') : (detail.expected_job_categories || '--') }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="审核状态">
          <el-tag
            :type="{ passed: 'success', rejected: 'danger', pending: 'warning' }[detail.audit_status] || 'info'"
            size="small"
          >
            {{ { passed: '已通过', rejected: '已驳回', pending: '待审' }[detail.audit_status] || '--' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="到期时间">
          <span :class="`jb-${ttlLevel(detail.expires_at)}-text`">
            {{ formatDateTime(detail.expires_at) }}
          </span>
        </el-descriptions-item>
        <el-descriptions-item v-if="detail.raw_text" label="原始描述" :span="2">
          <span style="white-space: pre-wrap">{{ detail.raw_text }}</span>
        </el-descriptions-item>
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
        <el-button @click="startEdit">编辑</el-button>
        <el-dropdown @command="onCommand">
          <el-button>
            更多操作
            <el-icon style="margin-left: 4px"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="delist" :disabled="!!detail.delist_reason">下架</el-dropdown-item>
              <el-dropdown-item command="extend-15">延期 15 天</el-dropdown-item>
              <el-dropdown-item command="extend-30">延期 30 天</el-dropdown-item>
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
import { fetchResumeDetail, updateResume, delistResume, extendResume } from '@/api/resumes'
import { formatDateTime, ttlLevel } from '@/utils/format'
import { ERROR_CODES } from '@/utils/constants'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  resumeId: { type: [Number, String], default: null },
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

watch(
  () => [props.modelValue, props.resumeId],
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
    detail.value = await fetchResumeDetail(props.resumeId)
  } finally {
    loading.value = false
  }
}

function startEdit() {
  if (!detail.value) return
  for (const k of Object.keys(form)) delete form[k]
  Object.assign(form, {
    gender: detail.value.gender,
    age: detail.value.age,
    salary_expect_floor_monthly: detail.value.salary_expect_floor_monthly,
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

async function save() {
  if (!detail.value) return
  saving.value = true
  try {
    await updateResume(detail.value.id, {
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
    await ElMessageBox.alert('此简历已被其他管理员修改，将刷新最新数据', '版本冲突', {
      type: 'warning',
    })
    await reload()
  }
}

async function onCommand(cmd) {
  if (!detail.value) return
  if (cmd === 'delist') {
    try {
      await ElMessageBox.confirm('确认下架该简历？', '二次确认', { type: 'warning' })
    } catch (_e) {
      return
    }
    try {
      await delistResume(detail.value.id, {
        version: detail.value.version,
        reason: 'manual_delist',
      })
      ElMessage.success('已下架')
      await reload()
      emit('updated')
    } catch (err) {
      await handleConflict(err)
    }
  }
  if (cmd.startsWith('extend-')) {
    const days = Number(cmd.split('-')[1])
    try {
      await extendResume(detail.value.id, { version: detail.value.version, days })
      ElMessage.success(`已延期 ${days} 天`)
      await reload()
      emit('updated')
    } catch (err) {
      await handleConflict(err)
    }
  }
}
</script>
