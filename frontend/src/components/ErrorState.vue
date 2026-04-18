<template>
  <div class="error-state">
    <el-result icon="error" :title="title" :sub-title="subTitle">
      <template #extra>
        <el-button type="primary" @click="$emit('retry')">重试</el-button>
      </template>
    </el-result>
  </div>
</template>

<script setup>
defineProps({
  title: { type: String, default: '加载失败' },
  subTitle: { type: String, default: '' },
})
defineEmits(['retry'])
</script>
