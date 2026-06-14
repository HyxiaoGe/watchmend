.PHONY: demo demo-down demo-logs up down logs test lint leak-check check services-yaml-ready llm-yaml-ready llm-init llm-switch llm-list

# ---- 全栈 demo:自带 prometheus/loki/示例服务,.env 里至少配一个通知渠道 ----
demo: .env llm-yaml-ready
	@if grep -q REPLACE_ME .env; then \
		echo "✗ .env 里还有 REPLACE_ME 占位符,替换成真实值再运行"; exit 1; fi
	@if ! { grep -qE '^(FEISHU_VENDOR_WEBHOOK|FEISHU_PATROL_WEBHOOK|SENTINEL_NTFY_URL|SENTINEL_WEBHOOK_URL)=..*' .env \
		|| { grep -qE '^SENTINEL_TELEGRAM_BOT_TOKEN=..*' .env && grep -qE '^SENTINEL_TELEGRAM_CHAT_ID=..*' .env; }; }; then \
		echo "✗ .env 未配置任何通知渠道:飞书/Telegram(需 token+chat_id)/ntfy/webhook 至少填一个再运行"; exit 1; fi
	docker compose -f docker-compose.demo.yml up -d --build
	@echo "✅ demo 已启动:curl http://127.0.0.1:8765/health 验活"
	@echo "   看告警卡: docker compose -f docker-compose.demo.yml stop demo-app(约 15 分钟后出卡)"
	@echo "   心跳卡/体检日报每天 09:00(北京时间)发;当天 09:00 已过则 1 分钟内补发心跳"

demo-down:
	docker compose -f docker-compose.demo.yml down

demo-logs:
	docker compose -f docker-compose.demo.yml logs -f sentinel

# ---- 正式部署:接入你已有的 prometheus/loki 与服务清单 ----
up: .env services-yaml-ready llm-yaml-ready
	@if grep -q REPLACE_ME .env; then \
		echo "✗ .env 里还有 REPLACE_ME 占位符,替换成真实值再运行"; exit 1; fi
	@if ! { grep -qE '^(FEISHU_VENDOR_WEBHOOK|FEISHU_PATROL_WEBHOOK|SENTINEL_NTFY_URL|SENTINEL_WEBHOOK_URL)=..*' .env \
		|| { grep -qE '^SENTINEL_TELEGRAM_BOT_TOKEN=..*' .env && grep -qE '^SENTINEL_TELEGRAM_CHAT_ID=..*' .env; }; }; then \
		echo "✗ .env 未配置任何通知渠道:飞书/Telegram(需 token+chat_id)/ntfy/webhook 至少填一个再运行"; exit 1; fi
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f sentinel

# ---- 开发 ----
test:
	uv run pytest -q

lint:
	uv run ruff check . && uv run ruff format --check .

leak-check:
	bash scripts/leak_check.sh

check: lint test leak-check

# ---- 配置文件脚手架:首次生成后停下来,等用户填完再跑 ----
.env:
	@cp .env.example .env
	@echo "✗ 已生成 .env:至少配置一个通知渠道(飞书/Telegram/ntfy/webhook)后重新运行"
	@exit 1

services-yaml-ready:
	@if [ -d services.yaml ]; then \
		rmdir services.yaml; \
		echo "(已清理 docker 误建的 services.yaml 空目录)"; fi
	@if [ ! -f services.yaml ]; then \
		cp services.example.yaml services.yaml; \
		echo "✗ 已生成 services.yaml(示例内容):改成你自己的服务清单后重新 make up"; \
		echo "  (不需要内部探针的话,删掉 services 列表里的条目、保留空列表即可)"; exit 1; fi

# ---- LLM provider 配置(可选):改 llm.yaml 下一轮诊断生效,无需重启 ----
# make up/demo 先经此预置空占位 llm.yaml(被 compose 挂进容器);留空=回落 .env 的 LLM_*。
llm-yaml-ready:
	@if [ -d llm.yaml ]; then \
		rmdir llm.yaml 2>/dev/null && echo "(已清理 docker 误建的 llm.yaml 空目录)" \
			|| echo "⚠ ./llm.yaml 是非空目录,无法清理;host make llm-init/switch 将无效,请手动处理"; fi
	@if [ ! -e llm.yaml ]; then \
		printf '# llm.yaml — WatchMend LLM provider 注册表(可选;空=回落 .env 的 LLM_* 变量)\n# make llm-init 加 provider、make llm-switch name=<x> 切 active;详见 llm.example.yaml\n' > llm.yaml; \
		echo "(已预置空 llm.yaml 占位:make llm-init 配置容器内直连诊断,或留空走 .env LLM_*)"; fi

llm-init:
	uv run python -m sentinel.llm_config init

llm-switch:
	@if [ -z "$(name)" ]; then echo "用法: make llm-switch name=<provider>"; exit 1; fi
	uv run python -m sentinel.llm_config switch $(name)

llm-list:
	uv run python -m sentinel.llm_config list
