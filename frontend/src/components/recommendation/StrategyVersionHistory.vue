<template>
  <div class="version-history">
    <div class="section-title">
      发布历史（release history）
      <span class="jb-muted">
        不可变快照，是回滚的恢复真源。回滚必须显式选择一个 target_revision，并会生成一个更高的新 revision。
      </span>
    </div>

    <el-table v-loading="loading" :data="history" border size="small">
      <el-table-column label="revision" width="90">
        <template #default="{ row }">
          <span class="mono">{{ row.revision }}</span>
          <el-tag v-if="row.revision === currentRevision" size="small" type="success" class="cur-tag">
            当前
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="120">
        <template #default="{ row }">
          {{ RELEASE_OPERATION_LABEL[row.operation] || row.operation }}
        </template>
      </el-table-column>
      <el-table-column label="执行模式" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="modeTagType(row.execution_mode)">{{ row.execution_mode }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="稳定版本" width="90">
        <template #default="{ row }">
          <span class="mono">{{ row.stable_version_id ?? 'legacy' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="候选版本" width="90">
        <template #default="{ row }">
          <span class="mono">{{ row.candidate_version_id ?? '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="灰度比例" width="90">
        <template #default="{ row }">
          <span class="mono">{{ row.rollout_percentage ?? 0 }}%</span>
        </template>
      </el-table-column>
      <el-table-column label="回滚目标" width="90">
        <template #default="{ row }">
          <span class="mono">{{ row.target_revision ?? '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="change_reason" label="修改原因" min-width="180" show-overflow-tooltip />
      <el-table-column prop="created_by" label="操作人" width="120" show-overflow-tooltip />
      <el-table-column label="时间" width="150">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="130" fixed="right">
        <template #default="{ row }">
          <el-button
            link
            type="danger"
            size="small"
            :disabled="!canRollback || row.revision === currentRevision"
            @click="$emit('rollback', row.revision)"
          >
            回滚到此 revision
          </el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无发布历史" :image-size="70" />
      </template>
    </el-table>

    <div class="section-title">
      策略版本（strategy version）
      <span class="jb-muted">草稿可编辑；已发布和已归档的版本参数不可变。</span>
    </div>

    <el-table v-loading="loading" :data="versions" border size="small">
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="param-detail">
            <span v-for="meta in STRATEGY_PARAM_META" :key="meta.key" class="param-item">
              <span class="jb-muted">{{ meta.label }}</span>
              <span class="mono">{{ paramValue(row, meta) }}</span>
            </span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="版本" width="90">
        <template #default="{ row }">
          <span class="mono">v{{ row.version_no }}</span>
        </template>
      </el-table-column>
      <el-table-column label="id" width="70">
        <template #default="{ row }"><span class="mono">{{ row.id }}</span></template>
      </el-table-column>
      <el-table-column label="模板" width="110">
        <template #default="{ row }">{{ templateLabel(row.template_key) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="statusTagType(row.status)">
            {{ VERSION_STATUS_LABEL[row.status] || row.status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="角色" width="90">
        <template #default="{ row }">
          <el-tag v-if="row.id === release?.stable_version_id" size="small" type="success">稳定</el-tag>
          <el-tag v-else-if="row.id === release?.candidate_version_id" size="small" type="warning">候选</el-tag>
          <span v-else class="jb-muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="参数指纹" width="130">
        <template #default="{ row }">
          <span class="mono">{{ shortDigest(row.parameters_digest) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="模拟指纹" width="130">
        <template #default="{ row }">
          <span class="mono">{{ shortDigest(row.last_simulated_digest) || '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="change_reason" label="修改原因" min-width="160" show-overflow-tooltip />
      <el-table-column prop="created_by" label="创建人" width="110" show-overflow-tooltip />
      <el-table-column label="创建时间" width="150">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="发布人 / 时间" width="180">
        <template #default="{ row }">
          <span v-if="row.published_at">{{ row.published_by }} · {{ formatDateTime(row.published_at) }}</span>
          <span v-else class="jb-muted">—</span>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无策略版本" :image-size="70" />
      </template>
    </el-table>
  </div>
</template>

<script setup>
import {
  RELEASE_OPERATION_LABEL,
  STRATEGY_PARAM_META,
  VERSION_STATUS_LABEL,
  templateLabel,
} from '@/api/recommendationStrategies'
import { formatDateTime } from '@/utils/format'

defineProps({
  history: { type: Array, default: () => [] },
  versions: { type: Array, default: () => [] },
  release: { type: Object, default: null },
  currentRevision: { type: Number, default: 0 },
  canRollback: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
})

defineEmits(['rollback'])

function shortDigest(digest) {
  if (!digest) return ''
  return String(digest).slice(0, 12)
}

function modeTagType(mode) {
  if (mode === 'on') return 'success'
  if (mode === 'shadow') return 'warning'
  return 'info'
}

function statusTagType(status) {
  if (status === 'published') return 'success'
  if (status === 'draft') return 'warning'
  return 'info'
}

function paramValue(row, meta) {
  const value = row?.parameters?.[meta.key]
  if (value === null || value === undefined) return '—'
  if (meta.type === 'enum') {
    return meta.options.find((o) => o.value === value)?.label || value
  }
  return `${value}${meta.unit || ''}`
}
</script>

<style scoped>
.version-history {
  display: grid;
  gap: 12px;
}
.section-title {
  font-weight: 600;
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}
.section-title .jb-muted {
  font-weight: 400;
  font-size: 12.5px;
}
.cur-tag {
  margin-left: 6px;
}
.param-detail {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
  padding: 8px 16px;
  font-size: 12.5px;
}
.param-item {
  display: inline-flex;
  gap: 5px;
}
</style>
