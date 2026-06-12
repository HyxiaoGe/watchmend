# dev-ops-sentinel

dev 服务器哨兵：外部状态页监控 + 内部服务巡检 + LLM 智能诊断，异常推飞书富卡片。

- 外部状态页：轮询 Anthropic/OpenAI/GitHub/Cloudflare/Google Cloud，状态翻转推卡
- 内部巡检（Phase 1-2）：16 服务探针、指标/日志扫描、确定性规则引擎、events 事件机、
  09:00 体检日报。设计稿 `docs/superpowers/specs/2026-06-11-internal-patrol-bot-design.md`
- 智能诊断（Phase 3）：事件自动根因诊断 + 日报 AI 总结，见下节

## 运行
```bash
uv sync --dev
cp .env.example .env   # 填飞书 webhook;部署须设 SENTINEL_DIAG_TOKEN 随机值
docker compose up -d   # 或 uvicorn sentinel.app:app
```

门禁：`uv run pytest -q && uv run ruff check . && uv run ruff format .`

## Phase 3 智能诊断（拉取链路）

```
确定性规则命中 → events 表 (diagnosis_status=pending)
                      │
openclaw cron (每 5m, 无模型零成本) → 宿主机 diag_orchestrator.py
                      │  GET /events/pending
                      │  逐事件(≤3) 起隔离会话 openclaw agent --agent sentinel-diag
                      │     工具: loki-mcp(日志) + ops-mcp(指标/容器/系统, 硬只读)
                      │     模型: litellm-proxy → moonshot/kimi-k2.5
                      │  POST /events/{id}/diagnosis (X-Sentinel-Token)
                      ▼
            sentinel 落库 + 发蓝色诊断卡(根因/证据/建议命令/置信度)

daily: 09:00 模板日报(sentinel 自发) → 09:05 openclaw cron → daily_summary.py
       GET /report/daily-data → 一次 agent 会话 → POST /report/summary → 绿色总结卡
```

### 编排 API（host-published 127.0.0.1:8765，仅宿主机可达）

| 端点 | 鉴权 | 用途 |
|---|---|---|
| GET /events/pending | 无 | 待诊断事件列表 |
| POST /events/{id}/diagnosis | X-Sentinel-Token | 诊断回写(done/failed/skipped)，done 时发卡 |
| GET /report/daily-data | 无 | 日报聚合数据 |
| POST /report/summary | X-Sentinel-Token | 发 AI 总结卡 |

### 宿主机部署（dev:~/sentinel-host/）

```bash
# 三件套 + venv(dev 无 uv)
python3 -m venv ~/sentinel-host/.venv
~/sentinel-host/.venv/bin/pip install "httpx>=0.27,<0.29" "mcp>=1.4,<2"
scp host/{ops_mcp,diag_orchestrator,daily_summary}.py dev:~/sentinel-host/
# .env: SENTINEL_DIAG_TOKEN=<与容器侧一致>, chmod 600
```

### sentinel-diag agent 约束（三层工具面）

- 层 1 自由只读：`profile: "minimal"` + alsoAllow 12 个 MCP 查询工具
  （loki 5 + ops 7）——tool policy 层结构性只读；sentinel-diag 已彻底拆除 exec，
  升级为工具面物理只读（无 exec 仍能诊断）
- 层 2 审批后恢复执行：**已上线**——飞书群 @bot 触发容器重启，由专用
  **sentinel-remediate** agent 经包装脚本 `sentinel-restart.sh` 执行（denylist +
  只 restart 非禁止运行容器），每次 exec 经飞书审批把关，未审批/拒绝一律不执行
  （fail-closed）。⚠️ allow-always 白名单粒度=二进制路径：恢复命令须先封装为包装脚本
  再放行（已落地），勿直接放行 docker 等多功能二进制
- 层 3 永远禁止：`security: "allowlist"` 空白名单 + askFallback deny 默认全拒
  + prompt 禁令（`tools.exec` 无命令级 deny 黑名单配置位）

### 排障入口

```bash
openclaw cron list                          # 两任务: sentinel-diag-poll / sentinel-daily-summary
openclaw cron runs --id <job-id>            # 运行记录
journalctl --user -u openclaw-gateway       # 网关日志(审批/工具策略/会话)
docker logs dev-ops-sentinel                # API 访问与发卡日志
sqlite3 data/sentinel.db 'select id,rule,diagnosis_status from events'
ssh dev '~/sentinel-host/.venv/bin/python ~/sentinel-host/diag_orchestrator.py'  # 手动补诊
```

诊断失败有 DB 留痕（diagnosis_status=failed + diagnosis.raw）；OpenClaw 整体不可用时
事件卡照发，diagnosis 滞留 pending，恢复后下个 5m 周期自动补诊。
