import test from 'node:test'
import assert from 'node:assert/strict'
import { formatGB, integerBytes, previewMatches, shanghaiInput, shiftLabel, usagePercent } from '../src/display.ts'

test('十进制1000GB不会被显示为GiB；超过JS安全整数仍精确处理', () => {
  assert.equal(formatGB('1000000000000'), '1,000 GB')
  assert.equal(integerBytes('9007199254740993'), 9007199254740993n)
  assert.equal(formatGB('30000000000'), '30 GB')
  assert.equal(formatGB('1234000000'), '1.23 GB')
})
test('未知、坏数据和不安全JSON整数不伪装为零或可信用量', () => {
  assert.equal(formatGB(null), '暂不可确认')
  assert.equal(formatGB(null, '待核算'), '待核算')
  assert.equal(formatGB(Number.MAX_SAFE_INTEGER + 1), '暂不可确认')
  assert.equal(formatGB('-1'), '暂不可确认')
  assert.equal(usagePercent(null, '100000000000'), null)
  assert.equal(formatGB('0'), '0 GB')
})
test('浏览器任意本地时区下都用上海日期和分钟，不丢月末锚点', () => {
  assert.equal(shanghaiInput('2028-02-29T11:27:00Z'), '2028-02-29T19:27')
  assert.equal(shanghaiInput('2026-10-31T16:00:00Z'), '2026-11-01T00:00')
  assert.equal(shanghaiInput(null), '')
})
test('预览绑定输入且到期失效，修改输入不可确认旧预览', () => {
  const now = Date.parse('2026-10-01T10:00:00Z')
  assert.equal(previewMatches('2026-11-01T09:00', '2026-11-01T09:00', '2026-10-01T10:10:00Z', now), true)
  assert.equal(previewMatches('2026-11-02T09:00', '2026-11-01T09:00', '2026-10-01T10:10:00Z', now), false)
  assert.equal(previewMatches('a', 'a', '2026-10-01T10:00:00Z', now), false)
  assert.equal(previewMatches('a', 'a', '无效时间', now), false)
  assert.equal(shiftLabel(-17 * 86400), '提前17天')
})
