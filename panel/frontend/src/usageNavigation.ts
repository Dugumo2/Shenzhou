import type { CalendarPeriod } from './usage-types'

// 只保存最近选择，不保存账号名称、凭据或用量；与全局会话代际同步清理。
const selections = new Map<string, CalendarPeriod>()
const key = (identity: string, service: string) => JSON.stringify([identity, service])
export const usageNavigation = {
  get(identity: string, service: string): CalendarPeriod { return selections.get(key(identity, service)) || '7d' },
  set(identity: string, service: string, period: CalendarPeriod) {
    const id = key(identity, service)
    selections.delete(id); selections.set(id, period)
    while (selections.size > 24) selections.delete(selections.keys().next().value!)
  },
  clearService(identity: string, service: string) { selections.delete(key(identity, service)) },
  clear() { selections.clear() },
}
