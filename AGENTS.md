# 仓库规范

## 目录

- `plugins/`：存放本仓库维护的所有 AstrBot 插件；新插件目录使用 `astrbot_plugin_<name>` 命名。
- `reference/AstrBot/`：AstrBot 官方仓库，只作开发参考，不纳入根仓库版本控制，不在其中开发或提交改动。
- `plan/`：存放开发计划。

## 官方参考

1. 开始插件开发前执行 `git -C reference/AstrBot pull --ff-only` 更新官方源码。
2. 先阅读 `reference/AstrBot/docs/zh/dev/star/plugin-new.md`，再按功能阅读其链接的 `guides/` 文档。
3. API 用法以 `reference/AstrBot/astrbot/api/`、`reference/AstrBot/astrbot/core/` 和 `reference/AstrBot/tests/` 的当前实现为准；旧插件代码仅用于理解业务，不作为兼容性依据。
4. 每个插件必须位于 `plugins/` 下，并维护准确的 `metadata.yaml`、`README.md`、`main.py`；有第三方依赖时维护 `requirements.txt`，有配置时维护 `_conf_schema.json`。
5. 插件网络请求使用异步客户端；持久化数据写入 AstrBot `data` 目录；提交前完成针对性测试和 Ruff 检查。

## 计划

1. 计划文件统一命名为 `plan/YYYY-MM-DD-HHmm.md`，使用本地 24 小时时间，不创建其他命名格式的计划文件。
2. 开始任务前按文件名顺序读取 `plan/` 中与任务相关的计划，以最新计划为当前依据。
3. 计划只记录目标、任务、验证和状态；执行过程中同步更新当前计划，不另写重复计划。

## 部署

- 目标服务器固定为 SSH 主机 `astrbot_beijing`；除非用户明确指定，不得部署到其他服务器。
