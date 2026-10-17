# 关键路径验收契约

`critical-paths.json` 是浏览器验收与标准发行证据校验共用的权威清单，schema_version=1。它包含11个场景、必要断言、视口、截图检查点、证据类型和当前源码引用，不保存账号、令牌、实际用户数据或执行结果。

发行接线见 [同包验收门禁](../../docs/public/release-acceptance.md)。执行器读取 `required_scenarios`，按场景所需视口执行每条断言；验证器拒绝缺场景、缺断言、缺视口/截图、skip及非PASS。证据应绑定此文件实际SHA-256和被验收发行包，不把一份旧报告复用到新包。

正常路径必须在真实Django与编译页面运行。已有 `data-chart=quota-gauge`、`transfer-composition`、`usage-timeseries` 是图形定位入口；它们只被源码包含不算通过。至少一条会话从登录沿可见导航走完整路径。

修改场景/断言需关联现行需求、同步当前Implementation Plan并通知所有消费者；不得为了消除失败自动删断言、跳过、放宽容差或更新截图。`undelivered_scope` 记录本批不能宣称完成的长期要求，不是可以忽略的需求，也不是本批假PASS占位。
