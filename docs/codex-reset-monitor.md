# Codex reset 预告与落地确认监控

该能力是 WatchMend 内的独立 `codex_reset` 领域 Job，不使用 Codex 定时任务，也不进入
`IncidentStatus`、告警恢复状态机或 `codex_hooks.py`。默认每 60 秒轮询公开结构化来源，复用
内部巡检广播和飞书自定义机器人签名。

## 事件阶段

| 阶段 | 判定门槛 | 通知 |
|---|---|---|
| `hint` | 官方相关帖子或明确雷达窗口；统计概率本身永不触发 | 疑似预告卡 |
| `announced` | 官方明确预告，同时具备预计窗口、原始链接和 `direct`/`banked` 类型 | 明确预告卡 |
| `delayed` | 明确预告超过预计截止和宽限期，仍未满足确认门槛 | 延迟卡 |
| `confirmed` | 至少两个独立公开来源族一致，或本机只读参考账号证据 | 落地确认卡 |

阶段只单调前进。`delayed` 不是终态，后续证据充分时仍升级 `confirmed`。首次启用不会补发
超过 `SENTINEL_CODEX_RESET_NOTIFY_MAX_AGE_HOURS` 的旧事件。

当前实现只接入公开来源，没有读取本机 Codex 凭证；代码保留了 `local_reference` 证据位，
只有未来明确接入本机只读额度探针时才可置真。第三方 feed 中名为 `reference` 的观察结果不被
视为本机参考账号证据。

## 数据源与合并

优先顺序如下：

1. `https://codex-reset-radar.pages.dev/current.json`
2. `https://codexradar.com/feed.xml`
3. `https://codex-reset.com/api/feed`
4. `https://codex-reset.com/api/timeline`
5. `https://codexreset.org/`，仅在结构化来源不足或出现近期确认时做 HTML 降级/交叉验证，
   且默认最多每小时一次。

解析器基于 2026-08-13 抓取的真实脱敏样本做契约测试。不同来源引用同一官方帖子时按 X
status ID 归一；确认帖与预告帖 ID 不同时，优先使用上游 contextual ID，其次按预告窗口和
相近确认时间合并。`codex-reset.com` 的 feed 与 timeline 视为同一来源族，不能互相凑足
“两个独立来源”的确认门槛；`codexradar` 与 `codexreset.org` 分属另外的来源族。

`current.json` 中的预测概率不参与事件分类。来源响应正文不写日志，错误只记录异常类型；
Webhook、签名密钥和其他凭证仍只由现有环境变量加载。

## 持久化与可靠性

数据保存在 `SENTINEL_DB_PATH` 指向的同一 SQLite 文件中，但使用独立表：

- `codex_reset_events`：规范事件与当前阶段；
- `codex_reset_evidence`：按 `source_name + source_item_id` 去重的证据；
- `codex_reset_deliveries`：规范事件每个阶段的投递回执、次数与下次重试时间；
- `codex_reset_source_health`：最近抓取成功、内容时间和连续失败次数；
- `codex_reset_leases`：跨进程单实例租约。

抓取使用有限次数指数退避。通知采取持久化重试：任一广播渠道成功才把该阶段标记为已送达；
全部失败时按 `RETRY_BASE × 2^(attempt-1)` 退避，受最大间隔和最大次数限制。SQLite 使用 WAL、
busy timeout 和带过期时间的租约；进程收到退出信号后由 WatchMend 生命周期释放租约并关闭
数据库连接。

## 启用

在私有 `.env` 中设置：

```dotenv
SENTINEL_CODEX_RESET_ENABLED=true
SENTINEL_CODEX_RESET_POLL_SECONDS=60
```

通知复用 `FEISHU_PATROL_WEBHOOK` / `FEISHU_PATROL_SIGN_SECRET`；巡检机器人留空时沿用 vendor
机器人。也会随现有广播配置发送到 Telegram、ntfy 或通用 webhook。不要把 Webhook 或签名
密钥写进源码、Compose、测试夹具或日志。

完整可调参数见 [`.env.example`](../.env.example)。修改配置后按现有部署方式重建/重启
WatchMend，无需创建第二个服务。

## 健康检查与排障

启用后 `/health` 增加 `codex_reset` 对象，包含 Job 最近成功时间、每个来源内容时间、连续失败
次数和新鲜来源族数量。单一来源短暂失败会保留最近成功内容，不会令 WatchMend 顶层健康状态
失败；没有新鲜来源时 reset 子状态变为 `stale`，只有一个新鲜来源族时为 `degraded`。

排障时先检查：

1. 日志中是否有 `codex reset source <name> failed: <ExceptionType>`；日志不应出现响应正文；
2. `/health` 中 `last_success_ts` 与各来源 `content_ts`；
3. SQLite 的 `codex_reset_deliveries` 是否处于 `pending`，以及 attempts 是否增长；
4. 飞书机器人是否启用、签名密钥是否与机器人配置一致。

要停用只需把 `SENTINEL_CODEX_RESET_ENABLED=false` 后重启。独立表保留历史去重状态；再次启用
不会重复发送已经成功投递的同一阶段。
