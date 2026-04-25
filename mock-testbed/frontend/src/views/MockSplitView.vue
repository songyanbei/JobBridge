<template>
  <div class="split-root">
    <MockBanner />
    <div class="split-intro">
      <h2>Mock 企业微信测试台 · 双视角模式</h2>
      <p>
        左栏扮演招聘者（厂家 / 中介），右栏扮演求职者。
        切换身份 → 发消息 → 观察 SSE 气泡弹出真实 bot 回复。
      </p>
      <p class="hint">
        多窗口模式：在 URL 加 <code>?external_userid=wm_mock_worker_001&role=worker</code> 进入单视角。
      </p>
    </div>
    <div class="split-container">
      <div class="pane pane-left">
        <div class="pane-label">👔 招聘者视角（factory / broker）</div>
        <MockChatPanel :role-filter="['factory', 'broker']" placeholder="选择招聘者身份" />
      </div>
      <div class="pane pane-right">
        <div class="pane-label">🧑‍🔧 求职者视角（worker）</div>
        <MockChatPanel :role-filter="['worker']" placeholder="选择求职者身份" />
      </div>
    </div>
  </div>
</template>

<script setup>
import MockBanner from '@/components/MockBanner.vue'
import MockChatPanel from '@/components/MockChatPanel.vue'
</script>

<style scoped>
.split-root {
  display: flex;
  flex-direction: column;
  height: 100vh;
  padding-top: 36px; /* 给 banner 留位 */
  box-sizing: border-box;
}
.split-intro {
  padding: 16px 24px 8px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
}
.split-intro h2 {
  margin: 0 0 6px;
  font-size: 18px;
  color: #303133;
}
.split-intro p {
  margin: 0;
  color: #606266;
  font-size: 13px;
  line-height: 1.5;
}
.split-intro .hint {
  color: #909399;
  font-size: 12px;
  margin-top: 4px;
}
.split-intro code {
  padding: 1px 6px;
  background: #f5f7fa;
  border-radius: 3px;
  font-family: "SFMono-Regular", Menlo, Consolas, monospace;
  font-size: 11px;
  color: #e6a23c;
}
.split-container {
  display: flex;
  flex: 1;
  min-height: 0;
  padding: 16px;
  gap: 16px;
  background: #f5f7fa;
}
.pane {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.pane-label {
  padding: 6px 4px;
  font-weight: 600;
  color: #303133;
  font-size: 14px;
}
</style>
