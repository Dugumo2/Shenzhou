# 面板同包验收门禁

2026-10-18，SZC-6.1.7.3。契约来自 [关键路径](../../panel/acceptance/critical-paths.json)。本地验收、技术切换、生产产品验收分别记录；本文不批准生产操作。

## 候选与标准入口

显式候选入口自行在项目 `panel/frontend` 执行固定的 `npm run build -- --emptyOutDir --outDir dist`，不需要先重复构建。`--dist` 只允许项目 `panel/frontend/dist`，没有跳过构建选项；失败立即停止，不能接纳旧 dist。构建前后及打包后实际源码指纹必须一致，候选嵌入固定命令、开始/完成时间和源码摘要组成的 `build` 证明。旧版缺此证明的候选不能提升为标准发行。

构建命令包含类型检查，受审脚本固定为 `npm run typecheck && vite build` 与 `vue-tsc --noEmit`，不接受新增 pre/post 生命周期脚本。工具只使用已有 npm 和项目依赖，不自动安装。候选包内 `release-candidate.json` 和包旁 `.manifest.json` 均标记 `UNVERIFIED_CANDIDATE`、`deployable:false`；原 release/frontend manifest 与四份 legacy static 继续保留。

```powershell
python scripts/public/build_panel_release.py --candidate --dist panel/frontend/dist --output staging/regression/candidate.tar.gz
```

浏览器运行器必须解包并运行此候选，通过实际 Django 与编译资源生成 `acceptance.json`。不可根据合同列出空结果后批量填 PASS。主审实际查看报告中的全部截图后记录独立 `visual-review.json`。以下只读命令校验两者：

```powershell
python scripts/public/verify_release_acceptance.py --candidate-archive staging/regression/candidate.tar.gz --acceptance staging/regression/run/acceptance.json --visual-review staging/regression/run/visual-review.json
```

标准入口通过门禁后把被验候选原字节复制到新输出，避免重新打包产生摘要循环；输出及旁文件均禁止覆盖。

```powershell
python scripts/public/build_panel_release.py --candidate-archive staging/regression/candidate.tar.gz --acceptance staging/regression/run/acceptance.json --visual-review staging/regression/run/visual-review.json --output staging/regression/accepted.tar.gz
```

默认入口不提供三项材料就失败；旧 `--dist --output` 命令不能默默产生正式发行。提升后的旁文件为 `ACCEPTED_LOCAL`，保留包清单，同时明确 `deployment_authorized:false`、`production_acceptance:"NOT TESTED"`。归档内候选标识不改，防止破坏同包摘要；部署审核必须携带匹配的提升旁文件与证据，不能仅看 tar 文件名。

## 浏览器回执 v1

顶层字段：

| 字段 | 约束 |
| --- | --- |
| `schema_version`、`producer` | `1`、`shenzhou-browser-regression` |
| `run_id`、`status` | 非空运行标识；所有场景真实通过才为 `PASS` |
| `started_at`、`finished_at` | UTC ISO 时间；候选生成后执行；有效期24小时，未来容差5分钟 |
| `git_commit` | 候选及当前 HEAD 完全一致，提交后需要重新生成并验收 |
| `source_tree_sha256` | 限定实际文件树摘要，包含 dirty 和新增文件，不能只绑定旧 HEAD |
| `contract_sha256` | 合同原始字节摘要 |
| `baseline_manifest_sha256` | 当前 e2e PNG 基线清单摘要 |
| `candidate_archive_sha256` | 被解包运行的候选归档原始摘要 |
| `fixture` | `{path, sha256}`；路径为仓库内 `panel/frontend/e2e` 夹具 |
| `environment` | `application:django-compiled-candidate`、`timezone:Asia/Shanghai`、`locale:zh-CN`、`browser`、真实数字格式 `browser_version`、与依赖锁定版本一致的 `playwright_version`、仅本地 HTTP 的 `base_url` |
| `playwright_report` | 原生Playwright JSON附件 `{path,sha256}`，必须有实际测试，错误、失败、跳过、重试不稳定均为零 |
| `journey` | `{status:PASS,session_id,scenario_ids}`；同一会话顺序完成合同前七项，不靠直接打开路由凑齐 |
| `results` | 每个合同场景/视口恰好一项，缺失、重复、额外、失败、跳过均拒绝 |

每个 `results` 元素为：

```json
{
  "scenario_id": "CP01-login-home",
  "viewport": "desktop",
  "status": "NOT TESTED",
  "assertions": [{"id": "CP01-A01", "status": "NOT TESTED"}],
  "screenshots": [{"checkpoint": "login", "path": "screenshots/login.png", "sha256": "实际摘要"}]
}
```

示例故意保持未测且不完整，不能作为可发行回执。真实输出必须覆盖合同的全部断言和截图 checkpoint；要求网络证据的场景另含 `network_log:{path,sha256}`。附件路径相对回执所在目录；每张 PNG 宽度对应 1440/390，附件字节摘要、PNG结构和CRC均检查。截图不得遮罩图形、数字、日期和关键入口。

源码指纹覆盖后端产品目录、panel/tests、前端 src/tests/e2e、前端根配置/依赖、acceptance 契约、scripts/public 运行器、CI `check.yml` 和 `.gitignore`；不包含运行库、node_modules、dist、报告或生产秘密。e2e 下的 PNG 全部纳入，更新基线也使原候选失效。最终构建后若任何上述文件或 Git HEAD 变化，需要新候选和新浏览器执行；旧CI绿色不能复用。

原生报告必须有非零测试，`errors` 为空，统计中的 unexpected/skipped/flaky 均为零，每个测试预期和实际结果均 passed，且不经过重试；统计数量与实际测试树一致。一个同会话测试可覆盖多项 CP，不强迫把完整旅程拆散为独立测试；CP/视口/断言的完整性由结构化回执另行严格核验。

## 独立视觉审阅 v1

主审必须实际审阅本次报告引用的截图，再保存以下字段，禁止由浏览器运行器自动生成 PASS：

```json
{
  "schema_version": 1,
  "status": "NOT TESTED",
  "reviewer": "实际审阅者",
  "reviewed_at": "实际UTC时间",
  "acceptance_sha256": "完整acceptance.json的字节摘要",
  "candidate_archive_sha256": "候选摘要",
  "screenshot_sha256": ["实际审阅的全部截图摘要，去重"],
  "baseline_change": "none",
  "baseline_before_sha256": "旧基线清单摘要",
  "baseline_after_sha256": "当前baseline_manifest_sha256"
}
```

未改基线时 before/after 必须相同；初次建立或有意更新基线时为 `approved`，还须 `approval_reference` 指向用户需求和审查记录，并提供 `baseline_before_manifest:{path,sha256}` 指向实际旧清单文件，其字节摘要等于 before。旧清单格式为 `{"files":{"仓库相对PNG路径":"sha256"}}`；无旧基线用真实空清单，不能把初次截图偷偷视为既有基线。当前清单采用键排序、紧凑 JSON、尾随换行的 UTF-8 字节求摘要。

门禁用于防止遗漏、错包、错版本和未经审阅的基线替换。它不提供签名信任根，不能阻止持有仓库写权限的人伪造材料；人的实际审阅与CI受控执行仍是必要环节。

## 生产回执与技术切换分开

未来取得生产变更批准后，另存生产只读验收回执，至少包含 `schema_version`、`checked_at`、`release_archive_sha256`、`frontend_manifest_sha256`、`git_commit`、`technical_switch_status`、`product_acceptance_status` 和逐项 `checks`。每项记录检查 ID、页面入口、观察到的构建标识、结果 `PASS/FAIL/BLOCKED/NOT TESTED`、脱敏证据路径。

核对范围包括首页入口、本人服务列表→详情→7天/本月/全年图→连接/指南、相关管理员页、宽窄屏与静态资源版本。技术切换成功不能自动填产品 PASS；本地合成测试也不填生产 PASS。不在此回执保存 Cookie、账号密码、节点URI或供应商凭据。

## 自身验证与界限

```powershell
python -m unittest discover -s panel/tests -p test_release_acceptance.py -v
python scripts/docs/check_documents.py
```

单测用明确的临时合成回执测试门禁正负例，包括默认拒绝、错Git/树/contract/包、缺断言/截图/视口、失败/跳过、过期、基线变更、附件篡改与路径/链接/大小限制。它们只证明校验器行为，不证明产品页面已通过。
