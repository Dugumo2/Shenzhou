export type NavigationIcon = 'overview' | 'users' | 'services' | 'rules' | 'servers' | 'lines' | 'guide' | 'account' | 'return'
export type PageTemplate = 'list' | 'detail' | 'operation' | 'import'
export interface NavigationItem {
  path: string
  label: string
  icon: NavigationIcon
}
export interface NavigationGroup {
  id: string
  label: string
  enabled: boolean
  items: readonly NavigationItem[]
}
export interface BreadcrumbItem { label: string; path?: string }

// 导航只有一个来源；尚未接入的运维槽位不生成路由或可点击页面。
export const adminNavigation: readonly NavigationGroup[] = [
  { id: 'business', label: '业务管理', enabled: true, items: [
    { path: '/admin/overview', label: '管理总览', icon: 'overview' },
    { path: '/admin/users', label: '用户管理', icon: 'users' },
    { path: '/admin/services', label: '订阅管理', icon: 'services' },
  ] },
  { id: 'resources', label: '连接资源', enabled: true, items: [
    { path: '/admin/rules', label: '代理规则', icon: 'rules' },
    { path: '/admin/servers', label: '服务器', icon: 'servers' },
    { path: '/admin/lines', label: '线路', icon: 'lines' },
  ] },
  { id: 'operations', label: '运维', enabled: false, items: [] },
]
export const userNavigation: readonly NavigationGroup[] = [
  { id: 'personal', label: '我的空间', enabled: true, items: [
    { path: '/services', label: '我的服务', icon: 'services' },
    { path: '/guides', label: '使用指南', icon: 'guide' },
    { path: '/account', label: '账号设置', icon: 'account' },
  ] },
]

// 详情更深层级使用页内页签，未实现部分只保留框架约定。
export const pageSections = {
  rules: ['自建规则', '规则来源', '组合方案', '命中检查'],
  server: ['概览', '核心与端点', '检测', '流量与账单', '备份'],
  line: ['路径', '能力与验证', '关联授权'],
} as const

export function navigationForWorkspace(admin: boolean): readonly NavigationGroup[] {
  return (admin ? adminNavigation : userNavigation).filter(group => group.enabled)
}
export function isNavigationActive(path: string, target: string): boolean {
  if (target === '/admin/rules' && path.startsWith('/admin/rule-policies')) return true
  return path === target || path.startsWith(target + '/')
}
export function breadcrumbsForPath(path: string): BreadcrumbItem[] {
  if (path.startsWith('/admin/rule-policies')) return [{ label: '代理规则', path: '/admin/rules' }, { label: '组合方案' }]
  const groups = path === '/admin' || path.startsWith('/admin/') ? adminNavigation : userNavigation
  const item = groups.flatMap(group => group.items).find(candidate => isNavigationActive(path, candidate.path))
  if (!item) return [{ label: '神舟云' }]
  if (path === item.path) return [{ label: item.label }]
  if (path === '/admin/rules/sources') return [{ label: item.label, path: item.path }, { label: '规则来源' }]
  if (item.path === '/admin/users') return [{ label: item.label, path: item.path }, { label: '用户详情' }]
  if (item.path === '/services') return [{ label: item.label, path: item.path }, { label: '服务详情' }]
  return [{ label: item.label }]
}
export function pageTemplateForPath(path: string): PageTemplate {
  if (path === '/admin/rules/sources') return 'import'
  if (path.startsWith('/services/') || path.startsWith('/admin/users/')) return 'detail'
  if (path === '/account' || path === '/guides' || path === '/admin/overview') return 'detail'
  return 'list'
}
