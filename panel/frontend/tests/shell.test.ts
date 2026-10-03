import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { parse, compileScript } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
import * as vue from 'vue'
import * as navigation from '../src/navigation.ts'

const require = createRequire(import.meta.url)
const components = new Map<string, any>()
function loadComponent(relative: string, replacements: Record<string, unknown> = {}): any {
  const cached = components.get(relative)
  if (cached && !Object.keys(replacements).length) return cached
  const descriptor = parse(readFileSync(new URL('../src/' + relative, import.meta.url), 'utf8')).descriptor
  const compiled = compileScript(descriptor, { id: 'shell-' + relative, inlineTemplate: true })
  const script = ts.transpileModule(compiled.content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const exports: Record<string, any> = {}
  const localRequire = (name: string): unknown => {
    if (name in replacements) return replacements[name]
    if (name === 'vue') return vue
    if (name === '../navigation') return navigation
    if (name.endsWith('.vue')) return { default: loadComponent(new URL(name, 'https://local/' + relative).pathname.slice(1)) }
    return require(name)
  }
  new Function('require', 'exports', script)(localRequire, exports)
  if (!Object.keys(replacements).length) components.set(relative, exports.default)
  return exports.default
}
const WorkspaceShell = loadComponent('layouts/WorkspaceShell.vue')
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); await vue.nextTick() }

// 使用 Vue 真实渲染器执行组件及事件；此宿主只代替 DOM，不重写导航逻辑。
class Element {
  tag: string
  props: Record<string, any> = {}
  children: Element[] = []
  parent: Element | null = null
  text = ''
  open = false
  constructor(tag: string) { this.tag = tag; vue.markRaw(this) }
  focus() { (globalThis.document as any).activeElement = this }
  contains(target: unknown): boolean { return target === this || this.children.some(child => child.contains(target)) }
  querySelectorAll<T>(selector: string): T[] {
    return descendants(this).filter(item => {
      if (selector === '[aria-current="page"]') return item.props['aria-current'] === 'page'
      if (selector === 'button' || selector === 'summary') return item.tag === selector
      return item.tag === 'a' && item.props.href || item.tag === 'button' && !item.props.disabled || item.tag === 'summary' || item.props.tabindex !== undefined && item.props.tabindex !== '-1'
    }) as T[]
  }
  querySelector<T>(selector: string): T | null { return this.querySelectorAll<T>(selector)[0] || null }
}
function descendants(root: Element): Element[] { return root.children.flatMap(child => [child, ...descendants(child)]) }
function invoke(handler: any, event: Record<string, unknown> = {}) {
  if (Array.isArray(handler)) handler.forEach(fn => fn(event))
  else handler?.(event)
}
function routerLink(state: { path: string; admin: boolean }, staff = true) {
  return vue.defineComponent({
    inheritAttrs: false,
    props: { to: { type: String, required: true }, custom: Boolean },
    setup(props, { slots, attrs }) {
      const navigate = () => { state.path = String(props.to); state.admin = staff && state.path.startsWith('/admin/') }
      return () => props.custom ? slots.default?.({ href: '#' + props.to, navigate }) : vue.h('a', {
        ...attrs, href: '#' + props.to, onClick: (event: Record<string, unknown>) => { navigate(); invoke(attrs.onClick, event) },
      }, slots.default?.())
    },
  })
}
function mount({ admin = true, staff = true, mobile = false, path = '/admin/services' } = {}) {
  const previousDocument = Object.getOwnPropertyDescriptor(globalThis, 'document'), previousWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
  const listeners = new Map<string, Set<(event: any) => void>>()
  const mediaListeners = new Set<(event: { matches: boolean }) => void>()
  const media = { matches: mobile, addEventListener: (_: string, fn: any) => mediaListeners.add(fn), removeEventListener: (_: string, fn: any) => mediaListeners.delete(fn) }
  const document = {
    activeElement: null as Element | null, body: { style: { overflow: 'scroll' } },
    addEventListener: (type: string, fn: any) => { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type)!.add(fn) },
    removeEventListener: (type: string, fn: any) => listeners.get(type)?.delete(fn),
  }
  Object.defineProperty(globalThis, 'document', { configurable: true, value: document })
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { matchMedia: () => media } })
  const renderer = vue.createRenderer<Element, Element>({
    createElement: tag => new Element(tag), createText: text => { const node = new Element('#text'); node.text = text; return node }, createComment: text => { const node = new Element('#comment'); node.text = text; return node },
    setText: (node, text) => { node.text = text }, setElementText: (node, text) => { node.text = text; node.children = [] },
    parentNode: node => node.parent, nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] || null,
    patchProp: (node, key, _, value) => { if (value === null || value === undefined) delete node.props[key]; else node.props[key] = value },
    insert: (node, parent, anchor = null) => { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); const at = anchor ? parent.children.indexOf(anchor) : -1; if (at < 0) parent.children.push(node); else parent.children.splice(at, 0, node); node.parent = parent },
    remove: node => { node.parent?.children.splice(node.parent.children.indexOf(node), 1); node.parent = null },
  })
  const state = vue.reactive({ path, admin })
  const app = renderer.createApp({ render: () => vue.h(WorkspaceShell, { ...state, isStaff: staff, username: '用于检验长名称不会撑开顶栏的测试账号' }, {
    default: () => vue.h('h1', '页面内容'), environment: () => vue.h('p', { class: 'candidate-banner' }, '本地候选 · 演示数据'),
  }) })
  app.component('RouterLink', routerLink(state, staff))
  const root = new Element('root')
  app.mount(root)
  let unmounted = false
  return {
    state, document, listeners, mediaListeners, ready: flush(),
    all: () => descendants(root),
    find: (predicate: (node: Element) => boolean) => descendants(root).find(predicate)!,
    async resize(matches: boolean) { media.matches = matches; mediaListeners.forEach(fn => fn(media)); await flush() },
    async key(key: string, shiftKey = false) { let prevented = false; listeners.get('keydown')?.forEach(fn => fn({ key, shiftKey, preventDefault: () => { prevented = true } })); await flush(); return prevented },
    async click(node: Element) { invoke(node.props.onClick, { target: node, currentTarget: node, preventDefault() {}, stopPropagation() {} }); await flush() },
    unmount() { if (unmounted) return; unmounted = true; app.unmount(); if (previousDocument) Object.defineProperty(globalThis, 'document', previousDocument); else delete (globalThis as any).document; if (previousWindow) Object.defineProperty(globalThis, 'window', previousWindow); else delete (globalThis as any).window },
  }
}
const hasClass = (node: Element, className: string) => String(node.props.class || '').split(' ').includes(className)

test('导航匹配精确路径边界；运维仅保留配置，详情面包屑不泄露标识', () => {
  assert.deepEqual(navigation.navigationForWorkspace(true).map(group => group.label), ['业务管理', '连接资源'])
  assert.deepEqual(navigation.navigationForWorkspace(false).flatMap(group => group.items).map(item => item.path), ['/services', '/guides', '/account'])
  assert.equal(navigation.adminNavigation.find(group => group.id === 'operations')?.enabled, false)
  assert.equal(navigation.isNavigationActive('/admin/users/123', '/admin/users'), true)
  assert.equal(navigation.isNavigationActive('/admin/users-other', '/admin/users'), false)
  assert.deepEqual(navigation.breadcrumbsForPath('/services/private-id'), [{ label: '我的服务', path: '/services' }, { label: '服务详情' }])
})

test('桌面折叠真实组件后保持活动路由，图标有名称，规则子页恢复选中与面包屑', async t => {
  const ui = mount(); t.after(() => ui.unmount()); await ui.ready
  const sidebar = ui.find(node => node.props.id === 'desktop-navigation')
  assert.equal(sidebar.querySelectorAll<Element>('[aria-current="page"]').length, 1)
  assert.equal(sidebar.querySelector<Element>('[aria-current="page"]')!.props.href, '#/admin/services')
  await ui.click(ui.find(node => hasClass(node, 'shell-menu-toggle')))
  assert.equal(ui.state.path, '/admin/services')
  assert.ok(ui.find(node => hasClass(node, 'workspace-shell-collapsed')))
  assert.equal(sidebar.querySelector<Element>('[aria-current="page"]')!.props['aria-label'], '订阅管理')
  ui.state.path = '/admin/rules/sources'; await flush()
  assert.equal(sidebar.querySelector<Element>('[aria-current="page"]')!.props.href, '#/admin/rules')
  assert.ok(ui.all().some(node => node.text === '规则来源'))
  assert.equal(ui.find(node => hasClass(node, 'page-frame')).props['data-page-template'], 'import')
})

test('普通用户只有独立三项导航，服务详情与指南切换不生成管理入口', async t => {
  const ui = mount({ admin: false, staff: false, path: '/services/test-service' }); t.after(() => ui.unmount()); await ui.ready
  assert.equal(ui.all().filter(node => node.tag === 'a' && String(node.props.href).startsWith('#/admin/')).length, 0)
  const nav = ui.find(node => hasClass(node, 'shell-user-navigation'))
  assert.equal(nav.querySelector<Element>('[aria-current="page"]')!.props.href, '#/services')
  assert.equal(ui.find(node => hasClass(node, 'page-frame')).props['data-page-template'], 'detail')
  await ui.click(descendants(nav).find(node => node.props.href === '#/guides')!)
  assert.equal(ui.state.path, '/guides')
  assert.equal(nav.querySelector<Element>('[aria-current="page"]')!.props.href, '#/guides')
})

test('手机导航打开后聚焦当前页并锁定背景；Tab循环，导航关闭后聚焦正文', async t => {
  const ui = mount({ mobile: true }); t.after(() => ui.unmount()); await ui.ready
  await ui.click(ui.find(node => hasClass(node, 'shell-menu-toggle')))
  const drawer = ui.find(node => node.props.role === 'dialog')
  assert.equal(drawer.props['aria-modal'], 'true')
  assert.equal(ui.document.activeElement?.props.href, '#/admin/services')
  assert.equal(ui.document.body.style.overflow, 'hidden')
  assert.equal(ui.find(node => hasClass(node, 'workspace-stage')).props.inert, true)
  const focusable = drawer.querySelectorAll<Element>('a[href], button:not([disabled]), summary, [tabindex]:not([tabindex="-1"])')
  focusable.at(-1)!.focus(); assert.equal(await ui.key('Tab'), true); assert.equal(ui.document.activeElement, focusable[0])
  assert.equal(await ui.key('Tab', true), true); assert.equal(ui.document.activeElement, focusable.at(-1))
  await ui.click(descendants(drawer).find(node => node.props.href === '#/admin/servers')!)
  assert.equal(ui.state.path, '/admin/servers')
  assert.equal(ui.all().some(node => node.props.role === 'dialog'), false)
  assert.equal(ui.document.activeElement?.props.id, 'workspace-main')
  assert.equal(ui.document.body.style.overflow, 'scroll')
  assert.equal(ui.find(node => hasClass(node, 'workspace-stage')).props.inert, undefined)
  await ui.click(ui.find(node => hasClass(node, 'shell-menu-toggle')))
  assert.equal(ui.document.activeElement?.props.href, '#/admin/servers')
})

test('Escape与遮罩均真实关闭手机抽屉并恢复菜单按钮焦点', async t => {
  const ui = mount({ mobile: true }); t.after(() => ui.unmount()); await ui.ready
  const trigger = ui.find(node => hasClass(node, 'shell-menu-toggle'))
  await ui.click(trigger)
  assert.equal(await ui.key('Escape'), true)
  assert.equal(ui.document.activeElement, trigger)
  assert.equal(ui.all().some(node => node.props.role === 'dialog'), false)
  await ui.click(trigger)
  await ui.click(ui.find(node => hasClass(node, 'shell-mobile-mask')))
  assert.equal(ui.document.activeElement, trigger)
  assert.equal(ui.document.body.style.overflow, 'scroll')
})

test('真实媒体变化关闭抽屉并保留折叠和选中；普通用户恢复正文焦点', async t => {
  const ui = mount(); t.after(() => ui.unmount()); await ui.ready
  await ui.click(ui.find(node => hasClass(node, 'shell-menu-toggle')))
  await ui.resize(true)
  await ui.click(ui.find(node => hasClass(node, 'shell-menu-toggle')))
  await ui.resize(false)
  assert.equal(ui.all().some(node => node.props.role === 'dialog'), false)
  assert.ok(ui.find(node => hasClass(node, 'workspace-shell-collapsed')))
  assert.equal(ui.document.activeElement?.props['aria-label'], '展开侧栏')
  assert.equal(ui.find(node => node.props.id === 'desktop-navigation').querySelector<Element>('[aria-current="page"]')!.props.href, '#/admin/services')
  ui.unmount()
  const user = mount({ admin: false, staff: false, mobile: true, path: '/services' }); t.after(() => user.unmount()); await user.ready
  await user.click(user.find(node => hasClass(node, 'shell-menu-toggle')))
  await user.resize(false)
  assert.equal(user.document.activeElement?.props.id, 'workspace-main')
})

test('卸载打开的抽屉清理键盘与媒体监听并恢复已有滚动状态', async () => {
  const ui = mount({ mobile: true }); await ui.ready
  await ui.click(ui.find(node => hasClass(node, 'shell-menu-toggle')))
  ui.unmount()
  assert.equal(ui.document.body.style.overflow, 'scroll')
  assert.equal(ui.listeners.get('keydown')?.size, 0)
  assert.equal(ui.mediaListeners.size, 0)
})

test('账号菜单Escape恢复摘要焦点；SSR无浏览器全局且普通账号不渲染管理员侧栏', async t => {
  const ui = mount(); t.after(() => ui.unmount()); await ui.ready
  const account = ui.find(node => node.tag === 'details'); account.open = true
  assert.equal(await ui.key('Escape'), true)
  assert.equal(account.open, false)
  assert.equal(ui.document.activeElement?.tag, 'summary')
  ui.unmount()
  assert.equal(typeof globalThis.window, 'undefined')
  const state = vue.reactive({ path: '/admin/services', admin: false })
  const App = loadComponent('App.vue', {
    'vue-router': { useRoute: () => state },
    './auth': { auth: { session: { user: { username: '普通账号', is_staff: false }, environment: { kind: 'local_candidate', is_demo: true } } }, applySession() {} },
    './api': { request: async () => ({}), errorMessage: () => '失败' },
    './router': { router: { replace: async () => undefined } },
  })
  const app = vue.createSSRApp(App)
  app.component('RouterLink', routerLink(state, false))
  app.component('RouterView', { render: () => vue.h('p', '页面内容') })
  app.component('el-alert', { render: () => null })
  const html = await renderToString(app)
  assert.ok(html.includes('页面内容'))
  assert.ok(html.includes('本地候选 · 演示数据'))
  assert.equal(html.includes('管理员侧栏'), false)
  assert.equal(html.includes('href="#/admin/'), false)
})
