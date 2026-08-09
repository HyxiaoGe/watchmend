# src/sentinel/config.py
from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    feishu_vendor_webhook: str = ""  # CHANGED: 不再必填;至少配一个通知渠道即可(build_jobs 校验)
    feishu_vendor_sign_secret: str | None = None
    sentinel_providers: str = "anthropic,openai,github,cloudflare,google_cloud"
    sentinel_poll_interval: int = 60
    sentinel_incident_verbosity: str = "phase"
    sentinel_fail_threshold: int = 3
    # 上游状态编辑器：独立于内部事件诊断 LLM，支持影子评测、增强和低风险门控。
    sentinel_provider_card_min_gap_s: int = 600
    sentinel_editor_mode: Literal["off", "shadow", "enrich", "gate"] = "off"
    sentinel_editor_base_url: str = ""
    sentinel_editor_api_key: str = ""
    sentinel_editor_model: str = ""
    sentinel_editor_timeout_seconds: float = 15
    sentinel_db_path: str = "/data/sentinel.db"
    sentinel_notification_mode: Literal["live", "shadow"] = "live"
    # 心跳日报:每天 heartbeat_hour 点(按 utc_offset 时区)推一张全绿汇总,给"存在感"+确认存活。
    sentinel_heartbeat_enabled: bool = True
    sentinel_heartbeat_hour: int = 9
    sentinel_heartbeat_utc_offset: int = 8  # 固定偏移(北京 UTC+8 无 DST),免 zoneinfo/tzdata 依赖

    # 内部探针 + 体检日报(Phase 1)
    sentinel_probe_interval: int = 300
    sentinel_services_file: str = "services.yaml"
    sentinel_report_hour: int = 9  # 体检日报发送时刻(与 heartbeat_hour 独立,可配不同时间)
    sentinel_evening_digest_hour: int = 18
    # 默认关闭以保持既有逐事件实时广播；启用后非硬告警进入 09:00/18:00 摘要。
    sentinel_defer_nonurgent: bool = False
    sentinel_probe_retention_days: int = 30
    feishu_patrol_webhook: str = ""  # 留空 → 复用 vendor webhook(基础设施群)
    feishu_patrol_sign_secret: str | None = None

    # 检测与事件(Phase 2):阈值全部可配,默认值基于设计文档 §6(备份龄按相位差有意收紧)
    sentinel_prometheus_url: str = ""  # CHANGED: 默认空=不接 prometheus(opt-in 数据源)
    sentinel_loki_url: str = ""  # CHANGED: 默认空=不接 loki(opt-in 数据源)
    sentinel_scan_interval: int = 900  # metrics_scan / log_scan 周期(秒)
    sentinel_cooldown_hours: int = 6  # 同 (rule, subject) 事件冷却
    sentinel_scan_fail_threshold: int = 3  # 数据源连续失败 N 次才发巡检失败卡
    sentinel_probe_fail_streak: int = 3  # 连续失败 N 次判服务异常
    sentinel_latency_ratio: float = 2.0  # 1h p95 超七日基线倍数
    sentinel_latency_margin_ms: float = 500.0  # 且超基线绝对量(两者取大)
    sentinel_latency_min_samples: int = 6
    sentinel_log_spike_ratio: float = 3.0
    sentinel_log_spike_min: int = 10  # 绝对下限,基线为 0 时防误报
    # 新错误指纹扫描默认关闭，启用后与错误尖峰互补；每个稳定指纹只生成 point 事件。
    sentinel_error_alert_enabled: bool = False
    sentinel_error_alert_interval: int = 300
    sentinel_error_window_minutes: int = 10
    sentinel_error_max_per_cycle: int = 8
    sentinel_error_query_limit: int = 1000
    sentinel_error_containers: str = ".+"
    sentinel_error_ignore_patterns: str = ""
    sentinel_self_container: str = "watchmend"
    sentinel_disk_usage_pct: float = 85.0
    sentinel_disk_forecast_days: int = 14
    sentinel_container_mem_pct: float = 90.0
    sentinel_swap_pct: float = 80.0
    # 存储中间件兜底 up 指标:CSV "metric:展示名",如 "pg_up:postgres,redis_up:redis";
    # 空=跳过 middleware_down 检查(没部署对应 exporter 时的安全默认)
    sentinel_middleware_metrics: str = ""
    sentinel_backup_dir: str = "/backups/postgresql"
    # 28h:备份 03:00/检查 09:00,正常龄 6h,漏一天 30h → 次晨即告警(设计稿 36h 会漏单日失败)
    sentinel_backup_max_age_hours: int = 28
    # 0=关闭；启用时读取 restic_backup_last_success_timestamp_seconds。
    sentinel_restic_backup_max_age_hours: int = 0
    sentinel_cert_domains: str = ""  # 逗号分隔公网域名;空=跳过证书检查
    sentinel_cert_min_days: int = 14
    # Phase 3 编排 API 写端点鉴权 token;空=不鉴权(仅容器网内可达时可接受)
    sentinel_diag_token: str = ""
    # Codex 通知入口独立鉴权；空=入口关闭（不能退化成匿名写入）。
    sentinel_codex_ingest_token: str = ""
    sentinel_codex_receipt_retention_days: int = Field(default=30, ge=1)
    # Hook 通知先进入内存候选；宽限期内的用户输入/审批后执行会取消，避免逐回合骚扰。
    sentinel_codex_hook_grace_seconds: int = Field(default=900, ge=1, le=3600)
    sentinel_codex_long_turn_seconds: int = Field(default=180, ge=1, le=86400)
    sentinel_codex_hook_poll_seconds: int = Field(default=1, ge=1, le=60)

    # LLM 直连(OpenAI-compatible):base_url 和 model 同时非空才启用,
    # 留空=LLM 诊断/总结层整体关闭,确定性巡检与告警不受影响。
    # 兼容 openai/deepseek/ollama/vllm 等任何同协议端点。
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: int = 120
    llm_max_tool_rounds: int = 8  # 单次诊断的工具调用轮数上限,防失控烧 token
    sentinel_diag_interval: int = 120  # pending 事件诊断轮询周期(秒)
    # LLM provider 注册表文件(子项目④);缺失=回落老 LLM_* env(零 breaking)。
    sentinel_llm_config_file: str = "llm.yaml"
    # docker 只读诊断 + 容器巡检(ps/logs/inspect):配 host 启用,空=整体不启用。
    # 挂 socket / 连 socket-proxy 等于给容器宿主机级权限,默认关、按需打开。
    sentinel_docker_socket: str = ""  # DEPRECATED:保留向后兼容,新部署改用 sentinel_docker_host
    sentinel_docker_host: str = ""  # "unix:///var/run/docker.sock" | "tcp://docker-proxy:2375"
    sentinel_docker_scan_interval: int = 60  # docker_scan 周期(秒)
    # crash-loop 检测(docker-only 盲区补充,见设计稿 §4):W 内重启 >=N 次发 point 卡。
    # prom+cadvisor 在跑时由 container_restart 覆盖,docker 层此规则被 emit_crashloop 门禁关掉。
    # 两个 knob 都 ge=1:<=0 会让 delta>=N 恒真 / 发"0 分钟"噪声卡,启动即拒绝(逼操作员改对)。
    sentinel_docker_crashloop_window: int = Field(default=600, ge=1)  # 滚动窗口(秒)
    sentinel_docker_crashloop_threshold: int = Field(default=3, ge=1)  # 窗口内 >=N 判 loop
    sentinel_docker_exclude: str = ""  # 排除的容器名 CSV

    # 通知渠道(子项目②):全部默认空=不启用。飞书之外,海外自托管可只配任一渠道。
    sentinel_telegram_bot_token: str = ""  # 与 chat_id 同时非空才启用
    sentinel_telegram_chat_id: str = ""  # 群组为负数,原样字符串
    sentinel_ntfy_url: str = ""  # 完整 topic URL,如 https://ntfy.sh/my-watchmend-xyz
    sentinel_ntfy_token: str = ""  # 可选 Bearer(受保护 topic / 自建)
    sentinel_webhook_url: str = ""  # 通用 webhook 端点(机器消费 JSON)
    sentinel_webhook_token: str = ""  # 可选 Bearer

    # 证据台只读面板(子项目③):SSR 零 JS,仅本机访问无鉴权。默认开,部署可关。
    sentinel_panel_enabled: bool = True

    # 证据台重设计（子项目⑤）：外观/可视化偏好默认 + 颜色阈值 + 诊断语言。
    sentinel_panel_default_lang: str = ""  # 空=auto（Accept-Language）；否则 zh|en 强制
    sentinel_panel_default_theme: str = "system"  # dark|light|system
    sentinel_panel_history_days: int = 90  # 健康柱条最长窗口 = 90/30 切换的上限
    sentinel_panel_default_window: int = 30  # 首屏默认窗口（{30, history_days} 之一）；降噪默认 30
    # 面板自动刷新间隔(秒)。JS 开 → 内联轮询用此值做无闪烁局部刷新;
    # JS 关 → <noscript> meta 用此值整页刷新。ge=5 防过频整页渲染打服务端。
    sentinel_panel_refresh_seconds: int = Field(default=30, ge=5)
    sentinel_panel_page_size: int = 8  # 事件流每页条数
    sentinel_panel_red_uptime_pct: float = 50.0  # 当日 uptime 低于此 → down（红）
    sentinel_panel_partial_uptime_pct: float = 99.5  # 低于此（且≥red）→ partial（橙）
    sentinel_panel_green_uptime_pct: float = 99.9  # ≥此→今日可用率绿；≥partial 琥珀；以下红
    sentinel_panel_overview_roster_cap: int = 6  # 总览服务表截断行数（其余折叠 → /services）
    sentinel_llm_lang: str = "zh"  # 此后新生成的诊断语言（zh|en）；历史不回溯翻译
    sentinel_event_feed_days: int = 30  # 事件流纳入"近 N 天"的窗口（open 不受限）

    # 更新检查（默认开，Dozzle 式便捷）：后台周期查 GitHub releases，有新版面板提示；
    # 隐私/airgap 设 SENTINEL_UPDATE_CHECK_ENABLED=false 或 URL 置空即关。仅公开 GET、无遥测。
    sentinel_update_check_enabled: bool = True
    sentinel_update_check_url: str = (
        "https://api.github.com/repos/HyxiaoGe/watchmend/releases/latest"
    )
    sentinel_update_check_interval: int = Field(default=21600, ge=60)  # 6h

    @property
    def providers_list(self) -> list[str]:
        return [p.strip() for p in self.sentinel_providers.split(",") if p.strip()]

    @property
    def editor_enabled(self) -> bool:
        return (
            self.sentinel_editor_mode != "off"
            and bool(self.sentinel_editor_base_url)
            and bool(self.sentinel_editor_model)
        )

    @property
    def vendor_webhook(self) -> str:
        # 对称回退:vendor 留空但配了 patrol 时,vendor 流(状态页/心跳)复用 patrol 群,
        # 避免 patrol-only 配置下 vendor broadcaster 为空、状态页事件静默不发(见 build_jobs 校验)。
        return self.feishu_vendor_webhook or self.feishu_patrol_webhook

    @property
    def vendor_sign_secret(self) -> str | None:
        if self.feishu_vendor_webhook:
            return self.feishu_vendor_sign_secret or None
        return self.feishu_patrol_sign_secret

    @property
    def patrol_webhook(self) -> str:
        return self.feishu_patrol_webhook or self.feishu_vendor_webhook

    @property
    def patrol_sign_secret(self) -> str | None:
        if self.feishu_patrol_webhook:
            return self.feishu_patrol_sign_secret or None
        return self.feishu_vendor_sign_secret

    @property
    def cert_domains_list(self) -> list[str]:
        return [d.strip() for d in self.sentinel_cert_domains.split(",") if d.strip()]

    @property
    def error_ignore_list(self) -> list[str]:
        return [
            pattern.strip().lower()
            for pattern in self.sentinel_error_ignore_patterns.split(",")
            if pattern.strip()
        ]

    def loki_stream_selector(self, container_re: str) -> str:
        selector = f'container=~"{container_re}"'
        if self.sentinel_self_container:
            selector += f', container!="{self.sentinel_self_container}"'
        return "{" + selector + "}"

    @property
    def middleware_subjects(self) -> dict[str, str]:
        """解析 sentinel_middleware_metrics → {metric: 展示名};省略展示名时用 metric 本身。"""
        out: dict[str, str] = {}
        for part in self.sentinel_middleware_metrics.split(","):
            part = part.strip()
            if not part:
                continue
            metric, _, subject = part.partition(":")
            out[metric.strip()] = subject.strip() or metric.strip()
        return out

    @property
    def docker_endpoint(self) -> str:
        if self.sentinel_docker_host:
            return self.sentinel_docker_host
        if self.sentinel_docker_socket:
            # /var/run/docker.sock → unix:///var/run/docker.sock(三斜杠)
            return f"unix://{self.sentinel_docker_socket}"
        return ""

    @property
    def docker_exclude_list(self) -> list[str]:
        return [x.strip() for x in self.sentinel_docker_exclude.split(",") if x.strip()]

    @property
    def feishu_enabled(self) -> bool:
        return bool(self.feishu_vendor_webhook or self.feishu_patrol_webhook)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.sentinel_telegram_bot_token and self.sentinel_telegram_chat_id)

    @property
    def ntfy_enabled(self) -> bool:
        return bool(self.sentinel_ntfy_url)

    @property
    def webhook_enabled(self) -> bool:
        return bool(self.sentinel_webhook_url)
