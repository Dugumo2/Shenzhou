import type { DeliveryResource } from './delivery-types'

export interface ConnectionStep { title: string; description: string; resource?: DeliveryResource; downloadLabel?: string }

// 用户只选择软件需要的操作；生成器内部来源文件不进入导入向导。
export function connectionSteps(clientId: string, resources: DeliveryResource[]): ConnectionStep[] {
  const subscription = resources.find(item => item.key === 'subscription')
  if (clientId === 'android') return [
    { title: '导入完整配置', description: 'SFA 的节点与分流规则包含在完整配置中，无需分别导入规则文件。', resource: subscription, downloadLabel: '下载演示配置' },
    { title: '应用与检查', description: '正式订阅导入后，选择该配置并启动服务。本地演示节点不可连接，本次只能预览配置文件。' },
  ]
  if (clientId === 'windows' || clientId === 'v2rayng') return [
    { title: '导入节点订阅', description: '节点订阅用于添加这份服务允许使用的节点。', resource: subscription, downloadLabel: '下载演示节点' },
    { title: '设置分流规则', description: '节点订阅和路由规则需要分别导入。具体位置请查看下方对应软件的指南。', resource: resources.find(item => item.key === 'routing'), downloadLabel: '下载演示路由' },
    { title: '应用与检查', description: '正式资源导入后，选择相应节点与路由并重启服务。本地演示节点不可连接，不能用于实际联网。' },
  ]
  return []
}
