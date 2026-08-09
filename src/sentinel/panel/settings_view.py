"""设置页 view-model（纯函数，无 Request 依赖，便于单测）。
build_config_inventory：按组列出 Settings 全字段（env 变量名为项标识），
所有密钥字段只产出"已配置/未配置"状态、绝不出值；非密钥值过 redact_text 兜底。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sentinel.config import Settings
from sentinel.redact import redact_text

if TYPE_CHECKING:
    from sentinel.llm_config import LLMConfig, LLMProfile

# 密钥字段:只显状态,永不出值。(必须覆盖全部敏感字段)
# 注:feishu_*_webhook 把签名 token 嵌在 URL path 里,故计入密钥;
# sentinel_webhook_url / sentinel_ntfy_url 是裸端点,其 Bearer 另存于
# *_token 字段(已计入),故 URL 本身非密钥(仍过 redact 兜底)。
_SECRETS: frozenset[str] = frozenset(
    {
        "feishu_vendor_webhook",
        "feishu_vendor_sign_secret",
        "feishu_patrol_webhook",
        "feishu_patrol_sign_secret",
        "llm_api_key",
        "sentinel_telegram_bot_token",
        "sentinel_ntfy_token",
        "sentinel_webhook_token",
        "sentinel_diag_token",
        "sentinel_codex_ingest_token",
        "sentinel_editor_api_key",
    }
)

# 分组（顺序即展示顺序）。覆盖 Settings 全部字段——test 守护集合相等。
_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "probe",
        (
            "sentinel_providers",
            "sentinel_poll_interval",
            "sentinel_incident_verbosity",
            "sentinel_fail_threshold",
            "sentinel_provider_card_min_gap_s",
            "sentinel_probe_interval",
            "sentinel_services_file",
            "sentinel_probe_retention_days",
            "sentinel_cooldown_hours",
            "sentinel_probe_fail_streak",
            "sentinel_latency_ratio",
            "sentinel_latency_margin_ms",
            "sentinel_latency_min_samples",
            "sentinel_log_spike_ratio",
            "sentinel_log_spike_min",
            "sentinel_error_alert_enabled",
            "sentinel_error_alert_interval",
            "sentinel_error_window_minutes",
            "sentinel_error_max_per_cycle",
            "sentinel_error_query_limit",
            "sentinel_error_containers",
            "sentinel_error_ignore_patterns",
            "sentinel_self_container",
            "sentinel_disk_usage_pct",
            "sentinel_disk_forecast_days",
            "sentinel_container_mem_pct",
            "sentinel_swap_pct",
            "sentinel_middleware_metrics",
        ),
    ),
    (
        "datasource",
        (
            "sentinel_prometheus_url",
            "sentinel_loki_url",
            "sentinel_scan_interval",
            "sentinel_scan_fail_threshold",
        ),
    ),
    (
        "channels",
        (
            "sentinel_notification_mode",
            "feishu_vendor_webhook",
            "feishu_vendor_sign_secret",
            "feishu_patrol_webhook",
            "feishu_patrol_sign_secret",
            "sentinel_telegram_bot_token",
            "sentinel_telegram_chat_id",
            "sentinel_ntfy_url",
            "sentinel_ntfy_token",
            "sentinel_webhook_url",
            "sentinel_webhook_token",
            "sentinel_heartbeat_enabled",
            "sentinel_heartbeat_hour",
            "sentinel_heartbeat_utc_offset",
            "sentinel_report_hour",
            "sentinel_evening_digest_hour",
            "sentinel_defer_nonurgent",
        ),
    ),
    (
        "llm",
        (
            "llm_base_url",
            "llm_api_key",
            "llm_model",
            "llm_timeout_seconds",
            "llm_max_tool_rounds",
            "sentinel_diag_interval",
            "sentinel_llm_config_file",
            "sentinel_llm_lang",
            "sentinel_editor_mode",
            "sentinel_editor_base_url",
            "sentinel_editor_api_key",
            "sentinel_editor_model",
            "sentinel_editor_timeout_seconds",
        ),
    ),
    (
        "docker",
        (
            "sentinel_docker_socket",
            "sentinel_docker_host",
            "sentinel_docker_scan_interval",
            "sentinel_docker_crashloop_window",
            "sentinel_docker_crashloop_threshold",
            "sentinel_docker_exclude",
        ),
    ),
    (
        "panel",
        (
            "sentinel_panel_enabled",
            "sentinel_panel_default_lang",
            "sentinel_panel_default_theme",
            "sentinel_panel_history_days",
            "sentinel_panel_default_window",
            "sentinel_panel_refresh_seconds",
            "sentinel_panel_page_size",
            "sentinel_panel_red_uptime_pct",
            "sentinel_panel_partial_uptime_pct",
            "sentinel_panel_green_uptime_pct",
            "sentinel_panel_overview_roster_cap",
            "sentinel_event_feed_days",
            "sentinel_update_check_enabled",
            "sentinel_update_check_url",
            "sentinel_update_check_interval",
        ),
    ),
    (
        "backup_cert",
        (
            "sentinel_backup_dir",
            "sentinel_backup_max_age_hours",
            "sentinel_restic_backup_max_age_hours",
            "sentinel_cert_domains",
            "sentinel_cert_min_days",
        ),
    ),
    (
        "security",
        (
            "sentinel_db_path",
            "sentinel_diag_token",
            "sentinel_codex_ingest_token",
            "sentinel_codex_receipt_retention_days",
        ),
    ),
)

# 字段友好名 + 一句话说明（中/英）。键=字段名（与 _GROUPS 对齐；test 守护全覆盖）。
# 友好名做主标题、说明做副行、原始 ENV 变量名降级为小字 chip——不再拿 SENTINEL_* 当标题。
# 与字段目录同处（而非塞进 i18n.MESSAGES）：这是和 _GROUPS 强耦合的数据表，加字段时一处补全。
_FIELD_META: dict[str, dict[str, tuple[str, str]]] = {
    # —— probe：探测与阈值 ——
    "sentinel_providers": {
        "zh": ("状态页厂商", "纳入外部状态页监控的上游厂商，逗号分隔"),
        "en": ("Status-page vendors", "Upstream vendors whose status pages are watched (CSV)"),
    },
    "sentinel_poll_interval": {
        "zh": ("状态页轮询间隔", "拉取外部状态页的周期（秒）"),
        "en": ("Status-page poll interval", "How often vendor status pages are fetched (seconds)"),
    },
    "sentinel_incident_verbosity": {
        "zh": ("事件详述粒度", "状态页事件卡片的叙述详细度"),
        "en": ("Incident verbosity", "Narrative detail of status-page incident cards"),
    },
    "sentinel_fail_threshold": {
        "zh": ("状态页判障阈值", "连续失败几次判厂商异常并发卡"),
        "en": ("Status-page fail threshold", "Consecutive failures before a vendor is flagged"),
    },
    "sentinel_provider_card_min_gap_s": {
        "zh": ("上游卡片最小间隔", "同一厂商非紧急状态卡的最小发送间隔（秒）"),
        "en": ("Provider card min gap", "Minimum gap between non-urgent cards per vendor (sec)"),
    },
    "sentinel_probe_interval": {
        "zh": ("内部探针周期", "对 services.yaml 内部服务健康探测的周期（秒）"),
        "en": ("Internal probe interval", "Health-probe period for internal services (sec)"),
    },
    "sentinel_services_file": {
        "zh": ("探针清单文件", "内部探针的服务清单 YAML 路径"),
        "en": ("Services file", "Path to the internal-probe service list (YAML)"),
    },
    "sentinel_probe_retention_days": {
        "zh": ("探针数据留存", "探针采样在库中保留的天数"),
        "en": ("Probe retention", "Days of probe samples kept in the database"),
    },
    "sentinel_cooldown_hours": {
        "zh": ("事件冷却时长", "同一（规则·对象）再次告警的冷却（小时）"),
        "en": ("Event cooldown", "Re-alert cooldown for the same rule·subject (hours)"),
    },
    "sentinel_probe_fail_streak": {
        "zh": ("探针判障连败数", "内部探针连续失败几次判服务异常"),
        "en": ("Probe fail streak", "Consecutive probe failures before a service is down"),
    },
    "sentinel_latency_ratio": {
        "zh": ("延迟告警倍率", "1h p95 超七日基线多少倍触发慢响应"),
        "en": ("Latency alert ratio", "1h p95 over baseline by this factor triggers an alert"),
    },
    "sentinel_latency_margin_ms": {
        "zh": ("延迟告警余量", "且需超基线的绝对毫秒数（与倍率取大）"),
        "en": ("Latency alert margin", "Absolute ms over baseline also required (max of the two)"),
    },
    "sentinel_latency_min_samples": {
        "zh": ("延迟最少样本", "样本不足此数不做延迟判定，防误报"),
        "en": ("Latency min samples", "Skip latency checks below this sample count"),
    },
    "sentinel_log_spike_ratio": {
        "zh": ("日志激增倍率", "错误日志超基线多少倍判激增"),
        "en": ("Log spike ratio", "Error logs over baseline by this factor count as a spike"),
    },
    "sentinel_log_spike_min": {
        "zh": ("日志激增下限", "错误条数绝对下限，基线为 0 时防误报"),
        "en": ("Log spike floor", "Absolute error-count floor to avoid false spikes"),
    },
    "sentinel_error_alert_enabled": {
        "zh": ("新错误指纹开关", "启用低频/单条错误指纹扫描"),
        "en": ("New-error scanner", "Enable low-frequency error fingerprint scanning"),
    },
    "sentinel_error_alert_interval": {
        "zh": ("错误指纹扫描周期", "新错误指纹扫描间隔（秒）"),
        "en": ("Error scan interval", "Error fingerprint scan interval (seconds)"),
    },
    "sentinel_error_window_minutes": {
        "zh": ("错误回看窗口", "每轮错误指纹扫描回看的分钟数"),
        "en": ("Error lookback window", "Minutes inspected by each fingerprint scan"),
    },
    "sentinel_error_max_per_cycle": {
        "zh": ("单轮错误上限", "每轮最多接纳的新错误指纹数"),
        "en": ("Errors per cycle", "Maximum new fingerprints admitted per cycle"),
    },
    "sentinel_error_query_limit": {
        "zh": ("错误日志查询上限", "单轮从 Loki 拉取的最大日志行数"),
        "en": ("Error query limit", "Maximum Loki log lines fetched per scan"),
    },
    "sentinel_error_containers": {
        "zh": ("错误扫描容器", "纳入错误指纹扫描的容器正则"),
        "en": ("Error containers", "Container regex included in error scanning"),
    },
    "sentinel_error_ignore_patterns": {
        "zh": ("错误静默词", "命中指纹即忽略的大小写无关子串 CSV"),
        "en": ("Error ignore patterns", "Case-insensitive fingerprint substrings to ignore (CSV)"),
    },
    "sentinel_self_container": {
        "zh": ("自身容器名", "从日志扫描排除，避免监控反馈环"),
        "en": ("Self container", "Excluded from log scans to prevent feedback loops"),
    },
    "sentinel_disk_usage_pct": {
        "zh": ("磁盘水位阈值", "磁盘占用超此百分比告警"),
        "en": ("Disk usage threshold", "Alert when disk usage exceeds this percent"),
    },
    "sentinel_disk_forecast_days": {
        "zh": ("磁盘填满预测窗", "按趋势预测此天数内填满则告警"),
        "en": ("Disk fill forecast", "Alert if disk is projected to fill within this many days"),
    },
    "sentinel_container_mem_pct": {
        "zh": ("容器内存阈值", "容器内存占用超此百分比告警"),
        "en": ("Container memory threshold", "Alert when container memory exceeds this percent"),
    },
    "sentinel_swap_pct": {
        "zh": ("Swap 阈值", "Swap 使用超此百分比告警"),
        "en": ("Swap threshold", "Alert when swap usage exceeds this percent"),
    },
    "sentinel_middleware_metrics": {
        "zh": ("中间件存活指标", "兜底 up 指标 CSV（metric:展示名），空=跳过"),
        "en": ("Middleware up-metrics", "Fallback up-metric CSV (metric:label); empty = skip"),
    },
    # —— datasource：数据源 ——
    "sentinel_prometheus_url": {
        "zh": ("Prometheus 地址", "接入 Prometheus 的 base URL，空=不接"),
        "en": ("Prometheus URL", "Prometheus base URL; empty = not connected"),
    },
    "sentinel_loki_url": {
        "zh": ("Loki 地址", "接入 Loki 的 base URL，空=不接"),
        "en": ("Loki URL", "Loki base URL; empty = not connected"),
    },
    "sentinel_scan_interval": {
        "zh": ("巡检扫描周期", "指标/日志巡检扫描周期（秒）"),
        "en": ("Scan interval", "Metrics / log scan period (seconds)"),
    },
    "sentinel_scan_fail_threshold": {
        "zh": ("数据源判障阈值", "数据源连续失败几次才发巡检失败卡"),
        "en": ("Scan fail threshold", "Consecutive data-source failures before a scan-fail card"),
    },
    # —— channels：通知渠道 ——
    "sentinel_notification_mode": {
        "zh": ("通知运行模式", "live=真实投递；shadow=只记日志不外发"),
        "en": ("Notification mode", "live = deliver; shadow = log only"),
    },
    "feishu_vendor_webhook": {
        "zh": ("飞书·业务群 Webhook", "状态页/心跳推送的飞书机器人地址"),
        "en": ("Feishu vendor webhook", "Feishu bot webhook for status-page / heartbeat pushes"),
    },
    "feishu_vendor_sign_secret": {
        "zh": ("飞书·业务群签名密钥", "业务群机器人的加签密钥（如启用）"),
        "en": ("Feishu vendor sign secret", "Signing secret for the vendor-group bot (if enabled)"),
    },
    "feishu_patrol_webhook": {
        "zh": ("飞书·基础设施群 Webhook", "巡检/体检推送地址；空=复用业务群"),
        "en": ("Feishu patrol webhook", "Bot webhook for patrol / hygiene; empty = reuse vendor"),
    },
    "feishu_patrol_sign_secret": {
        "zh": ("飞书·基础设施群签名密钥", "基础设施群机器人的加签密钥（如启用）"),
        "en": ("Feishu patrol sign secret", "Signing secret for the patrol-group bot (if enabled)"),
    },
    "sentinel_telegram_bot_token": {
        "zh": ("Telegram Bot Token", "机器人令牌；与 Chat ID 同时配齐才启用"),
        "en": ("Telegram bot token", "Bot token; enabled only with a Chat ID set"),
    },
    "sentinel_telegram_chat_id": {
        "zh": ("Telegram Chat ID", "目标会话 ID（群为负数）"),
        "en": ("Telegram chat ID", "Target chat ID (groups are negative)"),
    },
    "sentinel_ntfy_url": {
        "zh": ("ntfy 主题地址", "完整 topic URL，如 https://ntfy.sh/xxx"),
        "en": ("ntfy topic URL", "Full topic URL, e.g. https://ntfy.sh/xxx"),
    },
    "sentinel_ntfy_token": {
        "zh": ("ntfy 访问令牌", "受保护/自建 topic 的 Bearer（可选）"),
        "en": ("ntfy token", "Bearer for protected / self-hosted topics (optional)"),
    },
    "sentinel_webhook_url": {
        "zh": ("通用 Webhook 地址", "机器消费 JSON 的通用 webhook 端点"),
        "en": ("Generic webhook URL", "Generic JSON webhook endpoint for machines"),
    },
    "sentinel_webhook_token": {
        "zh": ("通用 Webhook 令牌", "通用 webhook 的 Bearer（可选）"),
        "en": ("Generic webhook token", "Bearer for the generic webhook (optional)"),
    },
    "sentinel_heartbeat_enabled": {
        "zh": ("心跳日报开关", "每天定时推一张全绿存活汇总"),
        "en": ("Heartbeat digest", "Daily all-green liveness summary push"),
    },
    "sentinel_heartbeat_hour": {
        "zh": ("心跳日报时刻", "心跳日报发送的小时（按下方时区）"),
        "en": ("Heartbeat hour", "Hour of day the heartbeat is sent (see offset)"),
    },
    "sentinel_heartbeat_utc_offset": {
        "zh": ("时区偏移", "固定 UTC 偏移小时数（北京=8）"),
        "en": ("UTC offset", "Fixed UTC offset hours (Beijing = 8)"),
    },
    "sentinel_report_hour": {
        "zh": ("体检日报时刻", "体检日报发送的小时（与心跳独立）"),
        "en": ("Report hour", "Hour the hygiene report is sent (independent of heartbeat)"),
    },
    "sentinel_evening_digest_hour": {
        "zh": ("晚间摘要时刻", "非紧急事件晚间汇总发送小时"),
        "en": ("Evening digest hour", "Hour the non-urgent evening digest is sent"),
    },
    "sentinel_defer_nonurgent": {
        "zh": ("非紧急事件延后", "将非硬告警合并进早晚摘要"),
        "en": ("Defer non-urgent events", "Aggregate non-hard alerts into scheduled digests"),
    },
    # —— llm：LLM 诊断 ——
    "llm_base_url": {
        "zh": ("LLM 接口地址", "OpenAI 兼容端点 base_url；与模型同时配齐才启用"),
        "en": ("LLM base URL", "OpenAI-compatible base URL; enabled only with a model"),
    },
    "llm_api_key": {
        "zh": ("LLM API Key", "调用 LLM 的密钥"),
        "en": ("LLM API key", "Credential for calling the LLM"),
    },
    "llm_model": {
        "zh": ("LLM 模型名", "诊断/总结使用的模型"),
        "en": ("LLM model", "Model used for diagnosis / summaries"),
    },
    "llm_timeout_seconds": {
        "zh": ("LLM 超时", "单次 LLM 请求超时（秒）"),
        "en": ("LLM timeout", "Per-request LLM timeout (seconds)"),
    },
    "llm_max_tool_rounds": {
        "zh": ("工具调用轮上限", "单次诊断的工具调用轮数上限，防失控烧 token"),
        "en": ("Max tool rounds", "Tool-call rounds cap per diagnosis (token guard)"),
    },
    "sentinel_diag_interval": {
        "zh": ("诊断轮询周期", "扫描待诊断事件的周期（秒）"),
        "en": ("Diagnosis interval", "Period for scanning pending events (seconds)"),
    },
    "sentinel_llm_config_file": {
        "zh": ("LLM 注册表文件", "provider 注册表 YAML 路径；缺=回落 LLM_* env"),
        "en": ("LLM registry file", "Provider registry YAML; missing = fall back to LLM_* env"),
    },
    "sentinel_llm_lang": {
        "zh": ("诊断语言", "此后新生成诊断的语言（zh|en），历史不回溯"),
        "en": ("Diagnosis language", "Language for new diagnoses (zh|en); not retroactive"),
    },
    "sentinel_editor_mode": {
        "zh": ("状态编辑器模式", "off | shadow | enrich | gate"),
        "en": ("Status editor mode", "off | shadow | enrich | gate"),
    },
    "sentinel_editor_base_url": {
        "zh": ("状态编辑器地址", "LiteLLM/OpenAI 兼容接口 base URL"),
        "en": ("Status editor URL", "LiteLLM/OpenAI-compatible base URL"),
    },
    "sentinel_editor_api_key": {
        "zh": ("状态编辑器密钥", "调用状态编辑模型的专用虚拟密钥"),
        "en": ("Status editor API key", "Dedicated virtual key for the editor model"),
    },
    "sentinel_editor_model": {
        "zh": ("状态编辑器模型", "上游状态降噪与解释使用的模型"),
        "en": ("Status editor model", "Model used for upstream status editing"),
    },
    "sentinel_editor_timeout_seconds": {
        "zh": ("状态编辑器超时", "单次编辑请求超时（秒）"),
        "en": ("Status editor timeout", "Per-editor-request timeout (seconds)"),
    },
    # —— docker：Docker 扫描 ——
    "sentinel_docker_socket": {
        "zh": ("Docker Socket（弃用）", "已弃用，新部署改用 Docker 连接地址"),
        "en": ("Docker socket (deprecated)", "Deprecated; use Docker host on new deploys"),
    },
    "sentinel_docker_host": {
        "zh": ("Docker 连接地址", "unix:// 或 tcp://；配上才启用容器巡检"),
        "en": ("Docker host", "unix:// or tcp://; set to enable container scanning"),
    },
    "sentinel_docker_scan_interval": {
        "zh": ("Docker 扫描周期", "容器巡检周期（秒）"),
        "en": ("Docker scan interval", "Container scan period (seconds)"),
    },
    "sentinel_docker_crashloop_window": {
        "zh": ("重启风暴窗口", "判定频繁重启的滚动窗口（秒）"),
        "en": ("Crash-loop window", "Rolling window for restart-storm detection (seconds)"),
    },
    "sentinel_docker_crashloop_threshold": {
        "zh": ("重启风暴阈值", "窗口内重启达几次判 crash-loop"),
        "en": ("Crash-loop threshold", "Restarts within the window to flag a crash-loop"),
    },
    "sentinel_docker_exclude": {
        "zh": ("排除容器", "不纳入巡检的容器名 CSV"),
        "en": ("Docker exclude", "Container names excluded from scanning (CSV)"),
    },
    # —— panel：面板 ——
    "sentinel_panel_enabled": {
        "zh": ("面板开关", "是否启用只读证据台面板"),
        "en": ("Panel enabled", "Whether the read-only panel is served"),
    },
    "sentinel_panel_default_lang": {
        "zh": ("默认语言", "空=按浏览器；否则 zh|en 强制"),
        "en": ("Default language", "Empty = by browser; else force zh|en"),
    },
    "sentinel_panel_default_theme": {
        "zh": ("默认主题", "dark | light | system"),
        "en": ("Default theme", "dark | light | system"),
    },
    "sentinel_panel_history_days": {
        "zh": ("健康柱最长窗口", "健康柱条可切换的最大天数上限"),
        "en": ("History window cap", "Max selectable days for the health bars"),
    },
    "sentinel_panel_default_window": {
        "zh": ("默认时间窗", "首屏默认窗口（30 或上限）"),
        "en": ("Default window", "Default first-screen window (30 or the cap)"),
    },
    "sentinel_panel_refresh_seconds": {
        "zh": ("自动刷新间隔", "面板自动刷新周期（秒）"),
        "en": ("Auto-refresh interval", "Panel auto-refresh period (seconds)"),
    },
    "sentinel_panel_page_size": {
        "zh": ("事件每页条数", "事件流分页大小"),
        "en": ("Events page size", "Events per page in the feed"),
    },
    "sentinel_panel_red_uptime_pct": {
        "zh": ("红线·可用率", "当日可用率低于此判不可用（红）"),
        "en": ("Red uptime line", "Below this today = down (red)"),
    },
    "sentinel_panel_partial_uptime_pct": {
        "zh": ("橙线·可用率", "低于此（且≥红线）判异常（橙）"),
        "en": ("Partial uptime line", "Below this (and ≥ red) = partial (amber)"),
    },
    "sentinel_panel_green_uptime_pct": {
        "zh": ("绿线·可用率", "达此判健康（绿）"),
        "en": ("Green uptime line", "At / above this = healthy (green)"),
    },
    "sentinel_panel_overview_roster_cap": {
        "zh": ("总览服务表行数", "总览服务表截断行数，其余折叠"),
        "en": ("Overview roster cap", "Rows shown in the overview roster before folding"),
    },
    "sentinel_event_feed_days": {
        "zh": ("事件流窗口", "事件流纳入近 N 天（未结事件不受限）"),
        "en": ("Event feed window", "Feed includes the last N days (open events unbounded)"),
    },
    "sentinel_update_check_enabled": {
        "zh": ("更新检查开关", "后台周期查 GitHub 有无新版"),
        "en": ("Update check", "Periodically check GitHub for a newer release"),
    },
    "sentinel_update_check_url": {
        "zh": ("更新检查地址", "查询最新 release 的 GitHub API"),
        "en": ("Update check URL", "GitHub API for the latest release"),
    },
    "sentinel_update_check_interval": {
        "zh": ("更新检查周期", "检查新版的周期（秒）"),
        "en": ("Update check interval", "How often to check for updates (seconds)"),
    },
    # —— backup_cert：备份与证书 ——
    "sentinel_backup_dir": {
        "zh": ("备份目录", "数据库备份文件所在目录"),
        "en": ("Backup directory", "Directory holding database backup files"),
    },
    "sentinel_backup_max_age_hours": {
        "zh": ("备份最大龄", "最新备份超此小时数判过期"),
        "en": ("Backup max age", "Latest backup older than this (hours) = stale"),
    },
    "sentinel_restic_backup_max_age_hours": {
        "zh": ("Restic 最大龄", "最近成功时间超过此小时数告警；0=关闭"),
        "en": ("Restic max age", "Alert when last success is older than this; 0=off"),
    },
    "sentinel_cert_domains": {
        "zh": ("证书域名", "检查 TLS 到期的公网域名 CSV；空=跳过"),
        "en": ("Cert domains", "Public domains to check TLS expiry (CSV); empty = skip"),
    },
    "sentinel_cert_min_days": {
        "zh": ("证书最少剩余天", "证书剩余有效期低于此天数告警"),
        "en": ("Cert min days", "Alert when cert validity drops below this many days"),
    },
    # —— security：安全与部署 ——
    "sentinel_db_path": {
        "zh": ("数据库路径", "SQLite 落盘路径"),
        "en": ("Database path", "SQLite file path"),
    },
    "sentinel_diag_token": {
        "zh": ("写 API 鉴权 Token", "编排/诊断写端点的鉴权令牌；空=不鉴权"),
        "en": ("Write-API token", "Auth token for diag/orchestration write endpoints; empty=none"),
    },
    "sentinel_codex_ingest_token": {
        "zh": ("Codex 通知 Token", "Codex 通知入口的独立鉴权令牌；空=入口关闭"),
        "en": ("Codex ingest token", "Dedicated token for Codex notifications; empty=disabled"),
    },
    "sentinel_codex_receipt_retention_days": {
        "zh": ("Codex 回执保留天数", "仅保留事件键哈希，用于重试幂等去重"),
        "en": ("Codex receipt retention", "Days to retain hashed idempotency receipts"),
    },
}


def _meta(name: str, lang: str) -> tuple[str, str]:
    """字段友好名 + 说明（按 lang，缺 lang 回退 zh，缺整条回退 env 名/空说明）。"""
    m = _FIELD_META.get(name)
    if not m:
        return name.upper(), ""
    label, desc = m.get(lang) or m["zh"]
    return label, desc


def _secret_values(settings: Settings) -> list[str]:
    """已配置的密钥真值,喂给 redact_text 做兜底子串脱敏。"""
    out: list[str] = []
    for name in _SECRETS:
        v = getattr(settings, name, None)
        if v:
            out.append(str(v))
    return out


def _field_row(settings: Settings, name: str, secret_vals: list[str], lang: str) -> dict:
    raw = getattr(settings, name, None)
    is_secret = name in _SECRETS
    configured = bool(raw)
    if is_secret:
        value = None  # 永不出值
    else:
        text = "" if raw is None else str(raw)
        value = redact_text(text, secrets=secret_vals, use_patterns=True)[0] if text else ""
    label, desc = _meta(name, lang)
    return {
        "env": name.upper(),
        "field": name,  # 小写字段名（模板/测试用）
        "label": label,  # 友好名（主标题）
        "desc": desc,  # 一句话说明（副行）
        "value": value,
        "secret": is_secret,
        "configured": configured,
        "change": "env",
    }


def _llm_synthetic_rows(llm_config: LLMConfig | None) -> list[dict]:
    """LLM 组顶部 active/fallback 合成行(取自注册表,非 Settings 字段)。
    只显 provider+model,绝不显 api_key/base_url;未启用显占位 '—'。"""

    def _model(profile: LLMProfile | None) -> str:
        # 值列只显模型;未启用占位 '—'(绝不显 api_key/base_url)。
        return profile.model if profile is not None else "—"

    def _source(profile: LLMProfile | None) -> str | None:
        # provider 名作 meta 区的来源 chip(与同级 env chip 左对齐),非主值。
        return profile.name if profile is not None else None

    active = llm_config.current() if llm_config is not None else None
    fallback = llm_config.fallback() if llm_config is not None else None
    # 合成行（active/fallback）名/说明由模板用 i18n 渲染，故 field/label/desc 置空。
    return [
        {
            "synthetic": "active",
            "env": None,
            "field": None,
            "label": None,
            "desc": None,
            "source": _source(active),
            "value": _model(active),
            "secret": False,
            "configured": active is not None,
            "change": "llm",
        },
        {
            "synthetic": "fallback",
            "env": None,
            "field": None,
            "label": None,
            "desc": None,
            "source": _source(fallback),
            "value": _model(fallback),
            "secret": False,
            "configured": fallback is not None,
            "change": "llm",
        },
    ]


def build_config_inventory(
    settings: Settings, *, llm_config: LLMConfig | None = None, lang: str = "zh"
) -> dict:
    """按组只读一览。返回 {"groups": [{"key", "rows": [...]}, ...]}。
    lang 决定每行友好名/说明（缺 lang 回退 zh）；env 名与值仍语言无关。"""
    secret_vals = _secret_values(settings)
    groups: list[dict] = []
    for key, names in _GROUPS:
        rows: list[dict] = []
        if key == "llm":
            rows.extend(_llm_synthetic_rows(llm_config))
        rows.extend(_field_row(settings, n, secret_vals, lang) for n in names)
        groups.append({"key": key, "rows": rows})
    return {"groups": groups}


def build_display_prefs(
    *,
    lang_eff: str,
    lang_cookie: str | None,
    theme_eff: str,
    window_eff: int,
    history_days: int,
    refresh_eff: int,
    refresh_cookie: str | None,
    server_refresh: int,
) -> dict:
    """显示偏好表单的当前选中态。lang/refresh 的"中性选项"(自动/默认)在无显式 cookie 时选中。"""
    lang_sel = (
        lang_cookie.strip().lower()
        if (lang_cookie or "").strip().lower() in ("zh", "en")
        else "auto"
    )
    refresh_sel = (
        refresh_cookie.strip()
        if (refresh_cookie or "").strip() in ("0", "15", "30", "60")
        else "default"
    )
    return {
        "lang": {"selected": lang_sel, "options": ["auto", "zh", "en"]},
        "theme": {"selected": theme_eff, "options": ["system", "dark", "light"]},
        "window": {"selected": window_eff, "options": [30, history_days]},
        "refresh": {
            "selected": refresh_sel,
            "options": ["default", "0", "15", "30", "60"],
            "server": server_refresh,
        },
    }
