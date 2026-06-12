# src/sentinel/config.py
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    feishu_vendor_webhook: str
    feishu_vendor_sign_secret: str | None = None
    sentinel_providers: str = "anthropic,openai,github,cloudflare,google_cloud"
    sentinel_poll_interval: int = 60
    sentinel_incident_verbosity: str = "phase"
    sentinel_fail_threshold: int = 3
    sentinel_db_path: str = "/data/sentinel.db"
    # 心跳日报:每天 heartbeat_hour 点(按 utc_offset 时区)推一张全绿汇总,给"存在感"+确认存活。
    sentinel_heartbeat_enabled: bool = True
    sentinel_heartbeat_hour: int = 9
    sentinel_heartbeat_utc_offset: int = 8  # 固定偏移(北京 UTC+8 无 DST),免 zoneinfo/tzdata 依赖

    # 内部探针 + 体检日报(Phase 1)
    sentinel_probe_interval: int = 300
    sentinel_services_file: str = "services.yaml"
    sentinel_report_hour: int = 9  # 体检日报发送时刻(与 heartbeat_hour 独立,可配不同时间)
    sentinel_probe_retention_days: int = 30
    feishu_patrol_webhook: str = ""  # 留空 → 复用 vendor webhook(基础设施群)
    feishu_patrol_sign_secret: str | None = None

    # 检测与事件(Phase 2):阈值全部可配,默认值基于设计文档 §6(备份龄按相位差有意收紧)
    sentinel_prometheus_url: str = "http://prometheus:9090"
    sentinel_loki_url: str = "http://loki:3100"
    sentinel_scan_interval: int = 900  # metrics_scan / log_scan 周期(秒)
    sentinel_cooldown_hours: int = 6  # 同 (rule, subject) 事件冷却
    sentinel_scan_fail_threshold: int = 3  # 数据源连续失败 N 次才发巡检失败卡
    sentinel_probe_fail_streak: int = 3  # 连续失败 N 次判服务异常
    sentinel_latency_ratio: float = 2.0  # 1h p95 超七日基线倍数
    sentinel_latency_margin_ms: float = 500.0  # 且超基线绝对量(两者取大)
    sentinel_latency_min_samples: int = 6
    sentinel_log_spike_ratio: float = 3.0
    sentinel_log_spike_min: int = 10  # 绝对下限,基线为 0 时防误报
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
    sentinel_cert_domains: str = ""  # 逗号分隔公网域名;空=跳过证书检查
    sentinel_cert_min_days: int = 14
    # Phase 3 编排 API 写端点鉴权 token;空=不鉴权(仅容器网内可达时可接受)
    sentinel_diag_token: str = ""

    @property
    def providers_list(self) -> list[str]:
        return [p.strip() for p in self.sentinel_providers.split(",") if p.strip()]

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
