# WatchMend

> Watches your server, figures out what broke, *then* pings you.

[中文](README.md) · MIT License · Python 3.12+ · single container · SQLite

WatchMend is an AI-assisted operations sentinel for personal servers, homelabs,
and small teams. It turns HTTP probes, Docker, Prometheus, Loki, and vendor status
pages into an **incident lifecycle with triggering, investigation, recovery, and
cooldowns**, then delivers only what deserves attention in real time or scheduled
digests.

- **Deterministic rules decide when to alert.** An LLM never gets to suppress a
  major or critical incident.
- **The optional LLM is read-only.** It investigates root causes, explains status
  changes, and de-noises non-urgent messages; it never executes a repair command.
- **The evidence panel is auditable.** Live service health, incident timelines,
  tool calls, and raw evidence remain inspectable.
- **Notifications are not platform-locked.** Feishu/Lark gets native rich cards,
  with Telegram, ntfy, and generic webhooks also supported.

Every layer can be enabled independently and fails safe when unconfigured. The
minimum footprint is Docker plus any one notification channel.

```
HTTP / Docker / Prometheus / Loki / vendor status pages
                         │
                         ▼
      deterministic rules → incident engine (cooldown / recovery / baselines)
                         │
             ┌───────────┴───────────┐
             ▼                       ▼
   hard alerts in real time        non-urgent 09:00 / 18:00 digests
             │                       │
             └───────────┬───────────┘
                         ▼
       optional read-only LLM investigation + live evidence panel
                         │
                         ▼
       Feishu/Lark cards / Telegram / ntfy / webhook
```

## Screenshots

> Both screenshots use synthetic demo data. No real server names, logs, or
> incidents are included.

![WatchMend live service overview](docs/images/watchmend-overview.png)

<p align="center"><sub>Live health, open incidents, recent trends, and service availability ordered by risk</sub></p>

![WatchMend read-only diagnosis evidence](docs/images/watchmend-diagnosis.png)

<p align="center"><sub>LLM root cause, confidence, read-only tool evidence, and suggested commands that are never auto-executed</sub></p>

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
| `services.yaml` | HTTP probes + latency baselines (per-entry optional `label` sets panel display name) | vendor-status-only mode |
| `SENTINEL_PROMETHEUS_URL` | disk/memory/restart metric rules | metrics layer off |
| `SENTINEL_LOKI_URL` | error-log spike detection | log layer off |
| `SENTINEL_ERROR_ALERT_ENABLED` | low-frequency error fingerprint detection | off by default |
| `SENTINEL_DEFER_NONURGENT` | aggregate non-hard alerts into 09:00/18:00 digests | real-time delivery |
| `SENTINEL_EDITOR_MODE` + editor endpoint/model | LiteLLM status-page explanation and low-risk gating | deterministic card unchanged |
| `SENTINEL_RESTIC_BACKUP_MAX_AGE_HOURS` | Restic Pushgateway freshness check | off by default |
| `SENTINEL_MIDDLEWARE_METRICS` | pg/redis exporter `up` fallback | check skipped |
| `SENTINEL_CERT_DOMAINS` | TLS certificate expiry check | check skipped |
| backup dir mount | pg_dump freshness check | check skipped |
| `LLM_BASE_URL` + `LLM_MODEL` | root-cause diagnosis + AI report summary | LLM layer off |
| `SENTINEL_DOCKER_HOST` | container down/unhealthy/OOM/crash-loop detection + docker diagnosis tools | docker layer off † |

> † Unlike the other layers, the docker layer is **on by default**: `docker-compose.yml`
> ships a read-only `docker-socket-proxy` sidecar (`CONTAINERS=1`, POST denied by
> default, dedicated `internal` network), and the WatchMend container **never mounts the
> bare socket**. To turn the whole layer off: clear `SENTINEL_DOCKER_HOST` and comment out
> the `docker-proxy` service plus sentinel's `depends_on`/`docker_proxy` network in compose
> (the file has inline notes).

> **Notification channels are a broadcast model**: every configured channel receives each alert/recovery/report/diagnosis concurrently; failures are isolated (a failing channel is only logged and does not affect the others). Two delivery semantics apply: **alerts/recoveries** use **send-then-commit** — the event is **committed only if at least one channel succeeds** (dedup/cooldown); if every channel fails nothing is committed and it is re-sent next round, so transient outages never drop events. **Diagnosis/report cards** are instead **persist-then-best-effort**: the underlying diagnosis result / report data is persisted first, and a broadcast failure does **not** roll back the persisted record (still visible in the evidence panel) nor re-send. Neither path retries a failed channel within a single broadcast. **At least one channel must be configured** (Feishu `FEISHU_VENDOR_WEBHOOK` or any of the above) to start; `FEISHU_VENDOR_WEBHOOK` is no longer mandatory — overseas self-hosters can run with Telegram/ntfy/webhook only.

All 40+ settings (thresholds, cooldowns, verbosity…) are documented inline in
[.env.example](.env.example).

## China / prebuilt-image deployment (turnkey)

Pick this over `make up` when you want the prebuilt [ghcr](https://github.com/HyxiaoGe/watchmend/pkgs/container/watchmend) image (no source build), you have **no** existing prometheus/loki/reverse-proxy docker networks, or you're **behind the GFW**. It uses a self-contained [`docker-compose.image.yml`](docker-compose.image.yml): image-based (no `build:`), no external `egress`/`metrics` networks, and the read-only docker-socket proxy swapped for a China-reachable same-source fork.

> You **still clone the repo** — only the image build is skipped, not the repo (the compose, `.env.example`, and `services.example.yaml` all live here).

**Step 0 — install Docker (China, if missing)**: `curl -fsSL https://get.docker.com | sh -s -- --mirror Aliyun` (plain `get.docker.com` fails on the GPG-key fetch from download.docker.com inside China, `curl` exit 35).

```bash
git clone https://github.com/HyxiaoGe/watchmend && cd watchmend
cp .env.example .env                      # fill AT LEAST ONE notification channel
cp services.example.yaml services.yaml    # your probe list, or set the services block to exactly `services: []` for vendor-status-only
mkdir -p data                             # SQLite persistence dir
docker compose -f docker-compose.image.yml up -d
curl http://127.0.0.1:8765/health
```

> **A channel is mandatory.** This path skips the Makefile gate, so running `docker compose up` with **zero channels in `.env` crash-loops** the container: the app asserts "at least one channel" during startup and `restart: unless-stopped` turns that into an infinite restart. A syntactically-valid-but-fake value boots (channels are checked for presence, not reachability), but delivery then fails and retries every minute (log noise) — double-check the token.

> **`services.yaml`**: edit it to your own probe list, or for vendor-status-only set the `services` block to `services: []` (clearest). A bare `services:` key, an empty file, or a missing file all **degrade safely to vendor-status-only** (no crash); only genuinely malformed config (e.g. a service entry missing the required `name`) fails loudly.

> **Image version**: `docker-compose.image.yml` hard-pins a specific `image:` tag (the repo never uses `:latest`). It may be stale by the time you read this — check the [releases page](https://github.com/HyxiaoGe/watchmend/releases) and bump the `image:` line to upgrade.

> **Docker layer** (on by default): the bundled read-only socket proxy is `lscr.io/linuxserver/socket-proxy`, not `tecnativa/docker-socket-proxy` (Docker Hub is blocked in China; `lscr.io` is reachable and the env interface — `CONTAINERS`/`POST`/`EVENTS`…, POST defaults to `0` = read-only — is identical). Two notes: the patrol auto-excludes the bundled socket proxy by image-name substring (both `tecnativa` and the `lscr` fork are covered), so no manual `SENTINEL_DOCKER_EXCLUDE` is needed — only set one if you swap in a different proxy image whose name lacks those substrings; to turn the layer off, follow the inline comments and clear `SENTINEL_DOCKER_HOST`.

> **LLM diagnosis** in this path is enabled via `.env` `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` (DeepSeek `api.deepseek.com` / Moonshot CN `api.moonshot.cn` are the natural picks behind the GFW; OpenAI/Anthropic/Gemini direct-connect is unreachable there). The `llm.yaml` registry + `make llm-init` hot-reload flow is for the `make up` build path; this compose deliberately does not mount `llm.yaml`. See [`## LLM diagnosis (optional)`](#llm-diagnosis-optional).

> **Access**: the port is bound to `127.0.0.1` only (zero public exposure). On the box itself open `http://127.0.0.1:8765` directly; for a remote box tunnel first — `ssh -L 8765:127.0.0.1:8765 you@your-server`, then open `http://127.0.0.1:8765` locally. Before exposing it (LAN bind / reverse proxy), set `SENTINEL_DIAG_TOKEN` — see [`## Evidence Panel (read-only)`](#evidence-panel-read-only).

## LLM diagnosis (optional)

**Not locked to one platform.** WatchMend speaks the standard OpenAI
`chat/completions` + function-calling protocol, so any OpenAI-compatible endpoint is
plug-and-play. Multiple providers coexist in `llm.yaml`, selected by the `active`
pointer; edits **hot-reload on the next diagnosis round — no restart**:

```yaml
# llm.yaml (gitignored; see llm.example.yaml)
active: deepseek       # current diagnoser
fallback: kimi         # optional: tried once after active fails (event diagnosis AND daily summary)
providers:
  deepseek:
    base_url: https://api.deepseek.com/v1
    model: deepseek-chat
    api_key_env: LLM_API_KEY_DEEPSEEK   # real key stays in env, never on disk
  kimi:
    base_url: https://api.moonshot.cn/v1
    model: kimi-k2-turbo-preview
    api_key_env: LLM_API_KEY_KIMI
```

```bash
make llm-init              # interactive wizard to add a provider
make llm-switch name=kimi  # switch active (next round, no restart)
make llm-list              # show providers and whether keys are ready
```

> **Docker deployment**: `llm.yaml` is bind-mounted into the container via compose
> (`./llm.yaml:/app/llm.yaml:ro`), and `make up`/`make demo` scaffold a blank placeholder
> for you. Running `make llm-init` (add a provider first) / `make llm-switch` (then switch
> active) on the host edits the very file the container reads. **Once the LLM is enabled**,
> switching provider/model **hot-reloads on the next diagnosis round — no exec into the
> container, no restart**; but **enabling diagnosis from scratch** over the blank placeholder
> still needs one container restart (the diagnosis job is registered at startup by whether
> the LLM is enabled — see the next note).

> **Backward compatible**: with no `llm.yaml` (or a blank/comments-only one, like the
> placeholder `make up` scaffolds), it falls back to the legacy three env vars
> `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` (zero breaking). Bad config is always
> fail-safe: a broken config at startup only disables the LLM layer (deterministic
> patrol keeps running); breaking `llm.yaml` while running keeps the last good config
> and never interrupts monitoring. **Enabling the LLM from scratch needs one restart**;
> after that, switching provider/model is hot-reloaded.

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

## Operations panel (read-only evidence)

WatchMend ships a read-only SSR panel that listens on localhost by default and
turns its four disciplines (deterministic verdicts / assessment ≠ recovery /
send-then-commit / read-only never-execute) into visible operational evidence:

- **Live service overview**: current health, availability, latency trends, and recent incidents; recently troubled services stay prominent and probe age updates continuously.
- **State-machine timeline**: currently open anomalies (triggered → investigating → diagnosed) plus recoveries in the last 24h; `scan_failed_*` is explicitly marked as a data-source failure, not a green "recovered" card.
- **Diagnosis evidence chain**: for each diagnosed event you can expand the **read-only tools the LLM actually called and their raw output snippets** (`docker_inspect`/`docker_logs`/`prom_query`/`loki_logs`) — hard proof of the read-only, push-diagnosis loop.
- **Hygiene and security posture**: backups, disk, certificates, read-only socket state, credential-redaction counts, enabled layers, and notification channels in one place.

Access: the container binds the panel to `127.0.0.1:8765` on the host (same port as the orchestration API); open `http://127.0.0.1:8765/`.

**Security note**: all panel GET routes (overview, services, events, hygiene,
settings, and changelog) are **read-only**, but the **same `127.0.0.1:8765`
port also hosts the orchestration WRITE API** (`POST /events/{id}/diagnosis`,
`POST /report/summary`). Those endpoints are authenticated only when
`SENTINEL_DIAG_TOKEN` is set; it defaults to empty, i.e. **no auth**. The whole
service therefore assumes localhost/intranet reachability. Raw log snippets are
stored in the localhost-only SQLite (the same data already sent to the LLM
endpoint, truncated to 4096 characters each). **To expose it publicly**, protect
the entire 8765 upstream with an authenticated reverse proxy, or allow only the
panel GET routes while blocking the write API, **and set
`SENTINEL_DIAG_TOKEN`**. Alternatively, set `SENTINEL_PANEL_ENABLED=false`.

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

**Why does Feishu get rich cards?** Feishu was the project's first channel and
has the most complete card ecosystem, so it receives native interactive cards;
Telegram / ntfy receive rendered text and the generic webhook receives structured
JSON (for machine consumption). Configure one or more channels and every alert
broadcasts to all of them (see the config table above). More channels
(Slack / Discord, …) are welcome via PR.

**vs. Uptime Kuma / Gatus?** Those are probes + status pages. WatchMend focuses
on the rule engine + incident lifecycle (cooldown/recovery/baselines) + LLM
root-cause diagnosis, and treats your existing Prometheus / Loki as data sources
instead of rebuilding collection.

**vs. Alertmanager?** Not a replacement. If you already run a full observability
stack with mature alerting rules you may not need this. WatchMend serves the
"one server, a dozen containers, want monitoring that works out of the box and
diagnoses I can actually read" scenario.

**Will the LLM mess with my server?** See the security boundaries above: every
tool is read-only, Docker is reachable only through a socket proxy that denies
POST by default, and suggested commands are never executed. The deterministic
layers do not depend on the LLM at all.

## Development

```bash
uv sync --dev
make check        # ruff + pytest + leak check
```

## License

[MIT](LICENSE)
