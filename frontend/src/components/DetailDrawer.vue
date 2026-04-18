<template>
  <el-drawer
    :model-value="modelValue"
    :title="title"
    :size="size"
    :before-close="onBeforeClose"
    @update:model-value="(v) => $emit('update:modelValue', v)"
  >
    <div v-loading="loading" class="drawer-body">
      <slot />
    </div>

    <template v-if="$slots.footer" #footer>
      <div class="drawer-footer">
        <slot name="footer" />
      </div>
    </template>
  </el-drawer>
</template>

<script setup>
import { ElMessageBox } from 'element-plus'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  title: { type: String, default: '详情' },
  size: { type: [String, Number], default: '640px' },
  loading: { type: Boolean, default: false },
  dirty: { type: Boolean, default: false },
})

defineEmits(['update:modelValue'])

async function onBeforeClose(done) {
  if (!props.dirty) {
    done()
    return
  }
  try {
    await ElMessageBox.confirm('有未保存的修改，确认关闭？', '未保存的修改', {
      confirmButtonText: '放弃修改',
      cancelButtonText: '继续编辑',
      type: 'warning',
    })
    done()
  } catch (_e) {
    // keep open
  }
}
</script>

<style scoped>
.drawer-body {
  padding: 0 4px;
}
.drawer-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>
