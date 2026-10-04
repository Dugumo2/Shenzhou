export interface P8Resource {
  key: string; label: string; download_url: string; verification: 'http_format_verified'
}
export interface P8Delivery {
  service_id: string; client_id: string; state: 'available' | 'blocked' | 'unsupported'
  message: string; resources: P8Resource[]; runtime_acceptance: 'not_tested'
}
export interface P8ConnectionStep { title: string; description: string; keys: string[] }

// 只列用户需要在软件中导入的内容，不展示生成器素材包。
export function p8ConnectionSteps(clientId: string): P8ConnectionStep[] {
  if (clientId === 'android') return [
    { title: '添加远程配置', description: '在 SFA 配置页添加“远程 / Remote”配置，填入完整配置地址，保存并更新。节点、DNS 和分流包含在配置中，无需分别导入。', keys: ['android'] },
    { title: '应用与检查', description: '停止其他 VPN 客户端，选择这份配置并启动。以后更新原来的远程配置，再停止并启动服务；在手机上检查实际连接和规则加载结果。', keys: [] },
  ]
  if (clientId === 'v2rayng') return [
    { title: '导入节点订阅', description: '在 v2rayNG“订阅分组设置”中添加地址，保存后回到主页更新该订阅。已有分组时更新原项。', keys: ['v2rayng'] },
    { title: '下载名单资源', description: '先备份旧文件。在“资源文件 → 添加 → 添加链接”登记以下地址，别名按这份路由实际引用的文件名填写，再执行“下载文件”。逐项确认下载成功，失败时保留原文件。', keys: ['v2rayng-geosite', 'v2rayng-geoip'] },
    { title: '导入路由规则', description: '下载路由文件，复制从 [ 到 ] 的完整 JSON 内容，在“路由设置 → 从剪贴板导入规则集”导入。此处粘贴文件内容，不是下载地址；核对规则顺序与名单资源引用。', keys: ['v2rayng-routes'] },
    { title: '应用与检查', description: '按服务指南核对 DNS，停止其他 VPN 客户端，再启动所选节点与路由。节点、名单和路由各用原生入口更新；下载成功不能代替手机上的连接与分流检查。', keys: [] },
  ]
  if (clientId === 'windows') return [
    { title: '导入节点订阅', description: '在 v2rayN“订阅分组设置”登记订阅地址，保存并更新原分组。', keys: ['windows'] },
    { title: '更新原生路由', description: '在“设置 → 路由设置”打开对应规则集，将原生路由地址填入“可选地址（Url）”并更新。素材包不能代替原生路由。', keys: ['windows-routing'] },
    { title: '应用与检查', description: '保存路由设置，重启客户端服务，核对活动节点、路由、DNS 与实际连接。软件菜单以当前版本及下方指南为准。', keys: [] },
  ]
  return []
}

export function verifiedP8Resources(value: P8Delivery | null, serviceId: string, clientId: string): P8Resource[] {
  if (!value || value.service_id !== serviceId || value.client_id !== clientId || value.state !== 'available') return []
  const keys = p8ConnectionSteps(clientId).flatMap(step => step.keys)
  if (!keys.length || !Array.isArray(value.resources)) return []
  const matched: P8Resource[] = []
  for (const key of keys) {
    const found = value.resources.filter(resource => resource.key === key)
    if (found.length !== 1 || found[0].verification !== 'http_format_verified') return []
    try {
      const url = new URL(found[0].download_url)
      if (url.protocol !== 'https:' || url.username || url.password || url.hash) return []
    } catch { return [] }
    matched.push(found[0])
  }
  return matched
}
