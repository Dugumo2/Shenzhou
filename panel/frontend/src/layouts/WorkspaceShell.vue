<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { breadcrumbsForPath, navigationForWorkspace, pageTemplateForPath } from '../navigation'
import ShellIcon from './ShellIcon.vue'
import ShellNavigation from './ShellNavigation.vue'
import PageFrame from './PageFrame.vue'

const props = withDefaults(defineProps<{ path: string; admin: boolean; username?: string; isStaff?: boolean; logoutBusy?: boolean }>(), { username: '', isStaff: false, logoutBusy: false })
defineEmits<{ logout: [] }>()
const collapsed = ref(false), mobile = ref(false), mobileOpen = ref(false)
const menuTrigger = ref<HTMLButtonElement | null>(null), drawer = ref<HTMLElement | null>(null), main = ref<HTMLElement | null>(null), accountMenu = ref<HTMLDetailsElement | null>(null)
const groups = computed(() => navigationForWorkspace(props.admin))
const breadcrumbs = computed(() => breadcrumbsForPath(props.path))
let media: MediaQueryList | undefined
let previousOverflow = ''
let mounted = false

function focusMain() { main.value?.focus() }
async function closeMobile(reason: 'dismiss' | 'navigation' | 'resize' = 'dismiss') {
  if (!mobileOpen.value) return
  mobileOpen.value = false
  if (typeof document !== 'undefined') document.body.style.overflow = previousOverflow
  await nextTick()
  if (!mounted) return
  if (reason === 'navigation' || (reason === 'resize' && !props.admin)) focusMain()
  else menuTrigger.value?.focus()
}
async function toggleNavigation() {
  if (!mobile.value) { collapsed.value = !collapsed.value; return }
  if (mobileOpen.value) { await closeMobile(); return }
  previousOverflow = document.body.style.overflow
  document.body.style.overflow = 'hidden'
  mobileOpen.value = true
  await nextTick()
  if (mobileOpen.value && mounted) (drawer.value?.querySelector<HTMLElement>('[aria-current="page"]') || drawer.value?.querySelector<HTMLElement>('button'))?.focus()
}
function onViewportChange(event: { matches: boolean }) {
  mobile.value = event.matches
  if (!event.matches) void closeMobile('resize')
}
function onKeydown(event: KeyboardEvent) {
  if (!mobileOpen.value) {
    if (event.key === 'Escape' && accountMenu.value?.open) {
      accountMenu.value.open = false
      accountMenu.value.querySelector<HTMLElement>('summary')?.focus()
      event.preventDefault()
    }
    return
  }
  if (event.key === 'Escape') { event.preventDefault(); void closeMobile(); return }
  if (event.key !== 'Tab') return
  const focusable = Array.from(drawer.value?.querySelectorAll<HTMLElement>('a[href], button:not([disabled]), summary, [tabindex]:not([tabindex="-1"])') || [])
  const first = focusable[0], last = focusable.at(-1)
  if (!first || !last) { event.preventDefault(); return }
  const active = document.activeElement
  if (event.shiftKey && (active === first || !drawer.value?.contains(active))) { event.preventDefault(); last.focus() }
  else if (!event.shiftKey && (active === last || !drawer.value?.contains(active))) { event.preventDefault(); first.focus() }
}
watch(() => props.path, () => {
  if (accountMenu.value) accountMenu.value.open = false
  void closeMobile('navigation')
})
onMounted(() => {
  mounted = true
  if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
    media = window.matchMedia('(max-width: 800px)')
    onViewportChange(media)
    media.addEventListener('change', onViewportChange)
  }
  if (typeof document !== 'undefined') document.addEventListener('keydown', onKeydown)
})
onUnmounted(() => {
  mounted = false
  media?.removeEventListener('change', onViewportChange)
  if (typeof document !== 'undefined') {
    document.removeEventListener('keydown', onKeydown)
    if (mobileOpen.value) document.body.style.overflow = previousOverflow
  }
})
</script>
<template>
  <div class="workspace-shell" :class="{ 'workspace-shell-admin': admin, 'workspace-shell-collapsed': admin && collapsed }">
    <div class="workspace-stage" :inert="mobileOpen ? true : undefined">
      <a href="#workspace-main" class="skip-navigation" @click.prevent="focusMain">跳到页面内容</a>
      <header class="shell-header">
        <RouterLink :to="admin ? '/admin/overview' : '/services'" class="brand shell-brand" :aria-label="admin ? '神舟云管理总览' : '神舟云首页'"><span class="brand-symbol">舟</span><span>神舟云</span></RouterLink>
        <button v-if="admin || mobile" ref="menuTrigger" type="button" class="shell-icon-button shell-menu-toggle" :aria-label="mobile ? '打开导航菜单' : collapsed ? '展开侧栏' : '折叠侧栏'" :aria-expanded="mobile ? mobileOpen : !collapsed" :aria-controls="mobile ? 'mobile-navigation' : 'desktop-navigation'" @click="toggleNavigation"><ShellIcon :name="mobile ? 'menu' : collapsed ? 'expand' : 'collapse'" /></button>
        <nav class="shell-breadcrumbs" aria-label="当前位置"><ol><li v-for="(item, index) in breadcrumbs" :key="item.label"><span v-if="index" class="breadcrumb-divider" aria-hidden="true">/</span><RouterLink v-if="item.path" :to="item.path">{{ item.label }}</RouterLink><span v-else :aria-current="index === breadcrumbs.length - 1 ? 'page' : undefined">{{ item.label }}</span></li></ol></nav>
        <ShellNavigation v-if="!admin && !mobile" class="shell-user-navigation" :groups="groups" :path="path" horizontal />
        <details ref="accountMenu" class="shell-account-menu">
          <summary :title="username" aria-label="账号菜单"><ShellIcon name="account" /><span class="username">{{ username }}</span><ShellIcon name="chevron" /></summary>
          <div class="shell-account-popover">
            <p class="shell-account-name">{{ username }}</p>
            <RouterLink to="/account">账号设置</RouterLink>
            <RouterLink v-if="isStaff && !admin" to="/admin/overview">管理面板</RouterLink>
            <RouterLink v-if="admin" to="/services">我的服务</RouterLink>
            <button type="button" :disabled="logoutBusy" @click="$emit('logout')">{{ logoutBusy ? '正在退出…' : '退出登录' }}</button>
          </div>
        </details>
      </header>
      <div class="shell-body">
        <aside v-if="admin && !mobile" id="desktop-navigation" class="shell-sidebar" aria-label="管理员侧栏">
          <ShellNavigation :groups="groups" :path="path" :collapsed="collapsed" />
          <RouterLink to="/services" class="shell-nav-link workspace-return" :class="{ 'workspace-return-collapsed': collapsed }" :aria-label="collapsed ? '切回我的服务' : undefined" :title="collapsed ? '切回我的服务' : undefined"><ShellIcon name="return" /><span :class="{ 'visually-hidden': collapsed }">切回我的服务</span></RouterLink>
        </aside>
        <div class="shell-content">
          <slot name="environment" />
          <main id="workspace-main" ref="main" class="shell-main" :class="admin ? 'admin-main' : 'page-container'" tabindex="-1">
            <PageFrame :kind="pageTemplateForPath(path)"><slot /></PageFrame>
          </main>
          <footer class="site-footer">神舟云 · 每一份服务，清楚可查</footer>
        </div>
      </div>
    </div>
    <div v-if="mobileOpen" class="shell-mobile-mask" @click.self="closeMobile()">
      <aside id="mobile-navigation" ref="drawer" class="shell-mobile-drawer" role="dialog" aria-modal="true" aria-labelledby="mobile-navigation-title">
        <div class="shell-drawer-heading"><strong id="mobile-navigation-title">{{ admin ? '管理导航' : '我的空间' }}</strong><button type="button" class="shell-icon-button" aria-label="关闭导航菜单" @click="closeMobile()"><ShellIcon name="close" /></button></div>
        <ShellNavigation :groups="groups" :path="path" @navigate="closeMobile('navigation')" />
        <RouterLink v-if="admin" to="/services" class="shell-nav-link workspace-return" @click="closeMobile('navigation')"><ShellIcon name="return" /><span>切回我的服务</span></RouterLink>
      </aside>
    </div>
  </div>
</template>
