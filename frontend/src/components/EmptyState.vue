<template>
  <el-empty :description="description" :image-size="size">
    <slot />
  </el-empty>
</template>

<script setup>
defineProps({
  description: { type: String, default: '暂无数据' },
  size: { type: Number, default: 80 },
})
</script>
