import test from 'node:test'
import assert from 'node:assert/strict'
import { effectScope, reactive, watch } from 'vue'
import type { Session } from '../src/types.ts'
import { useSessionIdentity } from '../src/useSessionIdentity.ts'

const session = (): Session => ({
  authenticated: true,
  user: { username: 'synthetic-user', is_staff: false },
  csrf_token: 'synthetic-csrf-one', brand: '神舟云', time_zone: 'Asia/Shanghai',
  password_policy: { min_length: 12, messages: [], validated_by_server: true },
})

test('账号、角色与登录状态变更同步改变身份，无需等待下次渲染', () => {
  const auth = reactive({ session: session() as Session | null })
  const scope = effectScope()
  const identity = scope.run(() => useSessionIdentity(() => auth.session))!
  let previous = identity.value
  for (const change of [
    () => { auth.session!.user!.username = 'another-user' },
    () => { auth.session!.user!.is_staff = true },
    () => { auth.session!.authenticated = false },
    () => { auth.session!.user = null },
    () => { auth.session = null },
  ]) {
    change()
    assert.notEqual(identity.value, previous)
    assert.match(identity.value, /^session:\d+$/)
    previous = identity.value
  }
  scope.stop()
})

test('同账号同角色会话替换及原对象令牌轮换均失效，返回值不包含秘密或用户名', () => {
  const auth = reactive({ session: session() })
  const scope = effectScope()
  const identity = scope.run(() => useSessionIdentity(() => auth.session))!
  let previous = identity.value
  auth.session = session()
  assert.notEqual(identity.value, previous)
  previous = identity.value
  auth.session.csrf_token = 'synthetic-csrf-two'
  assert.notEqual(identity.value, previous)
  assert.match(identity.value, /^session:\d+$/)
  assert.doesNotMatch(identity.value, /synthetic|user|csrf|cookie/i)
  previous = identity.value
  auth.session.csrf_token = 'synthetic-csrf-two'
  auth.session.brand = '仅标题变化'
  assert.equal(identity.value, previous)
  scope.stop()
})

test('同步下游监听立即收到新身份，作用域销毁后停止递增', () => {
  const auth = reactive({ session: session() })
  const scope = effectScope()
  let resets = 0
  const identity = scope.run(() => {
    const value = useSessionIdentity(() => auth.session)
    watch(value, () => { resets++ }, { flush: 'sync' })
    return value
  })!
  auth.session.user!.is_staff = true
  assert.equal(resets, 1)
  const previous = identity.value
  scope.stop()
  auth.session = session()
  assert.equal(identity.value, previous)
  assert.equal(resets, 1)
})
