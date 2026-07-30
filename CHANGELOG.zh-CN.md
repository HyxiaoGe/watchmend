# 变更日志

WatchMend 的所有重要变更记录于此。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/),
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
版本策略与发版流程见 [RELEASING.md](RELEASING.md)。

English changelog: [CHANGELOG.md](CHANGELOG.md)。

## [Unreleased]

## [0.16.0] - 2026-07-30

### 新增
- 可选的分时通知策略：硬告警仍实时发送，非紧急事件合并进 09:00 日报和
  18:00 摘要。
- 独立的 LiteLLM 兼容上游状态编辑器，支持 `shadow`、`enrich`、`gate`；
  major/critical 事件永不静默，模型失败必回退确定性卡片，分析结果全部留审计。
- 可选的 Loki 新错误指纹扫描与 Restic 备份时效检查。
- 无外部通知副作用的 `shadow` 模式，用于并行迁移验收。

### 变更
- Loki 错误尖峰改为只统计明确的错误级别日志，不再宽泛匹配
  `error|exception|traceback` 子串，并排除 WatchMend 自身日志。
- 服务列表在当前状态优先的前提下，按最近异常时间排序；服务恢复后仍会在近期问题服务中
  优先展示，不再重新混入按名称排序的全绿列表。
- 总览、服务、事件、体检及详情页统一局部刷新运行态数据；顶部新增距最近一次探针采样的
  秒级计时，能直接判断页面数据是否仍在更新。

### 修复
- 新错误指纹在事件页改为按容器聚合筛选，并清理 ANSI 控制符后展示精简摘要；事件详情
  优先展示已经落库的诊断结果。
- 体检页明确区分已启用与按策略关闭的巡检层，并展示状态编辑器的实际运行姿态。

## [0.15.1] - 2026-06-18

### 变更
- **设置**页的「配置一览」重做了排版,更易读:每行改为以友好名称与一行说明为主,
  原始环境变量名降级为下方的小标签;设置按区块分组并可折叠(默认仅展开第一组),
  当前生效 / 回退的诊断模型以「来源标签 + 模型名」呈现。九项凭证仍只显示
  已配置 / 未配置状态,绝不渲染其值。

### 修复
- **设置**页可访问性:显示偏好的选项标签现在带有键盘聚焦框,浅色主题下
  「已配置」状态与标签颜色满足 WCAG AA 对比度。

## [0.15.0] - 2026-06-18

### 新增
- 新增**设置**页 `/settings`,从页头齿轮(⚙)进入。两个只读区块,关闭 JavaScript
  也完全可用:
  - **显示偏好**——选择面板语言、主题、历史窗口与自动刷新间隔。选择经一个普通 GET
    表单按浏览器存为 cookie;选「自动 / 默认」会清除该偏好、回落服务端默认值。
  - **配置清单**——按环境变量名逐项列出全部配置(分组展示)及其生效值。九个凭证类
    配置(token、key、签名密钥、webhook)只显示「已配置 / 未配置」状态,绝不渲染其值。
- 面板自动刷新间隔现可在设置页按浏览器覆盖,作用于实时页(总览、服务、事件、巡检);
  详情页与更新日志页一如既往保持静态。设为「关闭」即停止该浏览器的自动刷新。

## [0.14.0] - 2026-06-17

### 变更
- 面板自动刷新现在无闪烁。开启 JavaScript 时,一小段内联脚本就地刷新页面内容——只替换正文
  区并同步标题与状态行——不再整页重载,滚动位置和已打开的更新日志模态都得以保留,页面也不
  再每隔一段就闪一下。关闭 JavaScript 时经 `<noscript>` 退回原先的整页 `<meta refresh>`,
  功能无损。
- 面板长期奉行的「零 JavaScript」规则,正式降级为有边界的渐进增强原则(见 `CONTRIBUTING.md`):
  面板关 JS 必须完整可用,不引入框架/bundler/构建步骤/外部 JS,只允许极小段内联原生脚本。

### 新增
- 新增可选配置 `SENTINEL_PANEL_REFRESH_SECONDS`(默认 30,最小 5),统一控制面板刷新间隔
  ——实时轮询与 `<noscript>` 兜底都用它。旧镜像忽略此项,可干净回滚。

## [0.13.3] - 2026-06-17

### 变更
- 面板头部现在只显示 WatchMend 字标——品牌识别交给 logo,去掉「证据台」描述词。
- 浏览器 tab 标题改为页面感知:`{页面名} · WatchMend`(总览 / 服务 / 事件 / 体检 /
  更新日志),固定/后台 tab 可区分。并新增状态灯前缀 `🔴 {n} · `,仅在有未结事件时出现
  ——平时安静、出事才亮红——复用与状态徽标相同的未结事件信号。移除原先「●」更新标记;
  有新版仍在版本 chip 显示(圆点 + 模态横幅)。

## [0.13.2] - 2026-06-17

### 新增
- 面板头部在标题旁新增 WatchMend 品牌标识——一枚内联的 "WM pulse" SVG mark(点击回
  首页),并配套同款 SVG favicon。二者均为内联(零 JavaScript、无外部请求、无 `<img>`),
  不新增任何路由或静态挂载,随镜像发布、可干净回滚。

## [0.13.1] - 2026-06-17

### 变更
- 头部版本 chip 改为点击弹出居中的零-JS 更新日志模态(`:target` 浮层 + 暗背景遮罩,
  点遮罩 / × 关闭),取代原先那个跳转独立页面的小下拉。全量历史内联可滚动,当前运行
  版本高亮,有新版时顶部置更新横幅;`/changelog` 整页保留作可分享的永久链接。

## [0.13.0] - 2026-06-17

### 变更
- 头部版本 chip 改为点击展开的更新日志弹层(原先是看不出可点的链接)。版本徽标
  变成零-JS 折叠层,内联展示当前运行版本的「本次更新」(带 ▾ 提示与 hover 态),
  并保留「完整更新日志」链接;`/changelog` 整页加面包屑回总览。

## [0.12.1] - 2026-06-17

### 安全
- 日志脱敏 ReDoS 加固:诊断日志脱敏器的连接串正则在超长、无空白、无闭合 `@` 的
  `scheme://user:pass` 串上呈二次回溯,经 `loki_logs` / `docker_logs` 喂入的对抗性
  日志行可能让事件循环卡顿数秒。现对 scheme / user / 密码三段全部长度收界,脱敏恢复
  线性;真实连接串不受影响。

## [0.12.0] - 2026-06-17

### 新增
- 事件 ↔ 服务导航:事件流每个事件链向其服务详情页,服务详情亦反向链回其筛选后的
  事件列表——双向下钻,且不丢失当前窗口 / 筛选上下文。

### 修复
- 事件时间戳带上日期,不再只显示裸 `HH:MM`,旧事件读起来不再有歧义。
- docker 重启动作:容器匹配现在先把 `docker ps` 输出落地再匹配,根除可能误拒合法
  重启的 `pipefail` / `SIGPIPE` 竞态。

### 安全
- 诊断日志脱敏:喂给 LLM 诊断工具(`docker_logs` / `loki_logs`)的容器 / Loki 日志
  输出,现在在出内网前、以及落证据链 / 面板展示前先 scrub 密钥。两层:精确匹配
  WatchMend 自有与目标容器自有的密钥值(其本身绝不外泄),加保守形态正则(API key、
  JWT、Bearer / Basic 凭据、`key=value` / JSON 赋值、连接串密码、PEM 私钥块)。模型
  与证据链拿到的是同一份脱敏后文本。

## [0.11.1] - 2026-06-17

### 新增
- 国内 / 镜像版部署:独立的 `docker-compose.image.yml` 直拉预构建镜像
  `ghcr.io/hyxiaoge/watchmend`(不本地构建),自带只读 `lscr.io/linuxserver/socket-proxy`
  边车——二者国内均可达(Docker Hub 被墙)——并在 README 增加开箱即用部署段。
- 发版守护:`tests/test_compose_image.py` 钉住 `docker-compose.image.yml` 的镜像
  tag 与 `pyproject.toml` 版本一致。

### 修复
- `services.yaml` 解析:空文件、裸 `services:`、缺 `services` 键现在安全回落
  「仅外部状态页」模式,而非让容器 crash-loop;`services` 写成非列表标量仍响亮失败。
- docker 巡检自排除现在也匹配 `linuxserver/socket-proxy`(lscr)fork,镜像版 compose
  自带的 socket 代理边车不再被误报为 down 容器。

## [0.11.0] - 2026-06-16

### 新增
- 更新日志面板:零-JS 的 `/changelog` 页离线展示本次构建的更新说明(及完整历史)。
  双语变更日志已烤进镜像,无需联网即可查看。版本胶囊点击进入此页。
- 更新标记:有新版可升级时,浏览器标签标题加 `●` 前缀,后台标签在下次刷新时即提示更新。

## [0.10.1] - 2026-06-16

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

[Unreleased]: https://github.com/HyxiaoGe/watchmend/compare/v0.16.0...HEAD
[0.16.0]: https://github.com/HyxiaoGe/watchmend/compare/v0.15.1...v0.16.0
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
