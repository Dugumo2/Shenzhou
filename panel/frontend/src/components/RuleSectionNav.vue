<script setup lang="ts">
withDefaults(defineProps<{
  active: 'custom' | 'sources' | 'policies'
  readOnly?: boolean
  production: boolean
  publishAvailable?: boolean
  httpsAvailable?: boolean
}>(), { readOnly: undefined, publishAvailable: undefined, httpsAvailable: undefined })
const sections = [
  { id: 'custom', label: '自建规则', path: '/admin/rules' },
  { id: 'sources', label: '规则来源', path: '/admin/rules/sources' },
  { id: 'policies', label: '规则方案', path: '/admin/rule-policies' },
]
</script>

<template>
  <div class="rule-section-header">
    <nav class="rule-section-tabs" aria-label="规则管理分区">
      <RouterLink v-for="section in sections" :key="section.id" :to="section.path"
        class="rule-section-tab" :class="{ selected: active === section.id }"
        :aria-current="active === section.id ? 'page' : undefined">{{ section.label }}</RouterLink>
    </nav>
    <section v-if="readOnly === true" class="rule-access-note" aria-label="规则修改说明">
      <div>
        <strong>{{ production ? '此页面暂不支持修改线上规则' : '当前规则接口只读' }}</strong>
        <p v-if="production">可以查看记录、历史版本和命中结果。修改线上规则请使用现有管理入口；保存、发布和客户端应用是不同步骤。</p>
        <p v-else>当前环境尚未开放保存操作，可以查看记录和检查匹配。</p>
      </div>
      <a v-if="production" class="rule-management-link" href="/manage/client-routing/">管理线上规则 <span aria-hidden="true">→</span></a>
    </section>
    <p v-if="publishAvailable === false" class="rule-capability-note">方案发布到订阅链接尚未接通，编译材料不代表客户端已应用。<span v-if="httpsAvailable === false"> HTTPS 来源获取暂未开放。</span></p>
  </div>
</template>

<style scoped>
.rule-section-header { margin: 0 0 24px; }
.rule-section-tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--border, #dfe6ed); }
.rule-section-tab { display: inline-flex; align-items: center; justify-content: center; min-height: 44px; padding: 10px 20px; color: var(--muted, #687887); text-decoration: none; font-weight: 500; border-bottom: 3px solid transparent; margin-bottom: -1px; }
.rule-section-tab.selected { color: var(--el-color-primary, #3568d4); border-bottom-color: currentColor; font-weight: 650; }
.rule-section-tab:hover { color: var(--el-color-primary, #3568d4); background: #f5f8fc; }
.rule-section-tab:focus-visible, .rule-management-link:focus-visible { outline: 2px solid var(--el-color-primary, #3568d4); outline-offset: 3px; }
.rule-access-note { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-top: 18px; padding: 16px 18px; border: 1px solid var(--border, #dfe6ed); border-radius: 8px; background: #f7f9fc; }
.rule-access-note strong { font-size: 14px; }
.rule-access-note p, .rule-capability-note { margin: 6px 0 0; color: var(--muted, #687887); font-size: 13px; line-height: 1.7; }
.rule-management-link { display: inline-flex; align-items: center; justify-content: center; gap: 8px; min-height: 44px; padding: 10px 16px; flex-shrink: 0; border-radius: 6px; background: var(--el-color-primary, #3568d4); color: white; text-decoration: none; font-size: 14px; font-weight: 600; }
.rule-management-link:hover { filter: brightness(.94); }
.rule-capability-note { margin-top: 12px; }
@media (max-width: 640px) {
  .rule-section-tab { flex: 1; padding: 10px 8px; white-space: nowrap; }
  .rule-access-note { align-items: stretch; flex-direction: column; gap: 12px; }
}
</style>
