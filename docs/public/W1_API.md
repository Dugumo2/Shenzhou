# 首批真实接口与范围

2026-10-01。此文描述本地候选接口；不代表生产旧资源已迁入或核心限额能力通过。

所有地址以 `/api/v1` 开头，无尾斜杠。会话沿用 Django Session；写请求使用同源 Cookie 和 `X-CSRFToken`，不另发浏览器长期访问令牌。成功为 `data` 加 `error: null`，失败为 `data: null` 加 `error`（错误码、中文说明及可选字段错误）。字节数使用十进制字符串；未知数值为 `null`。

| 接口 | 行为 |
| --- | --- |
| GET `/session` | 登录状态、CSRF、用户角色、候选环境标识 |
| POST `/login`、`/logout` | 真实登录与退出，会话与CSRF校验 |
| GET `/me/services`、`/me/services/{id}` | 本人服务及只读明细；无服务账号不自动开通 |
| GET `/catalog/clients`、`/catalog/clients/{id}/guide` | 登录后的客户端目录与指南核验状态 |
| GET `/admin/services` | 管理员搜索、状态筛选及分页；每份服务一行 |
| GET `/admin/rules` | 数据库规则候选及来源索引，不冒充生产已发布版本 |
| GET `/admin/services/{id}/billing` | 已保存活动账期和独立重置计划；纯读取，不建账期 |
| POST `/admin/services/{id}/billing/preview` | 预览下一次重置时间，显示之后三个自然月；纯读取 |
| PATCH `/admin/services/{id}/billing` | 确认保存独立计划；权限、修订、短时预览及幂等校验 |

账期预览传 `next_reset_at` 与 `expected_billing_revision`；保存再传 `preview_token` 与 `idempotency_key`。未来时间精确到分钟，本地日期按上海解释。保存保留本期已用量、额度、到期、历史账与接入配置修订，不等于立即重置流量或已执行核心变更。计划推进只用服务端当前时间，查询历史样本不能创建账期。

旧两套授权链尚未确认映射时，不凭同一账号自动合并或增加额度。未验证交付不返回虚构链接；旧用量未知、采样过期或缺口时如实显示。当前不支持的新套餐、按节点倍率、完整重置凭据、多机执行仍按阶段验收，不能从此接口表推断已具备。

回归包括普通用户越权、对象归属、CSRF、只读无建期副作用、大数字节、月底自然月、幂等冲突、已入账边界与跨期样本。SQLite验证不是最终生产数据库行锁证明。真实下载、实际客户端菜单、线上执行及Linux核心能力分别报告。
