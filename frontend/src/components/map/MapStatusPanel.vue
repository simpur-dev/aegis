<script setup lang="ts">
/**
 * MapStatusPanel —— 「一张图」左列：离线状态 + 未定位清单 + 未渲染的几何。
 *
 * 从原 `MapPanel.vue` 拆出来的左半（2026-10-30）。仍然只有 props/emits：
 * 不取数、不判断、不认识 Cesium；分组规则在 entities.ts。
 */

import { computed } from 'vue'

import type { RejectedGeometry, UnlocatedItem, UnlocatedReason } from '@/components/map/entities'
import { groupUnlocatedByReason, UNLOCATED_LABELS } from '@/components/map/entities'
import type { NetworkStatus } from '@/components/map/offline'
import { NETWORK_LABELS, NETWORK_TAG_COLORS } from '@/components/map/offline'

const props = defineProps<{
  status: NetworkStatus
  statusReason: string
  basemapMessage: string
  terrainMessage: string
  unlocated: UnlocatedItem[]
  rejected: RejectedGeometry[]
  zoom: number
  loading: boolean
  /** 三维场景起不来的原因（null 表示正常）；此时面板仍然可用。 */
  sceneError: string | null
}>()

const emit = defineEmits<{
  (event: 'select', featureId: string): void
  (event: 'refresh'): void
}>()

// 分组规则本身在 entities.ts（本组件不判断），这里只是把那份结果摆出来
const unlocatedGroups = computed(() => groupUnlocatedByReason(props.unlocated))
</script>

<template>
  <div class="panel">
    <a-card size="small" title="离线状态">
      <template #extra>
        <a-button size="small" :loading="props.loading" @click="emit('refresh')">刷新</a-button>
      </template>
      <a-space direction="vertical" size="small" style="width: 100%">
        <a-tag :color="NETWORK_TAG_COLORS[props.status]">
          {{ NETWORK_LABELS[props.status] }} · 缩放 {{ props.zoom }}
        </a-tag>
        <div class="note">{{ props.statusReason }}</div>
        <a-divider style="margin: 4px 0" />
        <div class="note">底图：{{ props.basemapMessage }}</div>
        <div class="note">地形：{{ props.terrainMessage }}</div>
        <a-alert
          v-if="props.sceneError"
          type="error"
          show-icon
          message="三维场景不可用"
          :description="props.sceneError"
        />
      </a-space>
    </a-card>

    <a-card size="small">
      <template #title>
        未定位清单
        <a-tag v-if="props.unlocated.length" color="orange">{{ props.unlocated.length }}</a-tag>
      </template>
      <a-empty v-if="!props.unlocated.length" description="全部要素均已落到图上" />
      <!-- 光给一个总数答不了"我到底缺哪几样"：站点缺坐标与区域没烘焙锚点
           是两件要找不同的人办的事，混在一个数字里就都看不见了 -->
      <div v-if="unlocatedGroups.length" class="unlocated-groups" data-testid="unlocated-groups">
        <span v-for="group in unlocatedGroups" :key="group.reason" class="unlocated-group">
          {{ group.label }} × {{ group.count }}
        </span>
      </div>
      <!-- 上面那句"全部要素均已落到图上"已经说完了，这里再挂一个空列表就会叠出第二句
           antd 默认的"暂无数据"（真机 @1440 空数据档量到：同一块卡里两句并存，互相打脸）。
           所以列表只在真有内容时渲染。 -->
      <a-list v-else-if="props.unlocated.length > 0" size="small" :data-source="props.unlocated">
        <template #renderItem="{ item }">
          <a-list-item>
            <a-space direction="vertical" size="0" style="width: 100%">
              <span class="unlocated-title">{{ item.title }}</span>
              <span class="note">{{ UNLOCATED_LABELS[item.reason as UnlocatedReason] }}</span>
            </a-space>
            <template #actions>
              <a-button type="link" size="small" @click="emit('select', item.id)">详情</a-button>
            </template>
          </a-list-item>
        </template>
      </a-list>
      <div class="note tip">
        宁可列清单，也不把无坐标要素画到 (0,0)：一个位于几内亚湾的假站点会被误读成"高原没灾情"。
      </div>
    </a-card>

    <a-card v-if="props.rejected.length" size="small" title="未渲染的几何">
      <a-list size="small" :data-source="props.rejected">
        <template #renderItem="{ item }">
          <a-list-item>
            <span class="note">#{{ item.index }} {{ item.id }} — {{ item.reason }}（{{ item.detail }}）</span>
          </a-list-item>
        </template>
      </a-list>
    </a-card>
  </div>
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.note {
  font-size: 12px;
  color: var(--aeg-color-text-secondary);
  line-height: 1.6;
}
.tip {
  margin-top: 8px;
}
.unlocated-groups {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
  margin-bottom: 8px;
  font-size: 12px;
  color: var(--aeg-color-text-secondary);
}
.unlocated-group {
  border-left: 2px solid var(--aeg-color-status-warning);
  padding-left: 6px;
}
.unlocated-title {
  font-size: 13px;
}
</style>
