import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'
import * as vueRouter from 'vue-router'
import * as api from '../src/api.ts'
import type { Session } from '../src/types.ts'

const guest = (): Session => ({ authenticated: false, user: null, csrf_token: 'test-guest-csrf',
  brand: '测试', time_zone: 'Asia/Shanghai', password_policy: { min_length: 12, messages: [], validated_by_server: true } })
const member = (username = 'demo'): Session => ({ ...guest(), authenticated: true,
  user: { username, is_staff: false }, csrf_token: 'test-member-csrf' })
type Sender = (path: string, method?: string, body?: unknown, signal?: AbortSignal) => Promise<any>
function deferred<T>() { let resolve!: (v: T) => void, reject!: (e: unknown) => void
  const promise = new Promise<T>((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); await vue.nextTick() }
function authModule(send: Sender) {
  const source = readFileSync(new URL('../src/auth.ts', import.meta.url), 'utf8')
  const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const exports: any = {}, tokens: string[] = []
  new Function('require', 'exports', output)((name: string) => name === 'vue' ? vue :
    name === './api' ? { ...api, request: send, setCsrfToken: (token: string) => tokens.push(token) } : undefined, exports)
  return { ...exports, tokens }
}
function loginPage(send: Sender, replace: (target: string) => Promise<unknown>, redirect?: unknown) {
  const auth = authModule(send), exports: any = {}, mounted: Array<() => unknown> = []
  const source = readFileSync(new URL('../src/pages/LoginPage.vue', import.meta.url), 'utf8')
  const output = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'login-recovery' }).content,
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const route = { query: { redirect } }, scope = vue.effectScope()
  new Function('require', 'exports', output)((name: string) => name === 'vue' ? { ...vue, onMounted: (fn: () => unknown) => mounted.push(fn) } :
    name === 'vue-router' ? { ...vueRouter, useRoute: () => route } : name === '../auth' ? auth :
    name === '../api' ? { ...api, request: send } : name === '../router' ? { router: { replace } } : undefined, exports)
  const state = scope.run(() => exports.default.setup({}, { expose: () => {} }))
  return { state, auth, route, mount: async () => { for (const fn of mounted) await fn() }, stop: () => scope.stop() }
}

test('真实登录组件：认证成功后导航异常保留会话，再试只导航不重发登录', async () => {
  let posts = 0, navigations = 0
  const ui = loginPage(async (_path, method) => method === 'POST' ? (++posts, member()) : posts ? member() : guest(), async () => {
    if (++navigations === 1) throw new TypeError('synthetic lazy module failure')
  })
  await ui.mount(); ui.state.username.value = 'demo'; ui.state.password.value = 'synthetic-password'
  await ui.state.login()
  assert.equal(ui.auth.auth.session.authenticated, true)
  assert.match(ui.state.error.value, /已登录.*页面/)
  assert.equal(ui.state.password.value, '')
  await ui.state.login()
  assert.equal(posts, 1); assert.equal(navigations, 2); assert.equal(ui.state.error.value, '')
  ui.stop()
})

test('迟到的初始化session不得覆盖刚登录的身份和CSRF', async () => {
  const old = deferred<Session>(), auth = authModule(async () => old.promise)
  const initial = auth.hydrateSession(true)
  auth.applySession(member())
  old.resolve(guest()); await initial
  assert.equal(auth.auth.session.user?.username, 'demo')
  assert.equal(auth.tokens.at(-1), 'test-member-csrf')
})

test('网络丢失、无效响应和already_authenticated只读恢复同账号，不重发POST', async () => {
  for (const failure of [new api.ApiError(0, 'NETWORK_ERROR', '网络异常'),
    new api.ApiError(200, 'INVALID_RESPONSE', '响应异常'), new api.ApiError(409, 'already_authenticated', '请先退出当前账号。')]) {
    let reads = 0, posts = 0
    const auth = authModule(async (_path, method) => {
      if (method === 'POST') { posts++; throw failure }
      return ++reads === 1 ? guest() : member()
    })
    await auth.authenticate('demo', 'synthetic-password')
    assert.equal(posts, 1); assert.equal(reads, 2)
    assert.equal(auth.auth.session.user.username, 'demo'); assert.equal(auth.tokens.at(-1), 'test-member-csrf')
  }
})

test('恢复读失败后下次先读已建立会话，不再POST或要求退出', async () => {
  let reads = 0, posts = 0
  const auth = authModule(async (_path, method) => {
    if (method === 'POST') { posts++; throw new api.ApiError(0, 'NETWORK_ERROR', '网络异常') }
    reads++
    if (reads === 2) throw new api.ApiError(0, 'NETWORK_ERROR', '读取失败')
    return reads === 1 ? guest() : member()
  })
  await assert.rejects(auth.authenticate('demo', 'synthetic-password'), /暂时无法确认登录状态/)
  await auth.authenticate('demo', 'synthetic-password')
  assert.equal(posts, 1); assert.equal(reads, 3); assert.equal(auth.auth.session.user.username, 'demo')
})

test('已有他人会话和恢复读到他人身份均不借用账号', async () => {
  let posts = 0
  const existing = authModule(async (_path, method) => { if (method === 'POST') posts++; return member('another') })
  await assert.rejects(existing.authenticate('demo', 'synthetic-password'), /其他账号/)
  assert.equal(posts, 0)
  let reads = 0
  const recovering = authModule(async (_path, method) => {
    if (method === 'POST') throw new api.ApiError(409, 'already_authenticated', '请先退出')
    return ++reads === 1 ? guest() : member('another')
  })
  await assert.rejects(recovering.authenticate('demo', 'synthetic-password'), /其他账号/)
  assert.notEqual(recovering.auth.session?.user?.username, 'another')
})

test('错误密码、CSRF拒绝、限频不触发恢复也不宣告成功', async () => {
  for (const failure of [new api.ApiError(401, 'invalid_credentials', '账号或密码不正确'),
    new api.ApiError(403, 'csrf_failed', '校验失败'), new api.ApiError(429, 'rate_limited', '限频')]) {
    let reads = 0, posts = 0
    const auth = authModule(async (_path, method) => {
      if (method === 'POST') { posts++; throw failure }
      reads++; return guest()
    })
    await assert.rejects(auth.authenticate('demo', 'wrong'), e => e === failure)
    assert.equal(reads, 1); assert.equal(posts, 1); assert.equal(auth.auth.session.authenticated, false)
  }
})

test('恢复为匿名时不能误报登录成功，用户名遵循服务端NFKC规范', async () => {
  const anonymous = authModule(async (_path, method) => {
    if (method === 'POST') throw new api.ApiError(0, 'NETWORK_ERROR', '网络异常')
    return guest()
  })
  await assert.rejects(anonymous.authenticate('demo', 'synthetic-password'), /尚未确认登录成功/)
  assert.equal(anonymous.auth.session.authenticated, false)
  const normalized = authModule(async () => member('demo'))
  await normalized.authenticate(' ｄｅｍｏ ', 'synthetic-password')
  assert.equal(normalized.auth.session.user.username, 'demo')
})

test('退出使未完成读取及登录失效，迟到结果不能复活身份', async () => {
  const old = deferred<Session>(), auth = authModule(async () => old.promise)
  const read = auth.hydrateSession(true); auth.clearSession(); old.resolve(member()); await read
  assert.equal(auth.auth.session, null); assert.equal(auth.tokens.at(-1), '')
  const post = deferred<Session>()
  const signing = authModule(async (_path, method) => method === 'POST' ? post.promise : guest())
  const login = signing.authenticate('demo', 'synthetic-password'); await flush()
  signing.clearSession(); post.resolve(member())
  await assert.rejects(login, /登录状态已变化/)
  assert.equal(signing.auth.session, null)
})

test('登录预检查期间退出，不得继续发送密码；并发认证不得发送双POST', async () => {
  const read = deferred<Session>(); let posts = 0
  const auth = authModule(async (_path, method) => { if (method === 'POST') { posts++; return member() }; return read.promise })
  const login = auth.authenticate('demo', 'synthetic-password'); await flush()
  auth.clearSession(); read.resolve(guest())
  await assert.rejects(login, /登录状态已变化/); assert.equal(posts, 0)
  const post = deferred<Session>()
  const concurrent = authModule(async (_path, method) => method === 'POST' ? (++posts, post.promise) : guest())
  const first = concurrent.authenticate('demo', 'synthetic-password'); await flush()
  await assert.rejects(concurrent.authenticate('demo', 'synthetic-password'), /正在登录/)
  post.resolve(member()); await first; assert.equal(posts, 1)
})

test('真实登录组件处理导航返回的中止结果，不误当页面已进入', async () => {
  const ui = loginPage(async (_path, method) => method === 'POST' ? member() : guest(), async () => ({ type: 4 }))
  await ui.mount(); ui.state.username.value = 'demo'; ui.state.password.value = 'synthetic-password'
  await ui.state.login(); assert.match(ui.state.error.value, /已登录.*页面/)
  assert.equal(ui.state.canContinue.value, true); assert.equal(ui.state.busy.value, false); ui.stop()
})

test('进入服务前重新确认身份，其他标签切换账号或会话过期不能借旧状态进入', async () => {
  for (const fresh of [member('another'), guest()]) {
    let posts = 0, navigations = 0
    const ui = loginPage(async (_path, method) => { if (method === 'POST') posts++; return fresh }, async () => { navigations++ })
    ui.auth.applySession(member()); ui.state.username.value = 'demo'
    assert.equal(ui.state.canContinue.value, true)
    await ui.state.login()
    assert.equal(posts, 0); assert.equal(navigations, 0)
    assert.match(ui.state.error.value, fresh.authenticated ? /其他账号/ : /登录已过期/)
    ui.stop()
  }
})

test('取消旧session请求后迟到401不触发真实API的失权回调', async () => {
  const response = deferred<Response>(), originalFetch = globalThis.fetch
  let unauthorized = 0
  globalThis.fetch = async () => response.promise
  api.setUnauthorizedHandler(() => unauthorized++)
  try {
    const auth = authModule(api.request), read = auth.hydrateSession(true)
    auth.applySession(member())
    response.resolve(new Response(JSON.stringify({ error: { code: 'session_expired' } }), { status: 401 }))
    await read
    assert.equal(unauthorized, 0); assert.equal(auth.auth.session.user.username, 'demo')
  } finally { globalThis.fetch = originalFetch; api.setUnauthorizedHandler(() => {}) }
})

test('真实登录组件重复提交只产生一个POST，身份变化后不能沿用继续入口', async () => {
  const post = deferred<Session>(); let posts = 0, navigations = 0
  const ui = loginPage(async (_path, method) => method === 'POST' ? (++posts, post.promise) : guest(), async () => { navigations++ })
  await ui.mount(); ui.state.username.value = 'demo'; ui.state.password.value = 'synthetic-password'
  const first = ui.state.login(); await flush(); await ui.state.login()
  assert.equal(posts, 1); post.resolve(member()); await first
  assert.equal(navigations, 1); assert.equal(ui.state.canContinue.value, true)
  ui.state.username.value = 'another'; await ui.state.login()
  assert.equal(navigations, 1); assert.equal(ui.state.canContinue.value, false)
  ui.auth.clearSession(); assert.equal(ui.state.canContinue.value, false); ui.stop()
})

test('真实登录组件保留安全内部redirect，拒外部、反斜线、登录循环及坏编码', async () => {
  for (const [redirect, expected] of [
    ['/admin/servers?status=enabled', '/admin/servers?status=enabled'],
    ['//outside.test', '/services'], ['https://outside.test', '/services'], ['/\\outside.test', '/services'],
    ['/%5Coutside.test', '/services'], ['/login?redirect=/admin', '/services'], ['/%', '/services'],
  ]) {
    const targets: string[] = []
    const ui = loginPage(async (_path, method) => method === 'POST' ? member() : guest(), async target => { targets.push(target) }, redirect)
    await ui.mount(); ui.state.username.value = 'demo'; ui.state.password.value = 'synthetic-password'
    await ui.state.login(); assert.deepEqual(targets, [expected]); ui.stop()
  }
})

test('旧读取finally不能清除更新读取，失败不把ready标成功', async () => {
  const first = deferred<Session>(), second = deferred<Session>(); let calls = 0
  const auth = authModule(async () => ++calls === 1 ? first.promise : second.promise)
  const oldRead = auth.hydrateSession(true); auth.clearSession()
  const newRead = auth.hydrateSession(true); first.resolve(guest()); await oldRead
  const same = auth.hydrateSession(true); assert.equal(calls, 2)
  second.reject(new api.ApiError(0, 'NETWORK_ERROR', '读取失败'))
  await assert.rejects(newRead); await assert.rejects(same)
  assert.equal(auth.auth.ready, false); assert.equal(auth.auth.session, null)
})
