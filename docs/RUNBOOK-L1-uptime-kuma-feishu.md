# L1 Runbook:Uptime Kuma 接飞书(基础设施告警群)

Kuma 1.23.x 内置 Feishu 渠道(发 msg_type=post / 仅 zh_cn / 不签名)。

## 1. 飞书侧:建自定义机器人
- 目标群(你的基础设施告警群)→ 设置 → 群机器人 → 添加「自定义机器人」→ 命名「Uptime Kuma 告警」。
- 安全设置:勾「关键词」,关键词填 `UptimeKuma`(永远在 Kuma 告警标题里)。**不要勾「签名校验」**。
- 复制 webhook URL(https://open.feishu.cn/open-apis/bot/v2/hook/XXXX)。

## 2. Uptime Kuma 侧
1. 登录你的 Kuma Web UI(如 status.example.com 后台)。
2. 右上头像 → Settings → Notifications → Setup Notification。
3. Notification Type 选 **Feishu**;Friendly Name = 「Feishu - 基础设施群」。
4. **Feishu WebHookUrl** 粘贴上面的 URL。
5. 勾「Apply on all existing monitors」→ **Test**(应收到文本测试消息)→ Save。
6. 之后新建的监控需在其 Notifications 里手动勾选本通知。

## 3. 端到端验证
暂停一个测试监控或令其 DOWN → 应收「UptimeKuma Alert: [Down] <name>」,恢复收「[Up]」。

## 排错
- 收不到 + Test 报错:八成机器人开了「签名校验」→ 改成关键词 `UptimeKuma`。
- 详情空白:用了 Lark 国际版(larksuite)webhook;须用大陆 open.feishu.cn。
