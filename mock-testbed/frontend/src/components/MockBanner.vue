<template>
  <div class="mock-banner" role="alert" aria-live="assertive">
    ⚠️ MOCK-WEWORK TESTBED — 非真实企业微信环境，仅用于演示 / 联调 ⚠️
  </div>
</template>

<script setup>
// 刻意不接受任何 props、不做条件渲染。
// 只要本组件被挂载，横幅就必须可见，任何 URL 参数或 localStorage 都无法隐藏。
</script>

<style scoped>
.mock-banner {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 9999;
  background: #d32f2f;
  color: #fff;
  padding: 8px 16px;
  font-size: 13px;
  font-weight: 700;
  text-align: center;
  letter-spacing: 0.5px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
  user-select: none;
}
</style>
