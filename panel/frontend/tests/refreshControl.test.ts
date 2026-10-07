import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse, compileScript } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
import * as vue from 'vue'
import * as display from '../src/display.ts'

const source = readFileSync(new URL('../src/components/RefreshControl.vue', import.meta.url), 'utf8')
const script = compileScript(parse(source).descriptor, { id: 'RefreshControl', inlineTemplate: true })
const exports: Record<string, any> = {}
const js = ts.transpileModule(script.content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
new Function('require', 'exports', js)((name: string) => {
  if (name === 'vue') return vue
  if (name === '../display') return display
  throw new Error('未声明的测试依赖：' + name)
}, exports)
async function render(props: Record<string, unknown>) {
  const app = vue.createSSRApp(exports.default, props)
  app.component('ElButton', { props: ['disabled'], setup: (props: any, { attrs, slots }: any) => () => vue.h('button', { ...attrs, disabled: props.disabled }, slots.default?.()) })
  return renderToString(app)
}

test('刷新控件显示图标、可访问反馈，读取时刻不冒充来源采样', async () => {
  const html = await render({ lastReadAt: '2026-10-07T05:00:00Z' })
  assert.match(html, /<svg[^>]*aria-hidden="true"/)
  assert.match(html, /role="status" aria-live="polite" aria-atomic="true"/)
  assert.match(html, /aria-describedby="v-0"/)
  assert.match(html, /页面读取：/)
  assert.match(html, /已读取最新可用数据/)
  assert.doesNotMatch(html, /来源采样：/)
  const withSource = await render({ lastReadAt: '2026-10-07T05:00:00Z', sampledAt: '2026-10-04T05:00:00Z' })
  assert.match(withSource, /来源采样：<time datetime="2026-10-04T05:00:00Z"/)
  assert.match(withSource, /页面读取：<time datetime="2026-10-07T05:00:00Z"/)
})

test('读取期间禁重复触发，失败有重试且保留旧读取时刻', async () => {
  const pending = await render({ loading: true, lastReadAt: '2026-10-07T05:00:00Z' })
  assert.match(pending, /正在读取已采集数据/)
  assert.match(pending, /aria-busy="true"/)
  assert.match(pending, /<button[^>]*disabled/)
  assert.match(pending, /页面读取：/)
  const failed = await render({ error: '网络失败', lastReadAt: '2026-10-07T05:00:00Z' })
  assert.match(failed, /读取失败，已保留上次内容/)
  assert.match(failed, /重试读取/)
  assert.match(failed, /datetime="2026-10-07T05:00:00Z"/)
  const firstFailed = await render({ error: '网络失败' })
  assert.match(firstFailed, /读取失败，请重试/)
  assert.doesNotMatch(firstFailed, /已保留上次内容/)
})

test('真实组件点击发出刷新，loading/disabled 时即使事件送达也不触发重复读取', async () => {
  type HostNode = { tag: string; props: Record<string, any>; children: HostNode[] }
  const node = (tag: string): HostNode => ({ tag, props: {}, children: [] })
  const renderer = vue.createRenderer<HostNode, HostNode>({
    createElement: node, createText: () => node('#text'), createComment: () => node('#comment'),
    insert: (child, parent) => { parent.children.push(child) }, remove: () => {},
    setText: () => {}, setElementText: () => {}, parentNode: () => null, nextSibling: () => null,
    patchProp: (element, key, _previous, value) => { element.props[key] = value },
  })
  let clicks = 0
  const state = vue.reactive({ loading: false, disabled: false, error: '' })
  const app = renderer.createApp({ setup: () => () => vue.h(exports.default, { ...state, onRefresh: () => { clicks++ } }) })
  app.component('ElButton', { props: ['disabled'], setup: (props: any, { attrs, slots }: any) => () => vue.h('button', { ...attrs, disabled: props.disabled }, slots.default?.()) })
  const root = node('root')
  app.mount(root)
  function findButton(element: HostNode): HostNode | undefined { return element.tag === 'button' ? element : element.children.map(findButton).find(Boolean) }
  const button = findButton(root)!
  button.props.onClick(); assert.equal(clicks, 1)
  state.loading = true; await vue.nextTick()
  button.props.onClick(); assert.equal(clicks, 1)
  state.loading = false; state.disabled = true; await vue.nextTick()
  button.props.onClick(); assert.equal(clicks, 1)
  state.disabled = false; state.error = '读取失败'; await vue.nextTick()
  button.props.onClick(); assert.equal(clicks, 2)
  app.unmount()
})
