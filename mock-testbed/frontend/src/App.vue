<template>
  <router-view />
</template>

<script setup>
// Root component, purely renders the current route.
// Global banner is rendered inside each view (not here) so that
// when/if this sandbox is removed, App.vue has nothing to clean up.
</script>

<style>
html, body, #app {
  margin: 0;
  padding: 0;
  height: 100%;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  background: #f5f7fa;
}
</style>
