.PHONY: demo demo-down demo-logs up down logs test lint leak-check check services-yaml-ready

# ---- 全栈 demo:自带 prometheus/loki/示例服务,.env 里至少配一个通知渠道 ----
demo: .env
	@if grep -q REPLACE_ME .env; then \
		echo "✗ .env 里还有 REPLACE_ME 占位符,替换成真实值再运行"; exit 1; fi
	@if ! grep -qE '^(FEISHU_VENDOR_WEBHOOK|FEISHU_PATROL_WEBHOOK|SENTINEL_TELEGRAM_BOT_TOKEN|SENTINEL_NTFY_URL|SENTINEL_WEBHOOK_URL)=..*' .env; then \
		echo "✗ .env 未配置任何通知渠道:飞书/Telegram/ntfy/webhook 至少填一个再运行"; exit 1; fi
	docker compose -f docker-compose.demo.yml up -d --build
	@echo "✅ demo 已启动:curl http://127.0.0.1:8765/health 验活"
	@echo "   看告警卡: docker compose -f docker-compose.demo.yml stop demo-app(约 15 分钟后出卡)"
	@echo "   心跳卡/体检日报每天 09:00(北京时间)发;当天 09:00 已过则 1 分钟内补发心跳"

demo-down:
	docker compose -f docker-compose.demo.yml down

demo-logs:
	docker compose -f docker-compose.demo.yml logs -f sentinel

# ---- 正式部署:接入你已有的 prometheus/loki 与服务清单 ----
up: .env services-yaml-ready
	@if grep -q REPLACE_ME .env; then \
		echo "✗ .env 里还有 REPLACE_ME 占位符,替换成真实值再运行"; exit 1; fi
	@if ! grep -qE '^(FEISHU_VENDOR_WEBHOOK|FEISHU_PATROL_WEBHOOK|SENTINEL_TELEGRAM_BOT_TOKEN|SENTINEL_NTFY_URL|SENTINEL_WEBHOOK_URL)=..*' .env; then \
		echo "✗ .env 未配置任何通知渠道:飞书/Telegram/ntfy/webhook 至少填一个再运行"; exit 1; fi
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
