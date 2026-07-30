# Changelog

All notable changes to WatchMend are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Versioning policy and release process: see [RELEASING.md](RELEASING.md).

中文版变更日志见 [CHANGELOG.zh-CN.md](CHANGELOG.zh-CN.md)。

## [Unreleased]

### Added
- Optional deferred-notification policy: hard alerts remain immediate while
  non-urgent findings are aggregated into the 09:00 report and 18:00 digest.
- A separate LiteLLM-compatible upstream status editor with `shadow`, `enrich`
  and `gate` modes. Major/critical events can never be suppressed, failures
  always fall back to deterministic cards, and every analysis is audited.
- Optional Loki error-fingerprint scanning and Restic backup-freshness checks.
- A side-effect-free `shadow` notification mode for parallel migration checks.

### Changed
- Loki spike detection now counts explicit error-level records instead of broad
  `error|exception|traceback` substring matches and excludes WatchMend itself.
- Service lists now keep current state as the primary sort key and use the most
  recent incident as the secondary key, so recently recovered services remain
  visible instead of falling back into an alphabetical all-green list.
- Overview, service, event, hygiene, and detail pages now share the same
  in-place live refresh contract. The header also shows a second-by-second age
  for the latest service probe.

### Fixed
- New error fingerprints are grouped and filtered by container in the event
  feed, with ANSI escapes stripped from concise previews; event details prefer
  diagnoses already persisted in the database.
- The hygiene page now distinguishes enabled layers from those disabled by
  policy and shows the status editor's actual runtime posture.

## [0.15.1] - 2026-06-18

### Changed
- The **Settings** page configuration inventory was redesigned for readability.
  Each row now leads with a human-friendly name and a one-line description, and
  the raw environment-variable name is demoted to a small chip beneath it.
  Settings are grouped into collapsible sections (only the first is open by
  default), and the active / fallback diagnosis model is shown with the provider
  as a chip and the model as the value. The nine credential settings keep their
  configured / not-configured status only — their values are still never rendered.

### Fixed
- **Settings** page accessibility: the display-preference chips now show a
  keyboard focus ring, and the "configured" status and tag colours meet WCAG AA
  contrast in the light theme.

## [0.15.0] - 2026-06-18

### Added
- New **Settings** page at `/settings`, reachable from the header gear (⚙). It
  has two read-only sections and works fully with JavaScript off:
  - **Display preferences** — choose the panel language, theme, history window,
    and auto-refresh interval. Choices are saved per-browser as cookies through
    a plain GET form; selecting "auto" / "default" clears that preference and
    reverts to the server default.
  - **Configuration inventory** — every setting listed by its environment-variable
    name, grouped by area, with its effective value. The nine credential settings
    (tokens, keys, sign secrets, webhooks) show a configured / not-configured
    status only — their values are never rendered.
- The panel auto-refresh interval is now overridable per browser from the
  Settings page on the live pages (overview, services, events, hygiene); detail
  and changelog pages stay static as before. Setting it to "off" stops
  auto-refresh for that browser.

## [0.14.0] - 2026-06-17

### Changed
- Panel auto-refresh is now flicker-free. With JavaScript enabled, a tiny inline
  script refreshes the page content in place — swapping only the main region and
  syncing the title and status line — instead of doing a full-page reload, so the
  scroll position and any open changelog modal are preserved and the page no
  longer blinks every interval. With JavaScript disabled it falls back to the
  previous full-page `<meta refresh>` via `<noscript>`, so nothing is lost.
- The long-standing "zero JavaScript" rule for the panel is now a bounded
  progressive-enhancement principle (documented in `CONTRIBUTING.md`): the panel
  must work fully with JS off, no framework/bundler/build/external JS is allowed,
  and only tiny inline vanilla scripts are permitted.

### Added
- New optional `SENTINEL_PANEL_REFRESH_SECONDS` setting (default 30, minimum 5)
  controls the panel refresh cadence for both the live poller and the
  `<noscript>` fallback. Old images ignore it, so it rolls back cleanly.

## [0.13.3] - 2026-06-17

### Changed
- The panel header now shows just the WatchMend wordmark — the logo carries the
  identity, so the "Evidence Panel" descriptor was dropped.
- The browser tab title is now page-aware: `{page} · WatchMend` (Overview /
  Services / Events / Hygiene / Changelog), so pinned or background tabs are
  distinguishable. It gains a status-light prefix `🔴 {n} · ` that appears only
  when there are open incidents — quiet when healthy, red when not — reusing the
  same open-incident signal as the status badge. The previous update-available
  `●` title marker was removed; update availability still shows in the version
  chip (dot + modal banner).

## [0.13.2] - 2026-06-17

### Added
- The panel header now shows the WatchMend brand mark — an inline "WM pulse" SVG
  that links home — next to the title, plus a matching SVG favicon. Both are
  inline (zero JavaScript, no external request, no `<img>`) and add no new route
  or static mount, so they ship with the image and roll back cleanly.

## [0.13.1] - 2026-06-17

### Changed
- The header version chip now opens the changelog as a centered, zero-JS modal
  (a `:target` overlay with a dimmed backdrop and click-outside / × to close)
  instead of the small dropdown that linked out to a separate page. The full
  release history is embedded inline and scrollable, the running version is
  highlighted, and an update banner sits on top when a newer release exists; the
  standalone `/changelog` page is kept as a shareable permalink.

## [0.13.0] - 2026-06-17

### Changed
- The header version chip is now a click-to-open changelog popover instead of a
  static-looking link. The badge became a zero-JS disclosure that shows the
  running version's "What's new" inline (with a caret affordance and hover
  state) plus a "Full changelog" link, and the full `/changelog` page gained a
  breadcrumb back to the overview.

## [0.12.1] - 2026-06-17

### Security
- Log-redaction ReDoS hardening: the connection-string pattern in the diagnosis
  log redactor was quadratic on a long, unbroken `scheme://user:pass` run with no
  closing `@`, so an adversarial log line fed through `loki_logs` / `docker_logs`
  could stall the event loop for seconds. All three segments (scheme / user /
  password) are now length-bounded, making redaction linear; real connection
  strings are unaffected.

## [0.12.0] - 2026-06-17

### Added
- Event ↔ service navigation: the event stream links each event to its service
  detail page, and service detail links back to its filtered event list —
  two-way drill-down without losing the active window / filter context.

### Fixed
- Event timestamps now carry the date instead of a bare `HH:MM`, so older events
  read unambiguously.
- Docker restart action: container matching now lands the `docker ps` output
  before matching, eliminating a `pipefail` / `SIGPIPE` race that could
  spuriously reject a valid restart.

### Security
- Diagnosis log redaction: the container / Loki log output fed to the LLM
  diagnosis tools (`docker_logs` / `loki_logs`) is now scrubbed for secrets
  before it leaves the network and before it is stored in the evidence chain or
  shown in the panel. Two tiers: exact-match of WatchMend's own and the target
  container's own secret values (which are themselves never emitted), plus
  conservative shape patterns (API keys, JWTs, Bearer / Basic credentials,
  `key=value` / JSON assignments, connection-string passwords, PEM private-key
  blocks). The model and the evidence chain receive the same redacted text.

## [0.11.1] - 2026-06-17

### Added
- China / prebuilt-image deployment: a standalone `docker-compose.image.yml` that
  pulls the prebuilt `ghcr.io/hyxiaoge/watchmend` image (no local build) and bundles
  a read-only `lscr.io/linuxserver/socket-proxy` sidecar — both reachable in China,
  where Docker Hub is blocked — plus a turnkey deploy section in the README.
- Release guard: `tests/test_compose_image.py` pins the image tag in
  `docker-compose.image.yml` to the `pyproject.toml` version.

### Fixed
- `services.yaml` parsing: an empty file, a bare `services:`, or a missing
  `services` key now degrades to vendor-status-only instead of crash-looping the
  container; a non-list `services` value still fails loudly.
- Docker patrol self-exclusion now also matches the `linuxserver/socket-proxy`
  (lscr) fork, so the bundled socket-proxy sidecar is no longer mis-reported as a
  down container when using the prebuilt-image compose.

## [0.11.0] - 2026-06-16

### Added
- Changelog panel: a zero-JS `/changelog` page renders this build's release
  notes (and full history) offline. The bilingual changelog is baked into the
  image, so the panel works without network access. The version pill links here.
- Update marker: when a newer release is available, the browser tab title gets a
  `●` prefix, so a backgrounded tab surfaces the update on its next refresh.

## [0.10.1] - 2026-06-16

### Added
- Versioning policy, bilingual changelog, and release-process docs:
  `CHANGELOG.md` / `CHANGELOG.zh-CN.md` (backfilled 0.1.0–0.10.0) and `RELEASING.md`.
- Changelog parity guard test (`tests/test_changelog.py`).

## [0.10.0] - 2026-06-16

### Added
- Zero-JS field-glossary tooltips: hover or keyboard-focus any metric
  (today-uptime, rolling window mean 7/30/90d, MTTR, p95, baseline, threshold,
  confidence) for an inline explanation. Pure CSS `:hover` / `:focus-within`
  popover, no JavaScript.

### Removed
- Static trust footer (wording was too broad and `localhost-only` was inaccurate
  on LAN-exposed deployments).

## [0.9.1] - 2026-06-16

### Changed
- Slimmed status line: time-only refresh indicator and a neutral AI-status marker.

## [0.9.0] - 2026-06-16

### Added
- Dozzle-style two-row navigation shell: title + tool cluster (version pill ·
  window · theme · language) on row 1; inline-SVG icon nav
  (overview / services / events / hygiene) + read-only marker on row 2.
- Always-on version pill, top-right.
- Update check (on by default): background poll of GitHub releases; a newer
  version lights an amber dot on the version pill with a CSS popover giving the
  upgrade command and release link. Never self-updates (honors pinned tags).
  Disable via `SENTINEL_UPDATE_CHECK_ENABLED=false` or an empty URL.

## [0.8.0] - 2026-06-16

### Changed
- Overview redesigned as an SLO dashboard: deduplicated status ring (segments =
  today's four-state split, center = today-uptime in threshold color), KPIs
  beside the ring (N/M services OK · 7/30/90d rolling means with Δ arrows · open
  events by severity · 24h net flow · MTTR), a worst-first service table with
  today / 7d / 30d uptime, and a summary row.

### Fixed
- SLO windows decoupled from the probe-bar window, so d90 truly covers 90 days at
  the default window=30 (previously clamped to a d30 clone); renders `–` honestly
  when history is insufficient.

## [0.7.0] - 2026-06-16

### Changed
- Overview page (`/`) condensed into a pure dashboard: HERO (status ring +
  today-uptime + open-anomaly count + trend line), a one-line "needs attention"
  strip (open alert events; "✓ all clear" when none), and three summary cards
  (services / hygiene / daily report) linking to their detail pages. Detail lives
  in the existing `/services`, `/events`, and `/hygiene` pages (no feature loss).

### Removed
- Dead `services-cap` config and leftover dead code / orphan i18n keys.

## [0.6.0] - 2026-06-16

### Added
- Panel redesign Phase 2 — four zero-JS, server-side-rendered subpages:
  `/services` (per-service p95 sparkline, worst-first), `/service/{name}`
  (p50/p95 latency chart, threshold comparison, status-code buckets, uptime
  heatmap, related events), `/events` (service / severity / status filters), and
  a rewritten `/event/{id}` (confidence-ring HERO + read-only evidence-chain
  timeline). `/hygiene` shows event-driven three-state local checks + upstream
  dependencies + sentinel self-posture.

### Security
- Upstream statuspage `incident.shortlink` (external JSON) passes through an
  http(s) scheme allowlist in the view layer, preventing `javascript:` / `data:`
  URLs from reaching an `href`.

## [0.5.0] - 2026-06-16

### Added
- Panel redesign Phase 1: a design-system foundation (tokens + component CSS,
  GitHub-dark palette with dark / light / system themes), a 4-tab nav shell, a
  HERO card (inline-SVG status ring, big today-uptime number, overall trend
  line), per-service mini sparklines, an embeddable shields-style `/badge.svg`
  (panel-gated, autoescaped), and zh/en i18n.

### Changed
- All new visuals are pure functions over existing reads — additive only, no new
  tables, migrations, or store writes.

## [0.4.0] - 2026-06-15

### Added
- Optional per-service display `label` (`services.yaml`): the panel shows
  `label or name` while `name` stays the stable DB key. Zero persistence, zero
  schema change, fully backward compatible (no label = previous behavior).

## [0.3.2] - 2026-06-15

### Added
- `container_crashloop` detection for docker-only deployments (no Prometheus):
  a tumbling-window check over `RestartCount` across scan ticks — ≥ N restarts
  within window W emits a point alert card with root-cause diagnosis. The
  baseline persists in a new additive table `container_restart_baseline`
  (survives WatchMend's own restarts) and stands down under the
  `metrics_covering` gate when prom + cadvisor already cover it (no double
  alerts). New optional config `SENTINEL_DOCKER_CRASHLOOP_WINDOW` (default 600s)
  and `SENTINEL_DOCKER_CRASHLOOP_THRESHOLD` (default 3).

## [0.3.1] - 2026-06-14

### Changed
- Evidence-panel information-density pass (issue #11): default window 90→30 with
  no-data rows de-emphasized; health list sorted worst-first showing the top 6
  with an "expand remaining N"; banner split into a primary status line + a
  compact metrics row; detail / latest / back links now carry `lang/theme/win`;
  and a note appears when the UI language differs from the diagnosis-generation
  language.

## [0.3.0] - 2026-06-14

### Added
- Evidence-panel redesign: interface i18n (zh/en for headers, legend, status,
  rule names; priority query > cookie > configured default > Accept-Language;
  `SENTINEL_PANEL_DEFAULT_LANG` forces a language), theme switch
  (dark / light / system via CSS variables + `prefers-color-scheme`, stored in a
  cookie), per-service health bars (five-state machine, nodata-first, missing
  uptime never collapses to down), host & self introspection, a paginated event
  stream with inline AI diagnosis summaries, and an event detail page
  (`/event/{id}`) with a diagnosis hero + tool-call chain. Diagnosis language
  follows `SENTINEL_LLM_LANG`.

### Changed
- Synced `pyproject.toml` + `uv.lock` to 0.3.0 (cleared lock drift that had
  stalled at 0.1.1).

## [0.2.0] - 2026-06-14

### Added
- First public milestone, completing four capability areas on top of the 0.1.x
  detection / notification core:
  - **Environment auto-discovery + socket safety**: zero-config detection of
    monitored containers via a read-only docker API (socket-proxy); suggested
    commands are never auto-executed.
  - **Multi-channel notifications**: Feishu rich cards + Telegram + ntfy +
    generic webhook; the first channel to succeed commits the event; a startup
    gate blocks a zero-channel configuration.
  - **Read-only evidence panel** (`:8765`): state-machine visualization
    (analyzed vs recovered), diagnosis evidence-chain capture, env-value
    redaction, HTML escaping; write endpoints protected by `SENTINEL_DIAG_TOKEN`.
  - **Declarative LLM config** (`llm.yaml`): active/fallback failover,
    `api_key_env` stores only the variable name (never the key), mtime
    hot-reload without restart, and fallback to legacy `LLM_*` env when
    absent/empty (zero breaking); diagnosis and daily-report summaries share the
    same failover path.

### Changed
- Internal package name `sentinel` and `SENTINEL_*` / `FEISHU_*` / `LLM_*` env
  prefixes retained — upgrading from 0.1.x needs no env changes.

## [0.1.1] - 2026-06-13

### Fixed
- Clean-VM onboarding (two issues caught in fresh-VM acceptance): bumped the demo
  cadvisor image v0.49.1→v0.55.1 so container metrics work on Docker 29 (API
  1.44+, where older cadvisor's docker factory fails to register and metrics come
  back empty), and gated the disk-forecast hygiene check behind a 24h-history
  probe so a brand-new machine's install / image-pull write slope no longer
  extrapolates into a false "disk filling up" alert (treated as unevaluated until
  enough history exists).

## [0.1.0] - 2026-06-12

### Added
- Initial open-source release of WatchMend (renamed from the internal
  dev-ops-sentinel). Core monitor: external status-page probes, Feishu
  alerts / recovery, optional LLM root-cause diagnosis, hygiene checks, and a
  read-only evidence panel. Published as a container image on
  `ghcr.io/hyxiaoge/watchmend` via a CI release workflow.

### Security
- Secret-leak redaction across docker metadata and environment values.

[Unreleased]: https://github.com/HyxiaoGe/watchmend/compare/v0.15.1...HEAD
[0.15.1]: https://github.com/HyxiaoGe/watchmend/compare/v0.15.0...v0.15.1
[0.15.0]: https://github.com/HyxiaoGe/watchmend/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/HyxiaoGe/watchmend/compare/v0.13.3...v0.14.0
[0.13.3]: https://github.com/HyxiaoGe/watchmend/compare/v0.13.2...v0.13.3
[0.13.2]: https://github.com/HyxiaoGe/watchmend/compare/v0.13.1...v0.13.2
[0.13.1]: https://github.com/HyxiaoGe/watchmend/compare/v0.13.0...v0.13.1
[0.13.0]: https://github.com/HyxiaoGe/watchmend/compare/v0.12.1...v0.13.0
[0.12.1]: https://github.com/HyxiaoGe/watchmend/compare/v0.12.0...v0.12.1
[0.12.0]: https://github.com/HyxiaoGe/watchmend/compare/v0.11.1...v0.12.0
[0.11.1]: https://github.com/HyxiaoGe/watchmend/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/HyxiaoGe/watchmend/compare/v0.10.1...v0.11.0
[0.10.1]: https://github.com/HyxiaoGe/watchmend/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/HyxiaoGe/watchmend/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/HyxiaoGe/watchmend/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/HyxiaoGe/watchmend/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/HyxiaoGe/watchmend/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/HyxiaoGe/watchmend/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/HyxiaoGe/watchmend/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/HyxiaoGe/watchmend/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/HyxiaoGe/watchmend/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/HyxiaoGe/watchmend/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/HyxiaoGe/watchmend/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/HyxiaoGe/watchmend/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/HyxiaoGe/watchmend/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/HyxiaoGe/watchmend/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/HyxiaoGe/watchmend/releases/tag/v0.1.0
