"""测试飞书通知路由：优先级分类和标题前缀"""

from sentinel.notify.message import Kind, Notification, Severity
from sentinel.notify.routing import (
    Priority,
    apply_title_prefix,
    classify_notification,
)


class TestClassifyNotification:
    """测试通知分类逻辑"""

    def test_alert_critical_to_p0(self):
        """Critical 告警 → P0"""
        n = Notification(
            kind=Kind.ALERT,
            severity=Severity.CRITICAL,
            title="测试告警",
            detail="详情",
        )
        assert classify_notification(n) == Priority.P0

    def test_alert_warning_to_p1(self):
        """Warning 告警 → P1"""
        n = Notification(
            kind=Kind.ALERT,
            severity=Severity.WARNING,
            title="测试告警",
            detail="详情",
        )
        assert classify_notification(n) == Priority.P1

    def test_recovery_to_p2(self):
        """恢复通知 → P2"""
        n = Notification(
            kind=Kind.RECOVERY,
            severity=Severity.INFO,
            title="已恢复",
            detail="详情",
        )
        assert classify_notification(n) == Priority.P2

    def test_vendor_incident_critical_to_p0(self):
        """上游严重故障 → P0"""
        n = Notification(
            kind=Kind.VENDOR_INCIDENT,
            severity=Severity.CRITICAL,
            title="OpenAI 状态变更",
            detail="详情",
        )
        assert classify_notification(n) == Priority.P0

    def test_vendor_incident_minor_to_p2(self):
        """上游轻微问题 → P2"""
        n = Notification(
            kind=Kind.VENDOR_INCIDENT,
            severity=Severity.WARNING,
            title="OpenAI 状态变更",
            detail="详情",
        )
        assert classify_notification(n) == Priority.P2

    def test_heartbeat_warning_to_p0(self):
        """心跳异常 → P0"""
        n = Notification(
            kind=Kind.HEARTBEAT,
            severity=Severity.WARNING,
            title="外部依赖巡检 · 存在异常",
            detail="详情",
        )
        assert classify_notification(n) == Priority.P0

    def test_heartbeat_ok_to_p2(self):
        """心跳正常 → P2"""
        n = Notification(
            kind=Kind.HEARTBEAT,
            severity=Severity.INFO,
            title="外部依赖巡检 · 全部正常",
            detail="详情",
        )
        assert classify_notification(n) == Priority.P2

    def test_report_critical_to_p0(self):
        """体检日报 critical → P0"""
        n = Notification(
            kind=Kind.REPORT,
            severity=Severity.CRITICAL,
            title="内部体检日报",
            detail="详情",
        )
        assert classify_notification(n) == Priority.P0

    def test_report_warning_to_p1(self):
        """体检日报 warning → P1"""
        n = Notification(
            kind=Kind.REPORT,
            severity=Severity.WARNING,
            title="内部体检日报",
            detail="详情",
        )
        assert classify_notification(n) == Priority.P1

    def test_report_info_to_p2(self):
        """体检日报 info → P2"""
        n = Notification(
            kind=Kind.REPORT,
            severity=Severity.INFO,
            title="内部体检日报",
            detail="详情",
        )
        assert classify_notification(n) == Priority.P2

    def test_digest_to_p2(self):
        """巡检摘要 → P2"""
        n = Notification(
            kind=Kind.DIGEST,
            severity=Severity.INFO,
            title="巡检摘要",
            detail="详情",
        )
        assert classify_notification(n) == Priority.P2

    def test_codex_turn_approval_required_to_p1(self):
        """Codex 等待审批 → P1"""
        n = Notification(
            kind=Kind.CODEX_TURN,
            severity=Severity.WARNING,
            title="Codex 等待审批",
            detail="详情",
            data={"category": "approval_required"},
        )
        assert classify_notification(n) == Priority.P1

    def test_codex_turn_input_required_to_p1(self):
        """Codex 等待输入 → P1"""
        n = Notification(
            kind=Kind.CODEX_TURN,
            severity=Severity.WARNING,
            title="Codex 等待输入",
            detail="详情",
            data={"category": "input_required"},
        )
        assert classify_notification(n) == Priority.P1

    def test_codex_turn_execution_failed_to_p1(self):
        """Codex 执行受阻 → P1"""
        n = Notification(
            kind=Kind.CODEX_TURN,
            severity=Severity.WARNING,
            title="Codex 执行受阻",
            detail="详情",
            data={"category": "execution_failed"},
        )
        assert classify_notification(n) == Priority.P1

    def test_codex_turn_long_turn_complete_to_p1(self):
        """Codex 长任务完成 → P1"""
        n = Notification(
            kind=Kind.CODEX_TURN,
            severity=Severity.INFO,
            title="Codex 长任务完成",
            detail="详情",
            data={"category": "long_turn_complete"},
        )
        assert classify_notification(n) == Priority.P1

    def test_codex_turn_normal_complete_to_p2(self):
        """Codex 普通完成 → P2"""
        n = Notification(
            kind=Kind.CODEX_TURN,
            severity=Severity.INFO,
            title="Codex 回合完成",
            detail="详情",
            data={"category": "turn_complete"},
        )
        assert classify_notification(n) == Priority.P2

    def test_codex_reset_forecast_to_p1(self):
        """Codex reset 预告 → P1"""
        n = Notification(
            kind=Kind.CODEX_RESET,
            severity=Severity.INFO,
            title="Codex 额度预告",
            detail="详情",
            data={"stage": "forecast"},
        )
        assert classify_notification(n) == Priority.P1

    def test_codex_reset_confirmed_to_p1(self):
        """Codex reset 确认 → P1"""
        n = Notification(
            kind=Kind.CODEX_RESET,
            severity=Severity.INFO,
            title="Codex 额度确认",
            detail="详情",
            data={"stage": "confirmed"},
        )
        assert classify_notification(n) == Priority.P1

    def test_codex_reset_delayed_to_p1(self):
        """Codex reset 延迟 → P1"""
        n = Notification(
            kind=Kind.CODEX_RESET,
            severity=Severity.WARNING,
            title="Codex 额度延迟",
            detail="详情",
            data={"stage": "delayed"},
        )
        assert classify_notification(n) == Priority.P1

    def test_diagnosis_to_p2(self):
        """诊断结果 → P2"""
        n = Notification(
            kind=Kind.DIAGNOSIS,
            severity=Severity.INFO,
            title="诊断",
            detail="详情",
        )
        assert classify_notification(n) == Priority.P2

    def test_summary_to_p2(self):
        """AI 总结 → P2"""
        n = Notification(
            kind=Kind.SUMMARY,
            severity=Severity.INFO,
            title="日报总结",
            detail="详情",
        )
        assert classify_notification(n) == Priority.P2


class TestApplyTitlePrefix:
    """测试标题前缀应用"""

    def test_apply_p0_prefix(self):
        """应用 P0 前缀"""
        result = apply_title_prefix("测试标题", Priority.P0)
        assert result == "[P0] 测试标题"

    def test_apply_p1_prefix(self):
        """应用验收前缀"""
        result = apply_title_prefix("测试标题", Priority.P1)
        assert result == "[验收] 测试标题"

    def test_apply_p2_prefix(self):
        """应用流水账前缀"""
        result = apply_title_prefix("测试标题", Priority.P2)
        assert result == "[流水] 测试标题"

    def test_unknown_priority_no_prefix(self):
        """未知优先级不添加前缀"""
        result = apply_title_prefix("测试标题", "unknown")
        assert result == "测试标题"

    def test_empty_title(self):
        """空标题返回前缀"""
        result = apply_title_prefix("", Priority.P0)
        assert result == "[P0] "


class TestEndToEndClassification:
    """端到端分类测试：确保关键场景正确分类"""

    def test_continuous_health_failure_is_p0(self):
        """连续健康失败判 P0"""
        n = Notification(
            kind=Kind.ALERT,
            severity=Severity.CRITICAL,
            title="内部探针 · 服务异常",
            detail="连续失败 3 次",
        )
        assert classify_notification(n) == Priority.P0

    def test_upstream_major_incident_is_p0(self):
        """上游 major 事件判 P0"""
        n = Notification(
            kind=Kind.VENDOR_INCIDENT,
            severity=Severity.CRITICAL,
            title="OpenAI · API 完全不可用",
            detail="major outage",
        )
        assert classify_notification(n) == Priority.P0

    def test_codex_long_task_done_is_p1(self):
        """Codex 长任务完成判 P1"""
        n = Notification(
            kind=Kind.CODEX_TURN,
            severity=Severity.INFO,
            title="Codex 长任务完成",
            detail="耗时 5 分钟",
            data={"category": "long_turn_complete"},
        )
        assert classify_notification(n) == Priority.P1

    def test_scheduled_digest_is_p2(self):
        """定时摘要判 P2"""
        n = Notification(
            kind=Kind.DIGEST,
            severity=Severity.INFO,
            title="巡检摘要 · 18:00",
            detail="聚合 5 类事件",
        )
        assert classify_notification(n) == Priority.P2

    def test_all_green_heartbeat_is_p2(self):
        """全绿心跳判 P2"""
        n = Notification(
            kind=Kind.HEARTBEAT,
            severity=Severity.INFO,
            title="外部依赖巡检 · 全部正常",
            detail="8/8 正常",
        )
        assert classify_notification(n) == Priority.P2

    def test_upstream_recovered_is_p2(self):
        """上游恢复判 P2"""
        n = Notification(
            kind=Kind.VENDOR_INCIDENT,
            severity=Severity.INFO,
            title="GitHub · 服务已恢复",
            detail="incident resolved",
        )
        assert classify_notification(n) == Priority.P2

    def test_successful_deployment_via_report_is_p2(self):
        """成功部署（通过体检日报 info）判 P2"""
        n = Notification(
            kind=Kind.REPORT,
            severity=Severity.INFO,
            title="内部体检日报 · 全部正常",
            detail="无未决事件",
        )
        assert classify_notification(n) == Priority.P2
