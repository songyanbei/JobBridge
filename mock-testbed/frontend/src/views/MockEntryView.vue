<template>
  <component :is="target" />
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import MockSplitView from './MockSplitView.vue'
import MockSingleView from './MockSingleView.vue'

const route = useRoute()

// 带 external_userid / role 参数 → 单视角
// 否则 → 双栏
const target = computed(() => (route.query.external_userid ? MockSingleView : MockSplitView))
</script>
