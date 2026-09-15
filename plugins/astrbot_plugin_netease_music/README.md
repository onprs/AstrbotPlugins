# 网易云临时登录与歌单分析

AstrBot 插件，通过二维码临时登录网易云账号，展示歌单，并使用当前会话配置的 LLM 分析指定歌单。

## 指令

- `/网易云 临时登录`：发送登录二维码并等待扫码。
- `/网易云 临时登录确认 yes`：确认扫码后显示的网易云账号属于自己，并完成绑定。
- `/网易云 临时登录确认 no`：拒绝绑定并注销本次临时会话。
- `/网易云 歌单list`：展示绑定账号的全部歌单，并分配从 1 开始的索引。
- `/网易云 歌单分析 <id>`：使用当前 LLM 分析最近一次歌单列表中的对应索引。

目前仅支持群聊中的 OneBot v11（`aiocqhttp`）平台。

## 配置

在 AstrBot WebUI 的插件配置中可以编辑“歌单分析提示词”，用于设置分析侧重点、输出结构和表达风格。留空时使用插件默认的五部分分析要求。自定义提示词会与歌单全量统计及曲目样本一起发送给当前会话配置的 LLM，但不会替换插件用于约束事实依据和敏感推断的系统提示。

## 网易云 API

插件默认连接 `http://127.0.0.1:3010`，需要单独运行 [NeteaseCloudMusicApi Enhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)。线上部署固定使用 `@neteasecloudmusicapienhanced/api@4.40.1`，并只监听本机地址。目标服务器使用的 systemd 单元位于 `deploy/netease-cloud-music-api.service`；其中 Node 路径固定为该服务器的 `v24.18.0` 独立只读副本，迁移服务器时需要同步调整。

二维码登录使用以下接口：

- `/login/qr/key`
- `/login/qr/create`
- `/login/qr/check`
- `/login/status`
- `/logout`

歌单功能使用 `/user/playlist`、`/playlist/detail` 和 `/playlist/track/all`。

## 数据与隐私

扫码成功后，网易云 Cookie 先只保存在插件进程内存中。只有发起扫码的 AstrBot 用户执行 `临时登录确认 yes` 后，插件才会将账号 UID、昵称和 Cookie 写入 AstrBot 插件 KV 存储。`no`、二维码过期或确认超时都会清理临时 Cookie，并尝试调用网易云注销接口。

API 客户端禁用共享 Cookie Jar，每个 AstrBot 用户的 Cookie 都只通过对应请求体传递。不要将 `api_base_url` 配置为不受信任的公共服务，否则登录 Cookie 会发送给该服务。

## 消息行为

插件通过原始 `AstrMessageEvent` 发送结果，保留当前 OneBot 连接的 `self_id` 路由；发送成功后再通过 AstrBot 消息历史管理器以 bot 角色写入群消息记录。命令和可读结果也会写入当前 AstrBot LLM 会话上下文。插件会识别并停止自己输出的 OneBot 回推事件，防止重复入库或进入群聊主动回复抽样；命令本身是唤醒消息，同样不会触发主动回复。

## 分析范围

插件读取歌单全部歌曲并计算歌手、专辑、发行年份和时长等全量统计。超大歌单会在曲目明细部分均匀抽样，同时限制包含自定义分析提示词在内的请求总长度，以免超过当前模型上下文窗口。

## 依赖

```text
aiohttp>=3.10,<4
```

插件版本要求 AstrBot `>=4.27.2,<5`。
