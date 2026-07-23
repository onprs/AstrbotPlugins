# AstrBot 多服务器性能监控面板

`astrbot_plugin_server_monitor` 是一个面向 OneBot v11(aiocqhttp) 的 AstrBot 插件。它会从多台服务器的 HTTP 探针采集指标，渲染成商业高级 SaaS 暗色风格的自适应图片面板，并通过 OneBot v11 原生 `send_group_msg` / `send_private_msg` 发送图片消息。

## 功能

- CPU 使用率、核心数、CPU 型号
- 内存使用情况
- 存储空间使用情况，支持多挂载点汇总
- 地理位置
- 系统版本和内核
- 入网 / 出网速率
- 磁盘读 / 写速率
- 备注名
- 图片内字段中文展示：在线、离线、位置、系统、运行时长、内存、存储、入网、出网、磁盘读取、磁盘写入
- 多服务器自适应布局：1 台单列，2-4 台双列，5 台及以上三列，图片高度随行数自动扩展
- 本地 Pillow 高精度渲染：默认 2x 超采样后压回目标尺寸，卡片采用暗色玻璃质感；左上角品牌为 `Onprs`，右下角署名为 `made by onprs`
- 探针异常降级检测：HTTP 503/timeout 等临时故障会先重试；如果 SSH/TCP 端口仍可达，图片显示“探针异常”而不是“离线”
- 手动发送和定时自动推送

## 安装插件

在 AstrBot WebUI 的插件管理里上传 zip 安装包即可。zip 内部结构应为：

```text
astrbot_plugin_server_monitor/
  main.py
  metadata.yaml
  _conf_schema.json
  requirements.txt
  README.md
  probe/
    linux_probe.py
    install_probe.sh
    astrbot-server-probe.service
```

## 配置服务器

推荐在插件配置面板的 `servers` 列表里逐台添加：

```json
{
  "name": "edge-01",
  "endpoint": "http://10.0.0.11:9810/metrics",
  "token": "change-me",
  "location": "Singapore",
  "remark": "API ingress",
  "headers": "{}"
}
```

如果当前 AstrBot 版本的 `template_list` 编辑不方便，可以把同样的数组写进 `servers_json`：

```json
[
  {
    "name": "edge-01",
    "endpoint": "http://10.0.0.11:9810/metrics",
    "token": "change-me",
    "location": "Singapore",
    "remark": "API ingress"
  },
  {
    "name": "db-01",
    "endpoint": "http://10.0.0.12:9810/metrics",
    "token": "change-me",
    "location": "Tokyo",
    "remark": "PostgreSQL"
  }
]
```

`token` 不为空时，插件请求会自动带上：

```text
Authorization: Bearer <token>
```

显示信息优先级：

- `servers[].name`、`servers[].location`、`servers[].remark` 会优先按 AstrBot 插件配置展示。
- 探针返回的 `hostname`、`location`、`remark` 只在对应配置留空时作为兜底。
- 如果你直接编辑 AstrBot 的 `servers` 原始配置，`template_list` 每项通常还需要 `__template_key: "server"`；如果写在 `servers_json`，保持普通 JSON 数组即可。

## Linux 探针部署

这里的关键是：AstrBot 插件本身只负责“拉取和渲染”，每台被监控服务器都要先启动一个只读 HTTP 探针。你需要把 `probe/` 目录里的文件复制到每台服务器，然后在每台服务器上安装成 systemd 服务。

假设有 3 台服务器：

```text
edge-01  10.0.0.11  Singapore  API ingress
db-01    10.0.0.12  Tokyo      PostgreSQL
cache-01 10.0.0.13  Hong Kong  Redis
```

### 1. 取出 probe 目录

在 AstrBot 插件安装包里有这个目录：

```text
astrbot_plugin_server_monitor/probe/
  linux_probe.py
  install_probe.sh
  astrbot-server-probe.service
```

如果你手里只有 zip，可以先解压：

```bash
unzip astrbot_plugin_server_monitor-1.0.7.zip
cd astrbot_plugin_server_monitor/probe
```

如果插件已经装进 Linux 端 AstrBot，通常也可以从 AstrBot 插件目录里找到：

```bash
cd /AstrBot/data/plugins/astrbot_plugin_server_monitor/probe
```

实际路径以你的 AstrBot 部署目录为准。

### 2. 在单台服务器上安装探针

先把 `probe/` 目录复制到目标服务器。下面假设要安装到 `10.0.0.11`：

```bash
scp linux_probe.py install_probe.sh root@10.0.0.11:/tmp/
```

然后 SSH 到这台服务器执行安装：

```bash
ssh root@10.0.0.11
cd /tmp
sudo bash install_probe.sh \
  --token 'change-me-long-random-token' \
  --location 'Singapore' \
  --remark 'API ingress' \
  --port 9810
```

安装脚本会自动完成这些事：

```text
1. 创建 /opt/astrbot-server-probe/
2. 复制 linux_probe.py 到 /opt/astrbot-server-probe/linux_probe.py
3. 生成 /etc/systemd/system/astrbot-server-probe.service
4. 执行 systemctl daemon-reload
5. 执行 systemctl enable --now astrbot-server-probe
6. 输出本机测试 curl 命令
```

确认服务正在运行：

```bash
systemctl status astrbot-server-probe --no-pager
```

在这台服务器本机测试：

```bash
curl -H 'Authorization: Bearer change-me-long-random-token' \
  http://127.0.0.1:9810/metrics
```

在 AstrBot 所在机器上测试能否访问这台服务器：

```bash
curl -H 'Authorization: Bearer change-me-long-random-token' \
  http://10.0.0.11:9810/metrics
```

只有 AstrBot 所在机器能 curl 通，插件才拉得到这台服务器的数据。

### 3. 批量安装到多台服务器

如果你有多台服务器，可以在本机准备一个列表，然后循环复制和安装。下面示例使用同一个 token，实际生产建议用一个足够长的随机 token：

```bash
TOKEN='change-me-long-random-token'

cat > servers.txt <<'EOF'
edge-01 10.0.0.11 Singapore API-ingress
db-01 10.0.0.12 Tokyo PostgreSQL
cache-01 10.0.0.13 Hong-Kong Redis
EOF

while read -r NAME IP LOCATION REMARK; do
  echo "Installing probe on $NAME ($IP)..."
  scp linux_probe.py install_probe.sh root@"$IP":/tmp/
  ssh root@"$IP" "sudo bash /tmp/install_probe.sh \
    --token '$TOKEN' \
    --location '$LOCATION' \
    --remark '$REMARK' \
    --port 9810"
done < servers.txt
```

如果服务器的 SSH 用户不是 root，比如是 `ubuntu`，可以改成：

```bash
scp linux_probe.py install_probe.sh ubuntu@"$IP":/tmp/
ssh ubuntu@"$IP" "sudo bash /tmp/install_probe.sh --token '$TOKEN' --location '$LOCATION' --remark '$REMARK' --port 9810"
```

### 4. 放通网络访问

探针默认监听：

```text
0.0.0.0:9810
```

你需要保证 AstrBot 所在机器能访问每台服务器的 `9810` 端口。

如果服务器有防火墙，可以只允许 AstrBot 机器访问，例如 AstrBot 机器 IP 是 `10.0.0.5`：

```bash
sudo ufw allow from 10.0.0.5 to any port 9810 proto tcp
```

如果使用云服务器安全组，也只放行 AstrBot 机器公网 IP 或内网 IP 到 `9810/tcp`。不要把探针端口无鉴权暴露到公网。

### 5. 在 AstrBot 插件里填写服务器列表

探针装好后，在 AstrBot 插件配置里写：

```json
[
  {
    "name": "edge-01",
    "endpoint": "http://10.0.0.11:9810/metrics",
    "token": "change-me-long-random-token",
    "location": "Singapore",
    "remark": "API ingress"
  },
  {
    "name": "db-01",
    "endpoint": "http://10.0.0.12:9810/metrics",
    "token": "change-me-long-random-token",
    "location": "Tokyo",
    "remark": "PostgreSQL"
  },
  {
    "name": "cache-01",
    "endpoint": "http://10.0.0.13:9810/metrics",
    "token": "change-me-long-random-token",
    "location": "Hong Kong",
    "remark": "Redis"
  }
]
```

这里填写的 `endpoint` 必须能从 AstrBot 所在机器访问；这里填写的 `token` 必须和探针安装时的 `--token` 一致。
`location` 和 `remark` 可以和探针安装时不同，最终图片会优先显示 AstrBot 配置里的值。

保存配置后，在 OneBot v11 会话里执行：

```text
/srvmon report
```

如果要自动推送，先在目标群聊或私聊执行：

```text
/srvmon bind
```

然后在插件配置里打开 `enable_auto_push`。

### 6. 探针失败但 SSH 能连上的情况

插件的主数据源仍然是 `endpoint` 指向的 HTTP 探针。1.0.7 开始，采集时会先对临时错误做重试，再做一次 TCP 可达兜底检测：

- `probe_retry_count`：默认 `2`，HTTP `408/429/500/502/503/504`、超时、临时网络异常会重试。
- `probe_retry_delay_seconds`：默认 `0.45`，第 2 次请求等待 `0.45s`，第 3 次等待约 `0.9s`。
- `enable_tcp_fallback_detection`：默认开启。
- `tcp_fallback_ports`：默认 `22`，也就是 SSH 端口；插件还会自动额外检测 `endpoint` 里的探针端口，例如 `9810`。
- `tcp_fallback_timeout_seconds`：默认 `1.5`。

因此，如果某台服务器 SSH 能连接，但探针 HTTP 暂时返回 `503` 或超时，图片会把该节点标成“探针异常”，不会再直接当成“离线”。这表示服务器本身可能还活着，但探针服务、鉴权、容器网络、反向代理或安全组到 `9810` 的链路需要检查。

如果你使用非标准 SSH 端口，例如 `2222`，可以把配置改成：

```text
tcp_fallback_ports = 22,2222
```

### 7. 不使用安装脚本的手动方式

如果你不想用 `install_probe.sh`，也可以手动安装：

```bash
sudo mkdir -p /opt/astrbot-server-probe
sudo cp linux_probe.py /opt/astrbot-server-probe/linux_probe.py
sudo chmod +x /opt/astrbot-server-probe/linux_probe.py
```

直接启动：

```bash
ASTRBOT_PROBE_TOKEN='change-me' \
ASTRBOT_PROBE_LOCATION='Singapore' \
ASTRBOT_PROBE_REMARK='API ingress' \
python3 /opt/astrbot-server-probe/linux_probe.py --host 0.0.0.0 --port 9810
```

或安装 systemd 服务：

```bash
sudo cp astrbot-server-probe.service /etc/systemd/system/astrbot-server-probe.service
sudo sed -i 's/change-me/你的令牌/g' /etc/systemd/system/astrbot-server-probe.service
sudo systemctl daemon-reload
sudo systemctl enable --now astrbot-server-probe
```

本地测试探针：

```bash
curl -H 'Authorization: Bearer change-me' http://127.0.0.1:9810/metrics
```

## 命令

所有命令默认仅 AstrBot 管理员可用。

```text
/srvmon report
```

立即采集并在当前 OneBot v11 会话发送监控图片。

```text
/srvmon bind
```

绑定当前 OneBot v11 群聊或私聊为自动推送目标。

```text
/srvmon status
```

查看服务器数量、绑定目标、自动推送和最近一次报告摘要。

```text
/srvmon unbind
```

清除自动推送目标。

## 探针 JSON 格式

插件也可以接入你自己的探针，只要 HTTP GET 返回类似结构：

```json
{
  "timestamp": "2026-05-28T12:34:56Z",
  "cpu": {"usage_percent": 63.4, "cores": 8, "model": "AMD EPYC"},
  "memory": {"used_bytes": 6442450944, "total_bytes": 17179869184},
  "storage": [
    {"mount": "/", "used_bytes": 85899345920, "total_bytes": 107374182400}
  ],
  "location": {"country": "SG", "city": "Singapore"},
  "system": {"os": "Ubuntu", "version": "24.04", "kernel": "6.8"},
  "network": {"rx_bps": 1250000, "tx_bps": 2500000},
  "disk_io": {"read_bps": 512000, "write_bps": 1024000},
  "uptime_seconds": 98765
}
```

字段名有一定兼容性，例如 `memory.percent`、`storage.percent`、`network.in_bps`、`network.out_bps`、`disk_io.read_bytes_per_sec` 等也能识别。

## 渲染和发送模式

默认 `render_backend=local_pillow`，插件会在 AstrBot 本机用 Pillow 直接生成监控图片文件，不依赖 AstrBot 官方远端 t2i 服务。因此即使日志里出现类似：

```text
t2i.network_strategy: Endpoint https://t2i-rain.soulter.top/text2img failed: HTTP 502
```

也不会影响默认模式下的服务器监控图片生成。这个 WARN 来自 AstrBot 核心远端文转图服务，不是服务器探针采集失败。

`render_backend` 可选值：

- `local_pillow`：默认，本地渲染，最稳定，不访问远端 t2i。
- `html_remote`：使用 AstrBot `html_render()` 远端 HTML 渲染，视觉最接近 CSS 模板，但依赖远端 t2i 服务稳定性。
- `auto`：先尝试远端 HTML 渲染，失败后自动使用本地 Pillow 兜底。该模式仍可能在日志里看到远端 t2i 502 WARN。

本地渲染默认 `local_render_scale=2`，会先用 2 倍画布绘制再缩小成最终 JPEG，圆角、文字和图表边缘会更细腻。图片尺寸仍然按布局输出，例如默认宽度仍是 `1600px`，服务器数量越多时只增加高度。

如果 `local_pillow` 渲染出来中文是方块，说明 AstrBot 所在 Linux 环境没有可用中文字体。先安装 CJK 字体：

```bash
sudo apt update
sudo apt install -y fonts-noto-cjk
```

安装后重启 AstrBot。也可以在插件配置里填写 `local_font_path`，例如：

```text
/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
```

插件会优先使用 `local_font_path`，留空时自动寻找 Noto Sans CJK、思源黑体、文泉驿等常见中文字体。

默认 `image_send_mode=onebot_url`，插件会把生成的图片通过 OneBot v11 原生 image 段发送。对于本地生成的图片，插件会自动转换成 `base64://...` 发送，避免 OneBot/aiocqhttp 在另一个容器或进程里读取不到 AstrBot 的 `/tmp` 文件而报 `ENOENT`。

如果你的 OneBot 协议端无法访问 AstrBot 渲染器返回的 URL，可以尝试：

- `onebot_file`：保留兼容项；本地文件仍会自动转成 `base64://...`，避免跨容器路径不可见
- `astrbot`：改用 AstrBot 标准 `MessageChain` 图片发送

## 注意

- 默认本地渲染依赖 Pillow。AstrBot 4.24.2 本身已包含 Pillow；插件 requirements 也声明了 `Pillow>=11.2.1`。
- 默认本地渲染还需要 AstrBot 所在系统安装中文字体；如果没装，图片中的中文会显示成方块。
- 只有在 `render_backend=html_remote` 或 `render_backend=auto` 时，才会调用 AstrBot 核心远端 t2i / HTML 转图片服务。
- 服务器越多，最终图片越高。插件会自动增加画布高度，保证每台服务器卡片都有稳定位置。
- 探针监听端口应只暴露在可信网络，或务必设置高强度 token。
