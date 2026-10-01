# 本地开发与恢复

此处仅用于隔离开发；产品状态见[当前状态](STATUS.md)，目标见[效果方案](../panel/USER_OUTCOME_PLAN_20261001.md)。

## 从仓库运行检查

需要Python 3.13和Git。主依赖版本见 `panel/requirements-lock.txt`；核心实验所需gRPC依赖单独列出，普通Django单测不要求安装核心。

```powershell
git clone https://github.com/Dugumo2/Shenzhou.git
Set-Location Shenzhou
python -m venv panel/.venv
& panel/.venv/Scripts/python.exe -m pip install --require-hashes -r panel/requirements-lock.txt
$env:PANEL_DATA_ROOT = Join-Path $env:TEMP ('shenzhou-dev-' + [guid]::NewGuid())
$env:PANEL_SITE_BRAND = '神舟云'
$env:PANEL_OPERATOR_ENABLED = '0'
$env:PANEL_LIVE = '0'
& panel/.venv/Scripts/python.exe panel/manage.py check
& panel/.venv/Scripts/python.exe scripts/public/test_panel.py
```

测试使用隔离数据与假身份。不把测试数据库放进生产，不设置真实订阅路径。当前公开测试入口运行398项独立测试，另有14项 `tests.test_production` 依赖未公开现场脚本，报告为NOT TESTED，绝不计入公开通过数。原工作区全套317项测试已通过；干净恢复目录曾实测该14项缺少源脚本而失败，不能声称全套从仓库可复现。可用原始命令 `panel/manage.py test tests portal --noinput` 复现此限制。公开文件检查：`python scripts/public/check_public_tree.py`；它核对Git文件清单、禁入内容和公开文档链接，不代替人工保密审查。

## 启动同源新候选

需要 Node 22.18 或更高的兼容版本。先在 `panel/frontend` 执行 `npm ci --ignore-scripts`、`npm test`、`npm run build`，再从仓库根执行：

```powershell
& panel/.venv/Scripts/python.exe scripts/public/run_candidate.py --create-admin
```

入口为 `http://127.0.0.1:18765/app/`，数据单独保存在本项目 `staging/local-candidate`。首次交互创建管理员，以后去掉 `--create-admin`。普通账号和管理员均不会自动获得服务；本命令不复制真实旧订阅或规则，不启动代理核心，也不更改现网。

前端由同一个候选Django服务提供构建资源与API。`/app/`文件服务仅允许显式本地候选；生产需由部署服务器提供构建静态文件，此版本没有自动上线功能。接口与限制见[首批接口说明](W1_API.md)。

旧模板仍作为兼容路径保留；新候选根地址直接转到`/app/`，不用把旧18763或静态原型18764当成新版入口。

## 查看旧候选

如需查看旧模板，可在相同环境变量下执行 `panel/manage.py migrate`、`panel/manage.py createsuperuser`，再执行 `panel/manage.py runserver 127.0.0.1:8000`（均用虚拟环境Python）。仅绑定本机回环；这是被整改的旧界面，不是效果方案已经实现。新前端通过上面的同源候选入口查看；生产发行和一键安装尚未提供。

## Git与备份

根目录忽略规则默认排除非公开路径。只暂存本次明确改动的文件，提交前审阅 `git diff --cached`。后续新建分支采用 `codex/` 前缀并通过PR审阅（当前已存在的功能分支保留），不强推共同分支。首次上传仅建立基线；分支保护和完整加密数据恢复演练仍待实施。仓库CI分别检查公开后端及前端构建，不部署生产；远端运行结果须在对应提交核对。

可用 `git bundle create <受限备份路径> --all`备份已提交代码；恢复时clone仓库或bundle。数据库、账本、运行配置、令牌和私有研究不在此备份中，需独立加密与恢复演练。代码回滚不能恢复旧凭据，也不能覆盖新账本。

原工作区还维护私有需求审计、研究报告、上下文接续与SSH入口；它们没有丢失，只是不公开。公开协作所需产品目标集中在效果方案，实施进展集中在STATUS，不能从旧测试数量推断交付完成。
