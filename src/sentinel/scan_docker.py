# src/sentinel/scan_docker.py
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sentinel.config import Settings
from sentinel.docker_client import DockerClient, parse_docker_time
from sentinel.findings import Finding

if TYPE_CHECKING:
    from sentinel.store import Store

logger = logging.getLogger(__name__)

# 受巡检的重启策略:这些容器"理应一直在/失败应自愈",停掉才是异常。
# no(一次性脚本)/空(批处理)不在其中 → 退出属正常生命周期,不报。
_WATCHED_POLICIES = frozenset({"always", "unless-stopped", "on-failure"})

# 自身与 socket 代理永不上报:自检/sidecar 噪声,且代理重启会牵连巡检本体。
# 用子串匹配而非 startswith:生产镜像带 registry 前缀(如 ghcr.io/<org>/watchmend:0.1.1),
# startswith("watchmend") 会漏掉它 → 把巡检本体当普通退出容器误报 container_down。
_SELF_IMAGE_SUBSTR = "watchmend"
_SOCKET_PROXY_IMAGE = "tecnativa/docker-socket-proxy"


async def run_docker_scan(
    docker: DockerClient,
    settings: Settings,
    *,
    now_ts: int,
    emit_oom: bool,
) -> tuple[list[Finding], set[str], dict[str, int]]:
    """Docker 容器规则:container_down / container_unhealthy / container_oom。

    返回 (findings, active_subjects, restart_counts):active_subjects 是"本轮真正评估
    过"的容器名集合,供 tick 层判断 open 事件是否该 hold(容器消失 ≠ 恢复);restart_counts
    是受巡检+active 容器的 RestartCount(crash-loop 检测输入,纯读)。

    docker.ps() 失败直接向上抛(数据源故障,由 docker_tick 当连续失败处理);
    单容器 inspect_safe() 失败仅记日志并跳过该容器(竞态容忍:扫描期间被删很常见,
    不入 active_subjects、不中断本轮)。但候选 >=2 且 inspect 全部失败 = 系统性故障
    (代理 ACL/socket 权限/API 变更),向上抛交 docker_tick 升级,不静默吞成空 active。

    emit_oom 为 True 时放行 container_oom 规则,False 时只观测不发卡(去重)。由调用方
    (docker_tick)决定:Prometheus 缺席或 metrics 巡检失败 → True(docker 是 OOM 兜底);
    Prometheus 在且 metrics 健康 → False(交 metrics_scan 的 container_restart 覆盖,免双卡)。
    """
    findings: list[Finding] = []
    active_subjects: set[str] = set()
    restart_counts: dict[str, int] = {}  # 受巡检+active 容器的 RestartCount(crash-loop 输入)
    exclude = settings.docker_exclude_list
    inspect_candidates = 0  # 通过自身/代理/排除过滤、本该 inspect 的容器数
    inspect_ok = 0  # 其中 inspect 成功的数;全失败(候选>=2)= 系统性故障,见函数尾
    # 陈旧 FinishedAt 也算 OOM 会在每轮重复出点卡:只认近窗内的退出。
    # 至少 2 个巡检周期,且不低于 5 分钟,容忍调度抖动。
    oom_window = max(2 * settings.sentinel_docker_scan_interval, 300)

    for row in await docker.ps():
        names = row.get("Names") or []
        if not names:
            continue
        name = names[0].lstrip("/")
        image = row.get("Image") or ""
        # 自身/socket 代理跳过(按镜像子串识别,兼容 registry 前缀,不依赖容器命名)
        if _SELF_IMAGE_SUBSTR in image or _SOCKET_PROXY_IMAGE in image:
            continue
        if name in exclude:
            continue

        inspect_candidates += 1
        try:
            info = await docker.inspect_safe(name)
        except Exception:
            # 单容器 inspect 失败(已删除/权限/解析):跳过,不入 active,不中断本轮
            logger.warning("inspect failed for container %s, skipping", name)
            continue
        inspect_ok += 1

        if info.get("RestartPolicy") not in _WATCHED_POLICIES:
            continue  # 批处理/一次性容器不巡检

        state = info.get("State") or {}
        if state.get("Status") == "removing":
            continue  # 正在删除:视作即将消失,既不评估也不入 active(否则会被误判"消失=恢复")

        active_subjects.add(name)
        rc = info.get("RestartCount")
        if rc is not None:
            restart_counts[name] = rc

        restart_policy = info.get("RestartPolicy")
        status = state.get("Status")
        exit_code = state.get("ExitCode")
        oom_killed = state.get("OOMKilled")
        error = state.get("Error") or ""
        finished_at = state.get("FinishedAt")
        health = state.get("Health")

        # --- container_down ---
        if status in {"exited", "dead"} and not state.get("Restarting"):
            # on-failure 且退出码 0:一次性任务正常跑完,不算停机
            if not (restart_policy == "on-failure" and exit_code == 0):
                findings.append(
                    Finding(
                        rule="container_down",
                        subject=name,
                        severity="critical",
                        detail=f"容器 {name} 已停止(status={status}, exit={exit_code})",
                        payload={
                            "status": status,
                            "exit_code": exit_code,
                            "error": error,
                            "finished_at": finished_at,
                            "restart_count": info.get("RestartCount"),
                            "restart_policy": restart_policy,
                        },
                        needs_diagnosis=True,
                    )
                )

        # --- container_unhealthy ---
        if isinstance(health, dict) and health.get("Status") == "unhealthy":
            findings.append(
                Finding(
                    rule="container_unhealthy",
                    subject=name,
                    severity="warning",
                    detail=f"容器 {name} 健康检查失败(health=unhealthy)",
                    payload={"status": status, "health": health.get("Status")},
                    needs_diagnosis=True,
                )
            )

        # --- container_oom (点事件,落库即 resolved) ---
        if emit_oom and oom_killed is True:
            finished_ts = parse_docker_time(finished_at)
            # 0 <= 下界:拒绝未来时间戳(时钟偏移),否则负差恒小于窗口会误触点卡
            if finished_ts is not None and 0 <= now_ts - finished_ts < oom_window:
                findings.append(
                    Finding(
                        rule="container_oom",
                        subject=name,
                        severity="critical",
                        detail=f"容器 {name} 因 OOM 被杀(exit={exit_code})",
                        payload={
                            "exit_code": exit_code,
                            "finished_at": finished_at,
                            "restart_count": info.get("RestartCount"),
                        },
                        needs_diagnosis=True,
                        point=True,
                    )
                )

    # 候选 >=2 且 inspect 全军覆没 = 系统性故障(代理 ACL/socket 权限/API 变更),
    # 不是单容器竞态:向上抛,由 docker_tick 计入连续失败 → 最终升级 scan_failed_docker。
    # 阈值 >=2:单个失败仍按竞态容忍(否则容器扫描期间偶发被删会误升级)。
    if inspect_candidates >= 2 and inspect_ok == 0:
        raise RuntimeError(
            f"all {inspect_candidates} container inspects failed; treating as docker scan failure"
        )

    return findings, active_subjects, restart_counts


# 基线行过期剪枝阈值(秒):消失/长期不更新的容器基线自然清理,表不膨胀。无 knob(YAGNI)。
_BASELINE_TTL = 86400


def detect_crashloops(
    store: Store,
    settings: Settings,
    restart_counts: dict[str, int],
    *,
    now_ts: int,
) -> list[Finding]:
    """RestartCount 跨 tick 滚动(tumbling)窗口 crash-loop 检测。见设计稿 §4。

    每个本轮 active 容器维护基线 (subject, base_count, window_start_ts),按顺序短路:
      1. 无基线(首次观测)→ upsert (rc, now),不报
      2. rc < base_count(容器被重建)→ 重置 (rc, now),不报(对齐 prom resets=0 不报)
      3. delta = rc - base_count >= N → 发 point 卡 + 重置 (rc, now)(开新窗)
      4. now - window_start >= W(窗口到期但 delta<N)→ 重置 (rc, now),不报
      5. 否则(窗口内、delta<N)→ 不动基线,不报(继续累积)
    达阈即报(低延迟,不必等窗口结束);窗口只承担"不够快就清零"。冷却与基线解耦:
    delta>=N 时总是发 Finding 并重置基线,事件是否真落库由 apply_findings 的 cooldown 决定。
    末尾剪枝 window_start_ts < now - _BASELINE_TTL 的行。纯读 restart_counts,只写基线表。
    """
    n = settings.sentinel_docker_crashloop_threshold
    w = settings.sentinel_docker_crashloop_window
    findings: list[Finding] = []
    for name, rc in restart_counts.items():
        baseline = store.get_restart_baseline(name)
        if baseline is None:
            store.upsert_restart_baseline(name, rc, now_ts)
            continue
        base_count, window_start = baseline
        if rc < base_count:  # 重建:必须先于 delta>=N 判定,否则负 delta 误入其它分支
            store.upsert_restart_baseline(name, rc, now_ts)
            continue
        delta = rc - base_count
        if delta >= n:  # 触发优先于窗口到期(§4.1)
            findings.append(
                Finding(
                    rule="container_crashloop",
                    subject=name,
                    severity="warning",
                    detail=f"容器 {name} {w // 60} 分钟内重启 {delta} 次(阈值 {n})",
                    payload={
                        "restart_count": rc,
                        "delta": delta,
                        "window_s": w,
                        "baseline_count": base_count,
                    },
                    needs_diagnosis=True,
                    point=True,
                )
            )
            store.upsert_restart_baseline(name, rc, now_ts)
            continue
        if now_ts - window_start >= w:  # 窗口到期、攒得不够快 → 清零
            store.upsert_restart_baseline(name, rc, now_ts)
            continue
        # 窗口内、delta<N:继续累积,基线不动
    store.prune_restart_baselines(now_ts - _BASELINE_TTL)
    return findings
