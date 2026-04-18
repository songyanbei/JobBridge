<template>
  <div class="jb-page">
    <div class="jb-page-header">
      <div class="jb-page-title">城市字典</div>
      <div>
        <el-input
          v-model="keyword"
          placeholder="按城市/拼音/别名搜索"
          clearable
          style="width: 240px"
        />
        <el-button type="primary" @click="load">刷新</el-button>
      </div>
    </div>

    <div v-loading="loading" class="cities-wrap">
      <el-collapse v-model="activeProvinces">
        <el-collapse-item
          v-for="(cities, province) in filteredGroups"
          :key="province"
          :name="province"
          :title="`${province}（${cities.length}）`"
        >
          <el-table :data="cities" size="small" border>
            <el-table-column prop="name" label="城市名" width="160" />
            <el-table-column prop="pinyin" label="拼音" width="160" />
            <el-table-column label="别名">
              <template #default="{ row }">
                <div v-if="row._editing" class="alias-edit">
                  <el-input-tag v-if="hasInputTag" v-model="row._aliases" />
                  <el-select
                    v-else
                    v-model="row._aliases"
                    multiple
                    filterable
                    allow-create
                    default-first-option
                    style="width: 100%"
                    :placeholder="'输入别名后回车添加'"
                  />
                </div>
                <div v-else>
                  <el-tag
                    v-for="a in row.aliases || []"
                    :key="a"
                    size="small"
                    style="margin-right: 4px"
                  >
                    {{ a }}
                  </el-tag>
                  <span v-if="!(row.aliases && row.aliases.length)" class="jb-muted">--</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column prop="status" label="状态" width="100">
              <template #default="{ row }">
                <el-tag :type="row.enabled === false ? 'info' : 'success'" size="small">
                  {{ row.enabled === false ? '禁用' : '启用' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="160" fixed="right">
              <template #default="{ row }">
                <template v-if="row._editing">
                  <el-button
                    link
                    type="primary"
                    size="small"
                    :loading="row._saving"
                    @click="onSave(row)"
                  >
                    保存
                  </el-button>
                  <el-button link size="small" @click="onCancel(row)">取消</el-button>
                </template>
                <el-button v-else link type="primary" size="small" @click="onEdit(row)">
                  编辑别名
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-collapse-item>
      </el-collapse>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { fetchCities, updateCity } from '@/api/dicts'

const loading = ref(false)
const groups = ref({})
const activeProvinces = ref([])
const keyword = ref('')
const hasInputTag = false

const filteredGroups = computed(() => {
  const q = keyword.value.trim().toLowerCase()
  if (!q) return groups.value
  const result = {}
  for (const [prov, cities] of Object.entries(groups.value)) {
    const hit = cities.filter(
      (c) =>
        (c.name || '').toLowerCase().includes(q) ||
        (c.pinyin || '').toLowerCase().includes(q) ||
        (c.aliases || []).some((a) => (a || '').toLowerCase().includes(q)),
    )
    if (hit.length) result[prov] = hit
  }
  return result
})

async function load() {
  loading.value = true
  try {
    const data = await fetchCities()
    const raw = Array.isArray(data) ? data : data.items || []
    const next = {}
    for (const c of raw) {
      const prov = c.province || c.province_name || '其他'
      if (!next[prov]) next[prov] = []
      next[prov].push({
        ...c,
        _editing: false,
        _aliases: [],
        _saving: false,
      })
    }
    groups.value = next
    if (!activeProvinces.value.length) activeProvinces.value = Object.keys(next).slice(0, 3)
  } finally {
    loading.value = false
  }
}

function onEdit(row) {
  row._aliases = [...(row.aliases || [])]
  row._editing = true
}
function onCancel(row) {
  row._editing = false
}
async function onSave(row) {
  row._saving = true
  try {
    await updateCity(row.id, {
      version: row.version,
      aliases: row._aliases,
    })
    ElMessage.success('已保存')
    row.aliases = [...row._aliases]
    row._editing = false
  } finally {
    row._saving = false
  }
}

load()
</script>

<style scoped>
.cities-wrap {
  padding-bottom: 20px;
}
</style>
