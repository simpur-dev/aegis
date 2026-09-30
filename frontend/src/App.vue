<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import api from '@/api/client'
import { useEventStream } from '@/composables/useEventStream'

const route = useRoute()
const menuKey = computed(() => (route.name as string) ?? 'dashboard')

const health = ref<{ status: string; version: string } | null>(null)
const { events, connected } = useEventStream()

let timer: number | undefined

async function probe(): Promise<void> {
  try {
    health.value = await api.health()
  } catch {
    health.value = null
  }
}

onMounted(() => {
  void probe()
  timer = window.setInterval(() => void probe(), 15_000)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <a-layout style="min-height: 100vh">
    <a-layout-sider width="216" theme="dark">
      <div class="brand">
        <span class="logo">AEGIS</span>
        <span class="sub">山地灾害协同调控</span>
      </div>
      <a-menu :selected-keys="[menuKey]" theme="dark" mode="inline">
        <a-menu-item key="dashboard">
          <router-link to="/dashboard">态势总览</router-link>
        </a-menu-item>
        <a-menu-item key="monitor">
          <router-link to="/monitor">监测预警</router-link>
        </a-menu-item>
        <a-menu-item key="warnings">
          <router-link to="/warnings">预警发布</router-link>
        </a-menu-item>
        <a-menu-item key="map">
          <router-link to="/map">一张图</router-link>
        </a-menu-item>
        <a-menu-item key="workflow">
          <router-link to="/workflow">流程编排</router-link>
        </a-menu-item>
        <a-menu-item key="metrics">
          <router-link to="/metrics">指标量测</router-link>
        </a-menu-item>
      </a-menu>
    </a-layout-sider>

    <a-layout>
      <a-layout-header class="header">
        <span class="title">{{ route.meta.title ?? 'AEGIS' }}</span>
        <a-space>
          <a-tag :color="health ? 'green' : 'red'">
            {{ health ? `后端在线 v${health.version}` : '后端不可达' }}
          </a-tag>
          <a-tag :color="connected ? 'blue' : 'orange'">
            {{ connected ? '事件流已连接' : '事件流重连中' }}
          </a-tag>
          <a-badge :count="events.length" :overflow-count="99" title="最近事件" />
        </a-space>
      </a-layout-header>
      <a-layout-content class="content">
        <router-view />
      </a-layout-content>
      <a-layout-footer class="footer">
        AEGIS · Adaptive Emergency Geo-hazard Intelligence System ｜ 西藏山地灾害多智能体协同调控技术与平台
      </a-layout-footer>
    </a-layout>
  </a-layout>
</template>

<style>
body {
  margin: 0;
  background: #f5f5f5;
  font-family: -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif;
}
.brand {
  color: #fff;
  padding: 16px 12px;
  line-height: 1.3;
}
.brand .logo {
  font-size: 20px;
  font-weight: 700;
  letter-spacing: 2px;
}
.brand .sub {
  display: block;
  font-size: 12px;
  opacity: 0.7;
}
.header {
  background: #fff;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-inline: 24px;
}
.header .title {
  font-size: 16px;
  font-weight: 600;
}
.content {
  padding: 20px;
}
.footer {
  text-align: center;
  font-size: 12px;
  color: #8c8c8c;
}
</style>
