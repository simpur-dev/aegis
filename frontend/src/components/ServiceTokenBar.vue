<script setup lang="ts">
/**
 * 服务令牌条（§4.8.1 的前端出口）：页脚一行，填一次，本机留着。
 *
 * 为什么放在页脚而不是顶栏：顶栏右列此前量过尺寸（@1440 只剩 295px，两枚胶囊就 273–285px），
 * 再塞一个输入框必然把状态文案挤成省略号——而"后端在线/事件流已连接"那两句是值班员要看的。
 * 令牌是低频配置（一台机器填一次），该占常驻信息的位置。
 *
 * 为什么常态也显示而不是只在 401 时弹：一个刚部署完的现场，值班员不知道"这台开了令牌"，
 * 只看得到八张页面都读不出数；这一行把"要凭据"这件事摆在每页都在的地方，比让人猜省事。
 */
import { computed, ref, watch } from 'vue'

import { authRejected, clearServiceToken, serviceToken, setServiceToken } from '@/api/serviceToken'

const draft = ref(serviceToken.value)
// 别处（用例、其它面板）改了令牌时，输入框跟着走，不出现"存的是一个、框里写的是另一个"
watch(serviceToken, (value) => {
  if (value !== draft.value) draft.value = value
})

const state = computed<string>(() => {
  if (authRejected.value) return 'rejected'
  return serviceToken.value === '' ? 'unset' : 'saved'
})
const hintText = computed<string>(() => {
  if (state.value === 'rejected') return '请求被鉴权面拒了（401）：核对令牌后保存，各页下一次取数就会恢复'
  if (state.value === 'saved') return `已保存（本机 ${serviceToken.value.length} 位，只存在这台浏览器的 localStorage）`
  return '未设置。后端配了 AEGIS_SERVICE_TOKEN 时，这里要填同一个值；没配令牌的部署留空即可'
})

function save(): void {
  setServiceToken(draft.value)
}

function wipe(): void {
  clearServiceToken()
  draft.value = ''
}
</script>

<template>
  <section class="tok" :class="`is-${state}`" data-testid="service-token-bar">
    <label class="tok__label" for="aegis-service-token">服务令牌</label>
    <input
      id="aegis-service-token"
      v-model="draft"
      class="tok__input"
      data-testid="service-token-input"
      type="password"
      autocomplete="off"
      spellcheck="false"
      placeholder="留空 = 不带凭据"
    />
    <button class="tok__btn" type="button" data-testid="service-token-save" @click="save">保存</button>
    <button
      v-if="serviceToken !== ''"
      class="tok__btn tok__btn--ghost"
      type="button"
      data-testid="service-token-clear"
      @click="wipe"
    >
      清除
    </button>
    <span class="tok__hint" data-testid="service-token-hint">{{ hintText }}</span>
  </section>
</template>

<style scoped>
/* 页脚那一行的高度预算很小：整条控制在 28px 内，窄屏下先藏提示语，不藏输入框。 */
.tok {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s2);
  margin: 0 0 var(--aeg-space-s2);
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-secondary);
}
.tok__label {
  flex: 0 0 auto;
  letter-spacing: 0.04em;
}
.tok__input {
  flex: 0 1 220px;
  min-width: 96px;
  height: 24px;
  padding: 0 var(--aeg-space-s2);
  font-family: var(--aeg-font-mono);
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text);
  background: var(--aeg-color-card);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-sm);
}
.tok__input:focus-visible {
  border-color: var(--aeg-color-brand);
  outline: 2px solid var(--aeg-color-brand-soft);
  outline-offset: 1px;
}
.is-rejected .tok__input {
  border-color: var(--aeg-color-status-danger);
}
.tok__btn {
  flex: 0 0 auto;
  height: 24px;
  padding: 0 var(--aeg-space-s3);
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-inverse);
  background: var(--aeg-color-brand);
  border: none;
  border-radius: var(--aeg-radius-pill);
  cursor: pointer;
}
.tok__btn:hover {
  background: var(--aeg-color-brand-hover);
}
.tok__btn--ghost {
  color: var(--aeg-color-text-secondary);
  background: transparent;
  border: 1px solid var(--aeg-color-border);
}
.tok__hint {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.is-rejected .tok__hint {
  color: var(--aeg-color-status-danger);
}
</style>
