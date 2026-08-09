# Codex 回合完成通知

WatchMend 可以接收 Codex 官方 `agent-turn-complete` 事件，沿用已配置的飞书、Telegram、
ntfy 和通用 webhook 广播渠道。该事件只表示 Codex 主回合已经结束，不代表代码、测试、
提交或部署成功，因此飞书使用蓝色信息卡，不进入 WatchMend 的告警、恢复、冷却或 LLM
诊断状态机。

## 1. 启用 WatchMend 入口

在 WatchMend 私有 `.env` 生成并配置独立 Token：

```bash
openssl rand -hex 32
```

```dotenv
SENTINEL_CODEX_INGEST_TOKEN=<上一步生成的随机值>
SENTINEL_CODEX_RECEIPT_RETENTION_DAYS=30
```

Token 为空时 `POST /notifications/codex` 返回 404，入口保持关闭。Token 不应复用飞书
Webhook、签名密钥或诊断 API Token。WatchMend 只持久化 `thread-id + turn-id` 的 SHA-256
回执用于去重，不保存任务与结果摘要。

## 2. 配置 Codex 本机分发器

安装开发环境后会生成 `watchmend-codex-notify` 命令：

```bash
uv sync --dev
```

复制私有客户端配置并限制权限：

```bash
mkdir -p ~/.config/watchmend
cp docs/snippets/codex-notify.example.json ~/.config/watchmend/codex-notify.json
chmod 600 ~/.config/watchmend/codex-notify.json
```

编辑其中的 URL 和 Token。URL 应指向受信任网络内的 WatchMend；不要为了本功能把 8765
直接暴露到公网。远程实例优先使用 VPN、SSH 隧道或带鉴权的反向代理。

Codex 的 `notify` 只能配置一个外部命令。如果已有回调，必须通过 `--upstream` 原样保留：

```toml
notify = [
  "/absolute/path/watchmend/.venv/bin/watchmend-codex-notify",
  "--config",
  "/absolute/path/.config/watchmend/codex-notify.json",
  "--upstream",
  "/absolute/path/existing-notify-client",
  "turn-ended",
]
```

Codex 会把事件 JSON 自动追加为最后一个参数。分发器不用 shell 执行原回调，并与
WatchMend 请求并行；任一分支失败都只记录非敏感警告，进程固定返回 0，不改变 Codex
回合结果。

## 3. 数据与投递边界

- 仅接受 `agent-turn-complete`；其他事件仍会转发给既有回调，但不会发送到 WatchMend。
- 只发送项目名、工作目录、最后一条输入摘要、最终回复摘要及线程/回合标识。
- 发送前运行通用密钥脱敏，并限制任务摘要 800 字、结果摘要 2,000 字。
- WatchMend 所有渠道均失败时返回 503，本机最多执行配置的有限重试。
- 成功回执按事件键去重；回执到期后清理，正文不落库。
