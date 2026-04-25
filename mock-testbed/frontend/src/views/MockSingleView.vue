<template>
  <div class="single-root">
    <MockBanner />
    <div class="single-intro">
      <h2>Mock 企业微信测试台 · 单视角</h2>
      <p>
        当前身份：<code>{{ initialUserid || '（未指定，将用下拉选择）' }}</code>
        ，角色：<code>{{ roleLabel }}</code>
      </p>
      <p class="hint">
        切到双栏模式：<router-link to="/">回首页</router-link> 或 <router-link to="/split">/split</router-link>
      </p>
    </div>
    <div class="single-container">
      <MockChatPanel
        :role-filter="roleFilter"
        :initial-userid="initialUserid"
        :placeholder="placeholder"
      />
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import MockBanner from '@/components/MockBanner.vue'
import MockChatPanel from '@/components/MockChatPanel.vue'

const route = useRoute()

const initialUserid = computed(() => String(route.query.external_userid || ''))
const role = computed(() => String(route.query.role || 'worker').toLowerCase())

const ROLE_LABEL = {
  worker: '求职者',
  factory: '招聘者（厂家）',
  broker: '招聘者（中介）',
}
const roleLabel = computed(() => ROLE_LABEL[role.value] || role.value)

const roleFilter = computed(() => {
  if (role.value === 'worker') return ['worker']
  if (role.value === 'factory') return ['factory']
  if (role.value === 'broker') return ['broker']
  // 默认招聘者两种都可选
  return ['factory', 'broker']
})

const placeholder = computed(() => `选择${roleLabel.value}身份`)
</script>

<style scoped>
.single-root {
  display: flex;
  flex-direction: column;
  height: 100vh;
  padding-top: 36px;
  box-sizing: border-box;
}
.single-intro {
  padding: 16px 24px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
}
.single-intro h2 {
  margin: 0 0 6px;
  font-size: 18px;
  color: #303133;
}
.single-intro p {
  margin: 0;
  color: #606266;
  font-size: 13px;
}
.single-intro .hint {
  color: #909399;
  font-size: 12px;
  margin-top: 4px;
}
.single-intro code {
  padding: 1px 6px;
  background: #f5f7fa;
  border-radius: 3px;
  font-family: "SFMono-Regular", Menlo, Consolas, monospace;
  font-size: 11px;
  color: #409eff;
}
.single-intro a {
  color: #409eff;
  text-decoration: none;
}
.single-intro a:hover { text-decoration: underline; }
.single-container {
  flex: 1;
  min-height: 0;
  padding: 16px;
  background: #f5f7fa;
  display: flex;
}
.single-container > :deep(.chat-panel) {
  flex: 1;
  max-width: 880px;
  margin: 0 auto;
}
</style>
