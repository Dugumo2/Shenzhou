# N3隔离兼容合同

portal/compatibility.py是待现场源核对后的兼容逻辑准备，**没有数据库适配、HTTP下载端点或生产凭据签发**。CompatibilityDecision仅返回synthetic_only结果及资源元数据，不能作为现网授权依据。原有下载门禁没有修改。

SourceKey由来源实例、类型、稳定ID组成。核验绑定指定owner/service、证据摘要和源修订；每次解析重读账号、当前核验管理员、服务、源、授权及别名，并复核仓储快照未变化。管理员降权、停用或核验修订漂移使绑定失效。

三类来源分别处理：Membership保留原eligible的已应用、measured、新鲜计量、额度/到期条件；DeviceSubscription保留既有下载条件、活动账期和全部入口身份集合；独立P8已发布资产没有个人套餐计量时仍为unknown，只作为existing_compatibility，不能转成新商业套餐。

版本含义分开：

| 字段 | 含义 |
| --- | --- |
| download_generation | 新根及旧别名的下载授权代际 |
| identity_generation | 实际节点身份代际 |
| source_credential_version | 源解析器的凭据版本，例如原token_version；不是token正文 |
| release_id / release_version | 产物发布版本 |

测试使用下载3、身份13、源凭据17、发布23，互不要求同值；分别一致才相容。发布新文件不必轮换下载声明，重置下载不自动轮换节点身份。上述事实仍须由未来真实适配器从权威源取得。

根、别名、内嵌引用走同一校验；资源采用封闭的synthetic JSON格式，核对全部声明引用、白名单、规范相对路径、同代manifest和内容摘要。真实客户端格式解析器尚未接入，不能声称已验证其所有内嵌URL。

2026-10-04有66项隔离反例及独立复核，覆盖越权/重复映射/错代/撤销/版本漂移/未知计量/路径编码/内容损坏/权限变化。真实数据、旧链接、核心计量与客户端仍需要独立现场验收。
