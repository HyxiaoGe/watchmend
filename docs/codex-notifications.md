# Codex 生命周期通知

WatchMend 通过 Codex 官方生命周期 Hooks 判断主任务是否真的需要用户关注，不再把每个
`agent-turn-complete` 都发送到飞书。Hook 只把经过白名单和脱敏的事件快速送入 WatchMend；
5 分钟宽限、取消和最终广播由 WatchMend 后台完成，不阻塞 Codex 代理循环。

官方契约参考：[Hooks](https://learn.chatgpt.com/docs/hooks) 与
[高级配置](https://learn.chatgpt.com/docs/config-file/config-advanced#hooks)。

## 1. 默认触发策略

| 场景 | 是否进入候选 | 5 分钟内如何取消 |
|---|---|---|
| Codex 请求审批 | 是 | 审批后工具产生 `PostToolUse`、用户提交新输入或会话结束 |
| Codex 明确请求补充输入 | 是 | 用户提交新输入或会话结束 |
| 最终回复表明执行失败或无法继续 | 是 | 用户提交新输入或会话结束 |
| 正常完成且本回合耗时至少 3 分钟 | 是 | 用户提交新输入或会话结束 |
| 普通问答、短回合、子代理结束、单次工具完成 | 否 | 不适用 |

默认宽限期由 `SENTINEL_CODEX_HOOK_GRACE_SECONDS=300` 控制，长任务阈值由
`SENTINEL_CODEX_LONG_TURN_SECONDS=180` 控制。候选正文只保存在进程内存中；WatchMend
重启会直接丢弃未发送候选，不在重启后补发旧上下文。成功发送后仅持久化事件键哈希回执，
用于幂等去重，不保存提示词或最终回复正文。

Codex 当前没有“审批结果”Hook。用户点击批准后，WatchMend 要等对应工具产生
`PostToolUse` 才能观察到操作并取消候选；如果获批工具本身运行超过 5 分钟，仍可能出现一条
“等待审批”通知。这是官方可观测事件的边界，不能通过 Hook 可靠感知窗口聚焦、滚动、正在
输入但尚未提交等 UI 行为。

## 2. 启用 WatchMend 入口

在 WatchMend 私有 `.env` 生成并配置独立 Token：

```bash
openssl rand -hex 32
```

```dotenv
SENTINEL_CODEX_INGEST_TOKEN=<上一步生成的随机值>
SENTINEL_CODEX_RECEIPT_RETENTION_DAYS=30
SENTINEL_CODEX_HOOK_GRACE_SECONDS=300
SENTINEL_CODEX_LONG_TURN_SECONDS=180
SENTINEL_CODEX_HOOK_POLL_SECONDS=1
```

Token 为空时 `POST /notifications/codex` 和 `POST /notifications/codex/hooks` 都返回 404。
Token 不应复用飞书 Webhook、签名密钥或诊断 API Token。8765 端口应只在受信任网络内
开放；远程实例优先使用 VPN、SSH 隧道或带鉴权的反向代理。

## 3. 配置本机 Hook 客户端

安装开发环境后会生成 `watchmend-codex-hook`：

```bash
uv sync --dev
mkdir -p ~/.config/watchmend
cp docs/snippets/codex-notify.example.json ~/.config/watchmend/codex-notify.json
chmod 600 ~/.config/watchmend/codex-notify.json
```

编辑 JSON 中的 URL 和 Token。URL 可继续写成 `/notifications/codex`，Hook 客户端会派生为
`/notifications/codex/hooks`。客户端固定使用最多 1.5 秒超时且不重试；任何网络、配置或
载荷错误都返回中性 `{}` 和退出码 0，不改变 Codex 行为。

复制 [codex-hooks.example.json](snippets/codex-hooks.example.json) 为
`~/.codex/hooks.json`，把两处占位绝对路径改为当前 WatchMend 虚拟环境和私有配置路径。
配置包含 `UserPromptSubmit`、`PermissionRequest`、`PostToolUse`、`Stop`、`SessionEnd`。

非托管 Hook 在新增或内容变化后不会自动执行。请在 Codex 中运行 `/hooks`，检查来源和命令
后信任当前定义；信任与 Hook 内容哈希绑定，后续修改需要重新审核。

## 4. 与现有 `notify` / Computer Use 共存

`notify` 目前只提供 `agent-turn-complete`，适合桌面 Computer Use 的 `turn-ended` 回调，
不适合直接做 WatchMend 业务通知。建议恢复原有回调：

```toml
notify = [
  "/absolute/path/SkyComputerUseClient",
  "turn-ended",
]
```

WatchMend 保留旧 `watchmend-codex-notify` 分发器和 `POST /notifications/codex` 端点用于兼容
已经部署的客户端，但新配置不应再把它串进 `notify`，否则仍会恢复逐回合通知。

## 5. 数据与投递边界

- Hook 客户端只发送会话/回合标识、项目、工作目录、脱敏后的提示/最终摘要，以及工具名、
  工具调用 ID 或工具输入哈希；不发送 `tool_input`、`tool_response` 或 transcript 内容。
- 提示摘要限制 800 字，结果摘要限制 2,000 字，并运行通用密钥形态脱敏。
- 候选在内存中按 `session_id + turn_id + event` 去重；发送成功后按 SHA-256 回执去重。
- 所有通知渠道失败时最多进行两次后台退避重试；期间如果用户产生可观察操作，候选仍可取消。
- 生命周期通知不进入 WatchMend 的告警、恢复、冷却或 LLM 诊断状态机。
