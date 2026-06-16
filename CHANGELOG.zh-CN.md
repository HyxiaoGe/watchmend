# 变更日志

WatchMend 的所有重要变更记录于此。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/),
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
版本策略与发版流程见 [RELEASING.md](RELEASING.md)。

English changelog: [CHANGELOG.md](CHANGELOG.md)。

## [Unreleased]

### 新增
- 版本策略、双语变更日志与发版流程文档:
  `CHANGELOG.md` / `CHANGELOG.zh-CN.md`(回填 0.1.0–0.10.0)与 `RELEASING.md`。
- 变更日志 parity 守护测试(`tests/test_changelog.py`)。

## [0.10.0] - 2026-06-16

### 新增
- 零-JS 字段解释气泡:鼠标悬停 / 键盘聚焦任一指标(今日可用率、滚动窗口均值
  7/30/90d、MTTR、p95、基线、阈值、置信度)即弹出解释。纯 CSS
  `:hover` / `:focus-within` popover,无任何 JavaScript。

### 移除
- 静态信任页脚(措辞过宽,且 `localhost-only` 在 LAN 暴露部署上不准确)。

## [0.9.1] - 2026-06-16

### 变更
- 状态栏精简:纯时间刷新指示 + 中性 AI 状态标记。

## [0.9.0] - 2026-06-16

### 新增
- Dozzle 式两行导航壳:行 1 标题 + 工具簇(版本胶囊 · 窗口 · 主题 · 语言);
  行 2 内联 SVG 图标导航(总览 / 服务 / 事件 / 体检)+ 只读态。
- 常驻版本胶囊(右上)。
- 更新检查(默认开):后台轮询 GitHub releases,有新版则版本胶囊亮琥珀点 +
  CSS 弹层给出升级命令与 release 链接。不自更新(尊重 pin tag)。可经
  `SENTINEL_UPDATE_CHECK_ENABLED=false` 或置空 URL 关闭。

## [0.8.0] - 2026-06-16

### 变更
- 总览重设计为 SLO 看板:去重状态环(环段=今日 4 态分布,中心=今日可用率阈值
  色)、环旁 KPI(N/M 服务正常 · 7/30/90d 滚动均值带 Δ 箭头 · 开放事件按严重度
  · 24h 净流 · MTTR)、最差优先服务表(今日 / 7d / 30d 可用率)、汇总行。

### 修复
- SLO 窗口与探针条窗口解耦,默认 window=30 下 d90 真覆盖 90 天(此前被钳成
  d30 克隆);历史不足时如实渲染 `–`。

## [0.7.0] - 2026-06-16

### 变更
- 总览页(`/`)收敛为纯仪表盘:HERO(状态环 + 今日可用率 + 开放异常数 + 趋势
  线)、一行「待关注」(仅未结告警事件,全绿时「✓ 一切正常」)、汇总三卡
  (服务 / 体检 / 日报)链到对应子页。明细交给已有的 `/services`、`/events`、
  `/hygiene`(零功能丢失)。

### 移除
- `services-cap` 死配置与残留死代码 / 孤儿 i18n。

## [0.6.0] - 2026-06-16

### 新增
- 面板重设计 Phase 2——四个零-JS、SSR 子页:`/services`(逐服务 p95
  sparkline,最差优先)、`/service/{name}`(p50/p95 延迟图、阈值对比、状态码
  分桶、可用率热力图、相关事件)、`/events`(service / severity / status 三维
  筛选),`/event/{id}` 重写(置信度环 HERO + 只读证据链 timeline)。`/hygiene`
  展示事件驱动三态本地检查 + 上游依赖 + 哨兵自身姿态。

### 安全
- 上游 statuspage `incident.shortlink`(外部 JSON)经 view 层 http(s) scheme
  白名单,防 `javascript:` / `data:` 落进 `href`。

## [0.5.0] - 2026-06-16

### 新增
- 面板重设计 Phase 1:设计系统(tokens + 组件 CSS,GitHub-dark 调色板,深 / 浅
  / 跟随系统主题)、4-tab 导航壳、HERO 卡(内联 SVG 状态环、大号今日可用率、
  整体趋势线)、逐服务 mini sparkline、可嵌的 shields 风格 `/badge.svg`(面板
  门禁、autoescape),zh/en i18n。

### 变更
- 所有新视觉均为既有读取之上的纯函数——纯 additive,无新表 / 迁移 / store 写入。

## [0.4.0] - 2026-06-15

### 新增
- 服务可选显示名 `label`(`services.yaml`):面板显示 `label or name`,`name`
  仍当稳定 DB key。零持久化、零 schema 变更、完全向后兼容(不配 label = 旧行为)。

## [0.3.2] - 2026-06-15

### 新增
- `container_crashloop` docker-only(未接 Prometheus)crash-loop 检测:按
  `RestartCount` 跨 scan tick 做 tumbling 窗口检测,窗口 W 内重启 ≥ N 次发一张
  带根因诊断的 point 告警卡。基线持久化到新 additive 表
  `container_restart_baseline`(跨 WatchMend 自身重启存活),接 prom + cadvisor
  时由 `metrics_covering` 门禁让位(不双发)。新增可选配置
  `SENTINEL_DOCKER_CRASHLOOP_WINDOW`(默认 600s)、
  `SENTINEL_DOCKER_CRASHLOOP_THRESHOLD`(默认 3)。

## [0.3.1] - 2026-06-14

### 变更
- 证据台信息密度优化(issue #11):默认窗口 90→30 + No-data 降权;健康列表最差
  优先,默认展示前 6 + 「展开剩余 N」;Banner 拆分为主状态行 + 紧凑指标行;详情
  / 最新 / 返回链接携带 `lang/theme/win`;UI 语言 ≠ 诊断生成语言时给出提示。

## [0.3.0] - 2026-06-14

### 新增
- 证据台重设计:界面 i18n(zh/en,表头 / 图例 / 状态 / 规则名;优先级
  query > cookie > 配置默认 > Accept-Language;`SENTINEL_PANEL_DEFAULT_LANG`
  可强制语言)、主题切换(深 / 浅 / 跟随系统,CSS 变量 + `prefers-color-scheme`,
  写 cookie)、逐服务健康柱(5 态机,nodata 优先,uptime 缺失不塌成 down)、宿主
  & 自身自省、分页事件流(内联 AI 诊断摘要)+ 证据详情页(`/event/{id}`,诊断
  hero + 工具调用链)。诊断语言随 `SENTINEL_LLM_LANG`。

### 变更
- 同步 `pyproject.toml` + `uv.lock` 至 0.3.0(消除停在 0.1.1 的 lock drift)。

## [0.2.0] - 2026-06-14

### 新增
- 首个公开里程碑,在 0.1.x 检测 / 通知核心上补齐四块能力:
  - **环境自动发现 + socket 安全**:经只读 docker API(socket-proxy)零配置识别
    在监容器;建议命令永不自动执行。
  - **多通道通知**:飞书富卡片 + Telegram + ntfy + 通用 webhook;任一渠道成功即
    落库;启动期门禁拦截零渠道配置。
  - **只读证据台**(`:8765`):状态机可视化(已分析 / 已恢复)、诊断证据链捕获、
    env 值遮蔽、HTML 转义;写端点受 `SENTINEL_DIAG_TOKEN` 保护。
  - **声明式 LLM 配置**(`llm.yaml`):active/fallback failover、`api_key_env`
    只存变量名(永不存 key)、mtime 热加载免重启、无 / 空配置回落老 `LLM_*` env
    (零 breaking);诊断与日报总结同口径走 failover。

### 变更
- 保留内部包名 `sentinel` 与 `SENTINEL_*` / `FEISHU_*` / `LLM_*` 环境变量前缀
  ——从 0.1.x 升级无需改 env。

## [0.1.1] - 2026-06-13

### 修复
- 干净 VM 开箱即用(全新 VM 验收抓到的两个问题):demo cadvisor 镜像
  v0.49.1→v0.55.1(适配 Docker 29 / API 1.44+,旧版 cadvisor 的 docker factory
  注册失败致容器指标全空);disk-forecast 体检加 24h 历史门槛,新装机只见装机 /
  拉镜像的写盘斜率不再被外推成误报「磁盘即将写满」(历史不足时视为未评估)。

## [0.1.0] - 2026-06-12

### 新增
- WatchMend 首个开源版(由内部 dev-ops-sentinel 改名)。核心监控:外部状态页
  探针、飞书告警 / 恢复、可选 LLM 根因诊断、体检检查、只读证据台。经 CI 发布
  工作流发布容器镜像 `ghcr.io/hyxiaoge/watchmend`。

### 安全
- docker 元数据与环境值的密钥泄漏遮蔽。

[Unreleased]: https://github.com/HyxiaoGe/watchmend/compare/v0.10.0...HEAD
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
