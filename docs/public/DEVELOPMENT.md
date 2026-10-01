# 本地开发与恢复

此处仅用于隔离开发；产品状态见[当前状态](STATUS.md)，目标见[效果方案](../panel/USER_OUTCOME_PLAN_20261001.md)。

## 从仓库运行检查

需要Python 3.13和Git。主依赖版本见 `panel/requirements.txt`；核心实验所需gRPC依赖单独列出，普通Django单测不要求安装核心。

```powershell
git clone https://github.com/Dugumo2/Shenzhou.git
Set-Location Shenzhou
python -m venv panel/.venv
& panel/.venv/Scripts/python.exe -m pip install -r panel/requirements.txt
$env:PANEL_DATA_ROOT = Join-Path $env:TEMP ('shenzhou-dev-' + [guid]::NewGuid())
$env:PANEL_SITE_BRAND = '神舟云'
$env:PANEL_OPERATOR_ENABLED = '0'
$env:PANEL_LIVE = '0'
& panel/.venv/Scripts/python.exe panel/manage.py check
& panel/.venv/Scripts/python.exe panel/manage.py test tests portal --noinput
```

测试使用隔离数据与假身份。不把测试数据库放进生产，不设置真实订阅路径。部分旧测试依赖历史发布脚本或核心，若失败需单列原因，不能跳过后声称整个产品通过。公开文件检查：`python scripts/public/check_public_tree.py`；它核对Git文件清单、禁入内容和公开文档链接，不代替人工保密审查。

## 查看旧候选

如需查看旧模板，可在相同环境变量下执行 `panel/manage.py migrate`、`panel/manage.py createsuperuser`，再执行 `panel/manage.py runserver 127.0.0.1:8000`（均用虚拟环境Python）。仅绑定本机回环；这是被整改的旧界面，不是效果方案已经实现。新前端、生产发行和一键安装尚未提供。

## Git与备份

根目录忽略规则默认排除非公开路径。只暂存本次明确改动的文件，提交前审阅 `git diff --cached`。后续按 `feat/`、`fix/`、`spike/`分支和PR审阅，不强推共同分支。首次上传仅建立基线；分支保护、公开CI、依赖传递锁及完整恢复演练仍待实施，不宣称已配置。

可用 `git bundle create <受限备份路径> --all`备份已提交代码；恢复时clone仓库或bundle。数据库、账本、运行配置、令牌和私有研究不在此备份中，需独立加密与恢复演练。代码回滚不能恢复旧凭据，也不能覆盖新账本。

原工作区还维护私有需求审计、研究报告、上下文接续与SSH入口；它们没有丢失，只是不公开。公开协作所需产品目标集中在效果方案，实施进展集中在STATUS，不能从旧测试数量推断交付完成。
