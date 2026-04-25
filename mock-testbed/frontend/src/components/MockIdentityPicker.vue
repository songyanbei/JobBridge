<template>
  <el-select
    v-model="selected"
    filterable
    :placeholder="placeholder"
    style="width: 100%"
    @change="onChange"
  >
    <el-option-group
      v-for="group in grouped"
      :key="group.role"
      :label="group.label"
    >
      <el-option
        v-for="u in group.users"
        :key="u.external_userid"
        :label="`${u.name}（${u.external_userid}）`"
        :value="u.external_userid"
      />
    </el-option-group>
    <el-option
      v-if="!users.length && !loading"
      :value="''"
      label="⚠️ 未找到 wm_mock_* 用户，请先执行 seed.sh"
      disabled
    />
  </el-select>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { fetchMockUsers } from '@/api.js'

const props = defineProps({
  modelValue: { type: String, default: '' },
  roleFilter: { type: Array, default: null },
  placeholder: { type: String, default: '选择模拟身份' },
})
const emit = defineEmits(['update:modelValue', 'change'])

const users = ref([])
const loading = ref(false)
const selected = ref(props.modelValue)

watch(() => props.modelValue, (v) => { selected.value = v })

const ROLE_LABELS = {
  worker: '求职者（worker）',
  factory: '招聘者 · 厂家（factory）',
  broker: '招聘者 · 中介（broker）',
}

const grouped = computed(() => {
  const filtered = props.roleFilter
    ? users.value.filter(u => props.roleFilter.includes(u.role))
    : users.value
  const buckets = { worker: [], factory: [], broker: [] }
  for (const u of filtered) {
    if (buckets[u.role]) buckets[u.role].push(u)
  }
  return Object.entries(buckets)
    .filter(([, us]) => us.length > 0)
    .map(([role, us]) => ({ role, label: ROLE_LABELS[role] || role, users: us }))
})

async function load() {
  loading.value = true
  try {
    const resp = await fetchMockUsers()
    users.value = resp?.users || []
  } catch (err) {
    console.error('[MockIdentityPicker] load failed', err)
    users.value = []
  } finally {
    loading.value = false
  }
}

function onChange(v) {
  emit('update:modelValue', v)
  emit('change', v)
}

onMounted(load)
</script>
