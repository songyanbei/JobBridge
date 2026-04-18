<template>
  <el-dialog
    :model-value="modelValue"
    title="键盘快捷键"
    width="420px"
    @update:model-value="(v) => $emit('update:modelValue', v)"
  >
    <el-table :data="items" border size="small">
      <el-table-column prop="key" label="按键" width="90" />
      <el-table-column prop="desc" label="说明" />
    </el-table>
    <div class="jb-muted tip">输入框聚焦时，快捷键不会触发。</div>
  </el-dialog>
</template>

<script setup>
defineProps({
  modelValue: { type: Boolean, default: false },
})
defineEmits(['update:modelValue'])

const items = [
  { key: 'P', desc: '通过当前审核条目' },
  { key: 'R', desc: '打开驳回理由面板' },
  { key: 'S', desc: '稍后处理（跳到下一条）' },
  { key: 'E', desc: '进入编辑模式' },
  { key: 'U', desc: '撤销最近一次动作（30s 内）' },
  { key: '?', desc: '打开快捷键帮助' },
]
</script>

<style scoped>
.tip {
  margin-top: 8px;
  font-size: 12px;
}
</style>
