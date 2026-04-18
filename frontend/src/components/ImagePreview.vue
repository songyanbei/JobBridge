<template>
  <div class="image-preview">
    <div v-if="!images || images.length === 0" class="jb-muted">无图片</div>
    <div v-else class="thumb-grid">
      <el-image
        v-for="(src, idx) in images"
        :key="idx"
        :src="src"
        :preview-src-list="images"
        :initial-index="idx"
        fit="cover"
        class="thumb"
        :preview-teleported="true"
      >
        <template #error>
          <div class="thumb-error">加载失败</div>
        </template>
      </el-image>
    </div>
  </div>
</template>

<script setup>
defineProps({
  images: { type: Array, default: () => [] },
})
</script>

<style scoped>
.thumb-grid {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.thumb {
  width: 96px;
  height: 96px;
  border-radius: 4px;
  border: 1px solid var(--el-border-color-lighter);
  overflow: hidden;
}
.thumb-error {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--el-fill-color-lighter);
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
