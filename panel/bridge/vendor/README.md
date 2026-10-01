# 权威生成器复用

2026-09-30 从本项目现有 `staging/p8-client/client_bundle.py` 和
`staging/p8-client/p7_build_profiles.py` 复制。前者仅将 `p7_build_profiles`
导入改为包内相对导入；函数中的路由、DNS、Geo 编码和来源校验保持原逻辑。

`bridge.client_delivery` 仅复用纯函数。不调用旧 CLI、文件输出函数、默认核心
路径或固定八节点生成入口；运行不依赖 staging 目录。选择单份订阅时只替换
节点集合、节点显示名、自动组、节点域名 bootstrap 列表和隔离缓存名。

历史内部 Geo 标签/文件名保留，以免破坏已验客户端引用；它们不是产品名称。
品牌显示来自新界面。完整规则中心及新规则策略另行实施。

本文件来源为当前自有项目；此复制未引入研究仓库中的第三方代码。

原件 SHA-256：

- client_bundle.py：`bf2496f3237a04e87d0e803e7f7471f03cf212865d52d5e7043abed7c7bc3e0f`
- p7_build_profiles.py：`210f998fafafd4de8e3988cb870b1c7ca9372d19dad7331274d9bb3c38ee7751`

公开基线修订：2026-10-01将历史固定域名/IP替换为保留示例值。上述SHA仅代表原始复制来源，不是当前文件摘要；严格来源检查保持，示例配置不能用于生产。
