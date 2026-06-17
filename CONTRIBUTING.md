# Contributing to WatchMend

## 面板 JavaScript 原则(渐进增强 / Progressive Enhancement)

WatchMend 面板早期奉行「零 JavaScript」。自 v0.x 起降级为有边界的**渐进增强原则**——
社区在意的是「轻、安全、可审计、无构建步骤、关 JS 也能用」,而非「一行 script 都没有」。
因此面板里的 JS 必须满足以下全部边界:

1. **关 JS 必须完整可用**:JS 永远只是渐进增强,绝不能成为任何功能的前置条件
   (例:自动刷新在 JS 关闭时用 `<noscript>` 的 `<meta http-equiv="refresh">` 兜底)。
2. **不引入**前端框架、bundler、构建步骤、外部/打包 JS 资源(无 `<script src>`)。
3. 只允许**极小段、内联、可一眼读完的原生 JS**(零依赖)。
4. 凡能用 SSR + CSS(`:target` / `:hover` / `:focus-within`)达成的交互,**优先不写 JS**。

新增任何 JS 前,先确认它通得过以上四条;过不了就用 SSR + CSS 重做。
