# WatchMend

> Watches your server, figures out what broke, *then* pings you.

[中文](README.md) · MIT License · Python 3.12+ · single container · SQLite

WatchMend is a lightweight monitoring sentinel for personal servers, homelabs and
small teams. **Deterministic rules decide when to alert; an LLM (optional) only
explains what happened.** Alerts arrive as rich Feishu (Lark) cards with automated
root-cause diagnosis attached. Everything degrades gracefully by configuration —
the minimal footprint is Docker plus a single Feishu webhook.

```
┌──────────────────────── watchmend container (256MB) ───────────────────────┐
│  Vendor status pages (Anthropic/OpenAI/GitHub/Cloudflare/GCP) ─┐           │
│  HTTP probes (your service list) ──────────────────────────────┤           │
│  Metric rules (PromQL: disk/mem/swap/restarts/OOM/middleware) ─┼─► rule    │
│  Log rules (LogQL: error spikes vs 7-day baseline) ────────────┤   engine  │
│  Daily hygiene (backup freshness/disk forecast/cert expiry) ───┘     │     │
│                                                                      ▼     │
│  LLM diagnosis (optional): pending events → tool loop ────► Feishu cards   │
│  All tools read-only: prom_query / loki_logs / docker ps·logs·inspect      │
└──────────────────────────────────────────────────────────────────────────────┘
```

## 5-minute demo

Ships its own prometheus / loki / cadvisor / node-exporter plus a sample service —
zero external dependencies:

```bash
git clone https://github.com/HyxiaoGe/watchmend && cd watchmend
make demo     # first run scaffolds .env and stops; fill in a Feishu bot webhook, run again
```

Kill the sample service to see an alert card:

```bash
docker compose -f docker-compose.demo.yml stop demo-app    # alert card in ~15 min
docker compose -f docker-compose.demo.yml start demo-app   # ✅ recovery card follows
```

## Production deployment

```bash
cp .env.example .env                      # at least one notification channel required
cp services.example.yaml services.yaml    # your own probe list
make up                                   # or docker compose up -d --build
```

Most data sources are optional — leave one empty and that layer turns off cleanly
(the docker layer is the exception, see the note below the table):

| Config | Capability | When empty |
|---|---|---|
| `FEISHU_VENDOR_WEBHOOK` | alert/report Feishu cards | that channel off |
| `SENTINEL_TELEGRAM_BOT_TOKEN` + `SENTINEL_TELEGRAM_CHAT_ID` | Telegram push (both required to enable) | that channel off |
| `SENTINEL_NTFY_URL` (optional `SENTINEL_NTFY_TOKEN`) | ntfy push, full topic URL | that channel off |
| `SENTINEL_WEBHOOK_URL` (optional `SENTINEL_WEBHOOK_TOKEN`) | Generic webhook, structured JSON | that channel off |
| `services.yaml` | HTTP probes + latency baselines | vendor-status-only mode |
| `SENTINEL_PROMETHEUS_URL` | disk/memory/restart metric rules | metrics layer off |
| `SENTINEL_LOKI_URL` | error-log spike detection | log layer off |
| `SENTINEL_MIDDLEWARE_METRICS` | pg/redis exporter `up` fallback | check skipped |
| `SENTINEL_CERT_DOMAINS` | TLS certificate expiry check | check skipped |
| backup dir mount | pg_dump freshness check | check skipped |
| `LLM_BASE_URL` + `LLM_MODEL` | root-cause diagnosis + AI report summary | LLM layer off |
| `SENTINEL_DOCKER_HOST` | container down/unhealthy/OOM detection + docker diagnosis tools | docker layer off † |

> † Unlike the other layers, the docker layer is **on by default**: `docker-compose.yml`
> ships a read-only `docker-socket-proxy` sidecar (`CONTAINERS=1`, POST denied by
> default, dedicated `internal` network), and the WatchMend container **never mounts the
> bare socket**. To turn the whole layer off: clear `SENTINEL_DOCKER_HOST` and comment out
> the `docker-proxy` service plus sentinel's `depends_on`/`docker_proxy` network in compose
> (the file has inline notes).

> **Notification channels are a broadcast model**: every configured channel receives each alert/report/diagnosis; failures are isolated (one channel failing does not affect the others). **At least one channel must be configured** (Feishu `FEISHU_VENDOR_WEBHOOK` or any of the above) to start; `FEISHU_VENDOR_WEBHOOK` is no longer mandatory — overseas self-hosters can run with Telegram/ntfy/webhook only.

All 40+ settings (thresholds, cooldowns, verbosity…) are documented inline in
[.env.example](.env.example).

## LLM diagnosis (optional)

**Not locked to one platform.** WatchMend speaks the standard OpenAI
`chat/completions` + function-calling protocol, so any service exposing an
OpenAI-compatible endpoint is plug-and-play — change three `.env` lines, zero code:

```bash
LLM_BASE_URL=https://api.deepseek.com/v1   # see table below for each platform
LLM_API_KEY=sk-...                          # any value for local endpoints
LLM_MODEL=deepseek-chat
```

| Platform | `LLM_BASE_URL` | `LLM_MODEL` (example) | Notes |
|---|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-5.5` | |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` | verified by this project |
| Moonshot / Kimi | `https://api.moonshot.cn/v1` | `kimi-k2` | international: `api.moonshot.ai/v1`; keys are region-bound |
| Zhipu GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4` | |
| Ollama (local) | `http://localhost:11434/v1` | `qwen3` | any key value; pick a tool-calling-capable model |
| vLLM (self-hosted) | `http://<host>:8000/v1` | your served model | start with `--enable-auto-tool-choice` and `--tool-call-parser` |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.5-flash` | compat-layer tool calling is limited; also geo-restricted — some regions get `User location is not supported`, route via a supported-region gateway |
| Anthropic Claude | `https://api.anthropic.com/v1/` | `claude-opus-4-8` | compat layer is officially test-only; `strict` is ignored |
| LiteLLM gateway | `http://<host>:4000` | your configured alias | proxies many providers; key optional when local |

> Model names are examples valid at time of writing (2026-06); check each
> platform's latest docs. Diagnosis depends on function calling — make sure your
> chosen model supports tool calls, otherwise the diagnosis layer degrades to a
> single-round summary with no tools.

When a rule fires, the model investigates inside the container with read-only
tools (PromQL queries, log fetches, container state) and produces a structured
diagnosis card: symptom / probable root cause / evidence / suggested commands /
confidence.

Security boundaries (design stance, not afterthought patches):

- **Deterministic rules decide whether to wake you up** — the model explains, it
  never gates alerts
- All tools are read-only; `docker` tools go through a read-only socket proxy (the
  container never mounts the bare socket; enabled by default via the compose
  `docker-proxy` sidecar), and `docker inspect` output has env values redacted before
  reaching the model
- Tool output is declared untrusted data, guarding against log-injection steering
- Suggested commands are for humans only and are never executed automatically

## Design philosophy

- **Not-evaluated ≠ recovered**: when a data source fails or is turned off, its
  open alerts are never falsely resolved — stay open rather than send a fake
  green card
- **Cooldowns, recovery cards, daily health report**: the same incident won't
  spam you within the cooldown window; recovery cards state the outage duration
- **Baselines over absolutes**: latency and error logs compare against 7-day
  same-time-of-day baselines; absolute thresholds are only a backstop
- **The watcher is watched**: consecutive data-source failures escalate into a
  "scan failed" card — the sentinel never goes silently blind

## Advanced

- **Host-side agent orchestration** (`host/`): instead of the in-container
  driver, let your own agent runner (any CLI) pull pending events via the HTTP
  orchestration API, optionally extending into allowlisted recovery scripts
  (denylist + human approval). Mutually exclusive with the in-container driver.
- **Reverse monitoring**: point Uptime Kuma (or similar) at `/health` to watch
  the watcher — see [docs/](docs/).

## FAQ

**Why Feishu-only notifications?** The project grew out of the author's own
setup. A notification-channel abstraction (Telegram / Slack / Discord / generic
webhook) is top of the roadmap — PRs welcome.

**vs. Uptime Kuma / Gatus?** Those are probes + status pages. WatchMend focuses
on the rule engine + incident lifecycle (cooldown/recovery/baselines) + LLM
root-cause diagnosis, and treats your existing Prometheus / Loki as data sources
instead of rebuilding collection.

**vs. Alertmanager?** Not a replacement. If you already run a full observability
stack with mature alerting rules you may not need this. WatchMend serves the
"one server, a dozen containers, want monitoring that works out of the box and
diagnoses I can actually read" scenario.

**Will the LLM mess with my server?** See the security boundaries above:
read-only everything, socket off by default, suggested commands never executed.
The deterministic layers don't depend on the LLM at all.

## Development

```bash
uv sync --dev
make check        # ruff + pytest + leak check
```

## License

[MIT](LICENSE)
