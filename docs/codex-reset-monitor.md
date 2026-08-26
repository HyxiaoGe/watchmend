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
| `confirmed` | 不同来源族且不同原始记录相互印证，或官方重置记录/正式预告 + 本机共享 Codex 周窗口 | 落地确认卡 |

`direct` 事件阶段只单调前进。`delayed` 不是终态，后续证据充分时仍升级 `confirmed`。
首次启用不会补发超过 `SENTINEL_CODEX_RESET_NOTIFY_MAX_AGE_HOURS` 的旧事件。

`banked` 是公告型事件：官方相关账号明确宣布 banked reset，且给出“当天内”等未来发放时间时，
直接发送 `announced`。这类事件不要求额度窗口重启或本机账号观察到入账，也不会进入
`delayed`/`confirmed`。卡片保留官方原帖措辞和链接，不把模糊的“当天内”伪装成某个时区的
精确截止时间。普通 credits、庆祝帖或统计概率仍不会触发。

可选的 `local_reference` 探针通过 Codex 官方 app-server 的 `account/rateLimits/read` 只读读取
额度窗口元数据。它可确认已有正式预告，也可与六小时内唯一的官方 direct 到账记录关联，
无需先有预告；单独的本机窗口不能创建全局重置事件。认证文件只以单文件只读方式挂载，
代码不复制、不解析、不记录其内容；卡片和数据库也
不保存额度百分比。第三方 feed 中名为 `reference` 的观察结果不被视为本机参考账号证据。

### 静默到账（无需预告）

2026-08-25 的静默重置已固定为真实归档契约样本：即使 `announcement_state=announced`、
没有 `official_window`，明确“weekly usage back to 100%”的非预览记录仍作为到账候选，
而非补造预告。官方“reset propagated to accounts”也属于完成措辞；未来式、否定式与
5 小时限额政策调整不能据此确认为到账。

不同网站引用同一 X status ID 只算一份原始公开证据，feed/timeline 也不能凑数。此时需
本机共享 `codex` 周窗口相互印证；模型专属窗口不参与全局确认，因为未使用的空窗口可能
每次读取都返回一个新的推算起点。窗口证据默认允许最近 24 小时内的匹配（只保存时间
元数据），先于公开归档到达的未关联事实也会持久保存，重启后可继续匹配。多个不同事件
均落入六小时容差时不自动关联。

确认后直接发送「Codex 静默重置已确认」，不伪造 `hint`/`announced` 阶段；卡片展示
“此前未发现预告”、核验方式和证据时间范围，不把不同站点的时间当作所有账号的精确到账
时刻。逐阶段去重、失败重试和历史事件最大通知年龄仍然适用。如果公开来源和本机都没有
足够证据，则等待后续证据，不保证发现每一次完全无公开记录的重置。

## 数据源与合并

优先顺序如下：

1. `https://codex-reset-radar.pages.dev/current.json`
2. `https://codexradar.com/feed.xml`
3. `https://codex-reset.com/api/feed`
4. `https://codex-reset.com/api/timeline`
5. `https://codexreset.org/`，仅在结构化来源不足或出现近期确认时做 HTML 降级/交叉验证，
   且默认最多每小时一次。

解析器基于 2026-08-13 与 2026-08-21 抓取的真实脱敏样本做契约测试，其中包括官方
`credits + banked reset + during the day` 公告。不同来源引用同一官方帖子时按 X
status ID 归一；确认帖与预告帖 ID 不同时，优先使用上游 contextual ID，其次按预告窗口和
相近确认时间合并。`codex-reset.com` 的 feed 与 timeline 视为同一来源族，不能互相凑足
“两个独立来源”的确认门槛；`codexradar` 与 `codexreset.org` 分属另外的来源族，
但即便来源族不同，转载同一原帖也不重复计为独立公开证据。

`current.json` 中的预测概率不参与事件分类。来源响应正文不写日志，错误只记录异常类型；
Webhook、签名密钥和其他凭证仍只由现有环境变量加载。

## 持久化与可靠性

数据保存在 `SENTINEL_DB_PATH` 指向的同一 SQLite 文件中，但使用独立表：

- `codex_reset_events`：规范事件与当前阶段；
- `codex_reset_evidence`：按 `source_name + source_item_id` 去重的证据；
- `codex_reset_deliveries`：规范事件每个阶段的投递回执、次数与下次重试时间；
- `codex_reset_source_health`：最近抓取成功、内容时间和连续失败次数；
- `codex_reset_leases`：跨进程单实例租约。
- `codex_reset_semantic_cache`：按来源条目和正文哈希缓存意图结果、失败次数与下次重试时间。

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

可选的模型补漏复用现有状态编辑器端点：

```dotenv
SENTINEL_CODEX_RESET_SEMANTIC_ENABLED=true
```

只有规则未识别、时间仍新鲜且 URL 属于官方账号的帖子会请求模型。结构化输出只允许
`ignore/hint/announced`，但第一版所有正向结果都按 `hint` 入库；明确预告窗口与落地确认仍只由
确定性公开证据或本机只读额度事实升级。结果按正文哈希持久缓存，轮询不会重复消耗模型调用。

启用模型补漏后，英文通知摘要还会在发送前生成简体中文翻译。飞书卡片先保留原始“摘要”，
再显示“中文翻译”；中文原文不会重复翻译。翻译按规范事件与正文哈希缓存，翻译失败只记录
异常类型并继续发送原始摘要，不阻塞 reset 通知。

如需启用本机参考账号确认，再设置：

```dotenv
SENTINEL_CODEX_RESET_REFERENCE_ENABLED=true
SENTINEL_CODEX_RESET_REFERENCE_CLI=/usr/local/bin/codex-reference
SENTINEL_CODEX_RESET_REFERENCE_CODEX_HOME=/run/codex-reference
```

部署时仅把宿主机现有 Codex CLI 和 `auth.json` 两个文件分别只读挂载到上述 CLI 路径和
`/run/codex-reference/auth.json`。不要挂载整个 Codex 配置目录，也不要把宿主机实际路径写进
仓库。探针使用最小环境启动 CLI，丢弃 stderr，协议错误只记录异常类型。只读挂载意味着凭证
过期后探针不会绕过权限去刷新文件；此时公开来源仍正常工作，健康信息会标记该来源失败。

官方 app-server 请求顺序为 `initialize`、`initialized`、`account/rateLimits/read`。探针仅选择
不少于 `SENTINEL_CODEX_RESET_REFERENCE_MIN_WINDOW_MINUTES` 的窗口，并按
`resetsAt - windowDurationMins × 60` 推导共享 `codex` 窗口起点；起点必须足够新且落入
已有预告窗口，或与唯一的官方到账记录在六小时容差内，才可确认。

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
