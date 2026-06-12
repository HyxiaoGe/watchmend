# RUNBOOK · 用 Uptime Kuma 反监控哨兵自身(dead-man's-switch)

哨兵是边沿触发的告警:平时全绿就沉默。心跳日报(每天 09:00)能给"存在感",但有个盲区——**哨兵进程要是自己挂了,心跳也跟着停**,又回到一片沉默。唯一可靠的办法是让一个**外部**监控盯着它的 `/health`。现成的 Uptime Kuma 正合适。

## 它要监控什么

哨兵容器把 `/health` 暴露在宿主机 `:8765`(容器内 8000)。Kuma 容器在自己的网络里,够不到容器名/宿主 LAN IP,但能走 docker 默认网桥网关到宿主发布端口:

- **URL(实测从 Kuma 容器内可达)**:`http://172.17.0.1:8765/health`
- 健康响应:`{"status":"ok"}`(HTTP 200)

> 172.17.0.1 是 docker0 默认网桥网关(=宿主机),一般稳定不变。若 docker 默认网段被改,或想用更稳的容器名 URL,可把 Kuma 挂到 sentinel 所在的 docker 网络后改用 `http://dev-ops-sentinel:8000/health`(需改 Kuma compose,非必须)。

## 配置步骤(Kuma Web UI,零代码)

1. 打开 Kuma → **Add New Monitor**
2. **Monitor Type**:`HTTP(s) - Keyword`
3. **Friendly Name**:`dev-ops-sentinel 哨兵`
4. **URL**:`http://172.17.0.1:8765/health`
5. **Keyword**:`"status":"ok"`(响应含此串才算 UP;比纯查 2xx 更严)
6. **Heartbeat Interval**:`60` 秒(与哨兵轮询同频)
7. **Retries**:`3`(连续 3 次失败才判 Down,吸收隧道瞬态抖动)
8. **Notifications**:勾选已配好的飞书渠道(发基础设施告警群的那个)
   - 哨兵死了 → Kuma 推 `UptimeKuma Alert: [Down] dev-ops-sentinel 哨兵` 到基础设施群
   - 该群机器人是**关键词 `UptimeKuma`** 模式,Kuma 告警标题自带该词,放行
9. **Save** → 列表里应立刻显示该监控为绿色 UP(因为哨兵正常)

## 验证

- 保存后 Kuma 该条显示 **UP / 绿**;手动 `docker stop dev-ops-sentinel` 几分钟,Kuma 应转 Down 并往基础设施群发 `[Down]`;`docker start` 后恢复发 `[Up]`。(验证完记得把哨兵起回来。)

## 两条腿合在一起

| 腿 | 进哪个群 | 作用 |
|----|----------|------|
| 心跳日报(哨兵自带,每天09:00) | 外部依赖哨兵群 | 存在感 + 全绿汇总(正面确认) |
| Kuma 反监控 `/health` | 基础设施告警群 | 哨兵真死了才报警(dead-man's-switch) |

心跳解决"看不见它",反监控解决"它会不会偷偷死"。
