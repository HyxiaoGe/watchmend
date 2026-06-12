.PHONY: demo demo-down demo-logs up down logs test lint leak-check check

# ---- 全栈 demo:自带 prometheus/loki/示例服务,只需 .env 里一个飞书 webhook ----
demo: .env
	docker compose -f docker-compose.demo.yml up -d --build
	@echo "✅ demo 已启动:1-2 分钟内会收到第一张飞书卡"
	@echo "   健康检查: curl http://127.0.0.1:8765/health"
	@echo "   制造一次告警试试: docker compose -f docker-compose.demo.yml stop demo-app"

demo-down:
	docker compose -f docker-compose.demo.yml down

demo-logs:
	docker compose -f docker-compose.demo.yml logs -f sentinel

# ---- 正式部署:接入你已有的 prometheus/loki 与服务清单 ----
up: .env services.yaml
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

# ---- 配置文件脚手架 ----
.env:
	cp .env.example .env
	@echo "已生成 .env:请填入 FEISHU_VENDOR_WEBHOOK(必填)"

services.yaml:
	cp services.example.yaml services.yaml
	@echo "已生成 services.yaml:请按你自己的服务编辑探针清单"
