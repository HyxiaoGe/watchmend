# WatchMend

> 盯着你的服务器,出事先查明白,再来叫你。

[English](README.en.md) · MIT License · Python 3.12+ · 单容器 · SQLite

WatchMend 是一个面向个人服务器 / homelab / 小团队的轻量监控哨兵:
**确定性规则负责发现问题,LLM(可选)只负责解释问题**。异常以飞书富卡片推送,
附带自动根因诊断;一切按配置优雅降级——最小可跑面 = Docker + 一个飞书 webhook。

```
┌────────────────────────── watchmend 容器(256MB)──────────────────────────┐
│  外部状态页轮询(Anthropic/OpenAI/GitHub/Cloudflare/GCP)──┐                │
│  HTTP 探针(你的服务清单)─────────────────────────────────┤                │
│  指标规则(PromQL:磁盘/内存/swap/容器重启/OOM/中间件)─────┼─► 规则引擎     │
│  日志规则(LogQL:错误日志相对七日基线激增)────────────────┤  事件/冷却/恢复│
│  每日 hygiene(备份新鲜度/磁盘填满预测/证书临期)──────────┘       │        │
│                                                                  ▼        │
│  LLM 诊断(可选):pending 事件 → tool 循环自主调查 ──────► 飞书卡片        │
│  工具全只读:prom_query / loki_logs / docker ps·logs·inspect              │
└────────────────────────────────────────────────────────────────────────────┘
```

## 5 分钟 demo

自带 prometheus / loki / cadvisor / node-exporter 和一个示例服务,零外部依赖:

```bash
git clone https://github.com/HyxiaoGe/watchmend && cd watchmend
make demo          # 首次会生成 .env 并停下来,填一个飞书群机器人 webhook 后再跑一次
```

起来后干掉示例服务看告警卡:

```bash
docker compose -f docker-compose.demo.yml stop demo-app   # ~15 分钟后收到事件卡
docker compose -f docker-compose.demo.yml start demo-app  # 之后会收到 ✅ 恢复卡
```

## 正式部署

```bash
cp .env.example .env                      # 按注释填:webhook 必填,其余按需
cp services.example.yaml services.yaml    # 改成你自己的服务探针清单
make up                                   # 或 docker compose up -d --build
```

接入点大多可选、留空即关、各层独立(docker 层例外,见表末):

| 配置 | 启用的能力 | 留空时 |
|---|---|---|
| `FEISHU_VENDOR_WEBHOOK` | 告警/日报卡片(**唯一必填**) | — |
| `services.yaml` | 内部服务 HTTP 探针 + 延迟基线 | 只监控外部状态页 |
| `SENTINEL_PROMETHEUS_URL` | 磁盘/内存/容器重启等指标规则 | 指标层关闭 |
| `SENTINEL_LOKI_URL` | 错误日志激增检测 | 日志层关闭 |
| `SENTINEL_MIDDLEWARE_METRICS` | pg/redis 等 exporter up 兜底 | 跳过该检查 |
| `SENTINEL_CERT_DOMAINS` | 公网证书临期检查 | 跳过该检查 |
| 备份目录挂载 | pg_dump 备份新鲜度检查 | 跳过该检查 |
| `LLM_BASE_URL` + `LLM_MODEL` | 事件根因诊断 + 日报 AI 总结 | LLM 层关闭 |
| `SENTINEL_DOCKER_HOST` | 容器停止/不健康/OOM 检测 + docker 诊断工具 | docker 层关闭 † |

> † 与其它层相反:docker 层**默认开启**——`docker-compose.yml` 预置了只读
> `docker-socket-proxy` 边车(`CONTAINERS=1`、POST 默认拒、专用 `internal` 网),
> 本容器**永不挂裸 socket**。要整层关闭:清空 `SENTINEL_DOCKER_HOST` 并注释掉 compose
> 里的 `docker-proxy` service 及 sentinel 的 `depends_on`/`docker_proxy` 网络(文件内有注释)。

全部 40+ 配置项(阈值、冷却、播报粒度…)见 [.env.example](.env.example),每项带注释。

## LLM 诊断(可选)

**不锁平台。** WatchMend 走标准 OpenAI `chat/completions` + function calling 协议,
任何提供 OpenAI 兼容端点的服务都即插即用——只改 `.env` 三行,代码零改动:

```bash
LLM_BASE_URL=https://api.deepseek.com/v1   # 各平台见下表
LLM_API_KEY=sk-...                          # 本地端点可随便填
LLM_MODEL=deepseek-chat
```

| 平台 | `LLM_BASE_URL` | `LLM_MODEL` 示例 | 备注 |
|---|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-5.5` | |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` | 本项目实测验证 |
| Moonshot / Kimi | `https://api.moonshot.cn/v1` | `kimi-k2` | 国际站用 `api.moonshot.ai/v1`,key 分区不通用 |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4` | |
| Ollama(本地) | `http://localhost:11434/v1` | `qwen3` | key 随便填;选支持 tool calling 的模型 |
| vLLM(自建) | `http://<host>:8000/v1` | 你部署的模型 | 启动需加 `--enable-auto-tool-choice` 和 `--tool-call-parser` |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.5-flash` | 兼容层 tool calling 受限;且有地域限制,部分地区(如中国大陆)直连报 `User location is not supported`,需经支持地域的网关 |
| Anthropic Claude | `https://api.anthropic.com/v1/` | `claude-opus-4-8` | 官方定位兼容层为测试用,`strict` 不生效 |
| LiteLLM 网关 | `http://<host>:4000` | 你在 LiteLLM 配的别名 | 统一代理多家,本地可免 key |

> 模型名为撰写时(2026-06)各平台可用示例,实际以平台最新文档为准。诊断依赖
> function calling,选模型时务必确认其支持工具调用——不支持则诊断层退化为不调工具的单轮总结。

事件命中后,模型在容器内用只读工具自主调查(查 PromQL、捞日志、看容器状态),
产出结构化诊断卡:现象 / 推测根因 / 证据 / 建议命令 / 置信度。

安全边界(设计立场,不是事后补丁):

- **要不要叫醒你由确定性规则决定**,模型只解释,不参与告警判定
- 工具全只读;`docker` 工具经只读 socket 代理访问(本容器不挂裸 socket,默认随 compose
  的 `docker-proxy` 边车启用),`docker inspect` 输出先遮蔽环境变量值再喂给模型
- 工具返回的日志内容被声明为不可信数据,防日志注入操纵结论
- 建议命令只是给人看的,永远不会被自动执行

## 设计哲学

- **不评估 ≠ 恢复**:某层数据源故障或被关闭时,它覆盖的存量告警绝不会被
  误判为"已恢复"发绿卡——宁可保持未决,不发假恢复
- **告警有冷却、有恢复卡、有每日体检日报**:同一事件 6 小时内不重复轰炸;
  恢复时明确告知持续时长
- **基线相对值优先**:延迟和错误日志都对比七日同时段基线,
  绝对阈值只做兜底,少误报
- **巡检自身也被监控**:数据源连续失败会升级成"巡检失败"卡,
  哨兵不会安静地失明

## 进阶

- **宿主机 agent 编排**(`host/`):不用容器内直连,改由你自己的 agent runner
  (任何 CLI)经 HTTP 编排 API 拉取 pending 事件做诊断,还可扩展白名单恢复脚本
  (denylist + 人工审批)。与容器内直连**二选一**。
- **反向监控**:`/health` 端点可挂到 Uptime Kuma 等,看护哨兵本身,见 [docs/](docs/)。

## FAQ

**为什么通知只有飞书?** 项目从作者自用长出来,飞书卡片生态最完整。
通知渠道抽象(Telegram / Slack / Discord / 通用 webhook)在 roadmap 首位,欢迎 PR。

**和 Uptime Kuma / Gatus 什么区别?** 它们是探针 + 状态页;WatchMend 的重心是
规则引擎 + 事件机(冷却/恢复/基线)+ LLM 根因诊断,并把你已有的
Prometheus / Loki 当数据源,不重复造采集层。

**和 Alertmanager 什么区别?** 不是替代品。如果你已有完整 observability 栈和
告警规则体系,可能不需要它;WatchMend 服务的是"一台服务器跑十几个容器,
想要开箱即用的监控 + 看得懂的诊断"的场景。

**LLM 会不会乱动我的服务器?** 见上文安全边界:全只读、socket 默认关、
建议命令不自动执行。完全不配 LLM 也能用,确定性巡检不依赖它。

## 开发

```bash
uv sync --dev
make check        # ruff + pytest + 泄漏检查
```

## License

[MIT](LICENSE)
