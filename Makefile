.DEFAULT_GOAL := help

export pnpm_config_verify_deps_before_run := false

.PHONY: help init dev dev-detached dev-web dev-api stop restart logs ps \
	migrate bootstrap-admin format format-check lint type-check test test-db check build

help: ## 顯示可用指令
	@awk 'BEGIN {FS = ":.*## "; printf "Daily Insights 開發指令：\n\n"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

init: ## 初始化環境、安裝依賴並啟用 Git hooks
	@command -v git >/dev/null 2>&1 || { echo "找不到 git，請先安裝。"; exit 1; }
	@command -v pnpm >/dev/null 2>&1 || { echo "找不到 pnpm，請先安裝 pnpm 11。"; exit 1; }
	@command -v uv >/dev/null 2>&1 || { echo "找不到 uv，請先安裝 uv。"; exit 1; }
	@if [ ! -f .env ]; then cp .env.example .env; echo "已由 .env.example 建立 .env，啟動服務前請替換 CHANGE_ME。"; else echo "保留現有 .env。"; fi
	pnpm install
	uv sync --project apps/api
	git config core.hooksPath .githooks
	@echo "初始化完成。執行 make dev 前請確認 .env 內容。"

dev: ## 建置並以前景模式啟動完整開發環境
	@test -f .env || { echo "找不到 .env，請先執行 make init。"; exit 1; }
	docker compose up --build

dev-detached: ## 建置並在背景啟動完整開發環境
	@test -f .env || { echo "找不到 .env，請先執行 make init。"; exit 1; }
	docker compose up --build --detach

dev-web: ## 啟動 TanStack Start 開發伺服器
	pnpm dev

dev-api: ## 啟動 FastAPI 開發伺服器（自動重新載入）
	uv run --project apps/api uvicorn daily_insights_api.main:app --reload

stop: ## 停止開發環境
	docker compose down

restart: ## 重新啟動開發環境服務
	docker compose restart

logs: ## 持續顯示開發環境日誌
	docker compose logs --follow

ps: ## 顯示開發環境服務狀態
	docker compose ps

migrate: ## 將 API 資料庫 migration 升級至最新版
	docker compose run --rm api alembic upgrade head

bootstrap-admin: ## 建立初始 admin（需 EMAIL 與 NAME）
	@test -n "$(EMAIL)" || { echo "請提供 EMAIL，例如 make bootstrap-admin EMAIL=admin@example.com NAME='Admin'。"; exit 1; }
	@test -n "$(NAME)" || { echo "請提供 NAME，例如 make bootstrap-admin EMAIL=admin@example.com NAME='Admin'。"; exit 1; }
	docker compose run --rm api python -m daily_insights_api.scripts.bootstrap_admin --email "$(EMAIL)" --display-name "$(NAME)"

format: ## 格式化 Web、API 與文件
	pnpm format

format-check: ## 檢查 Web、API 與文件格式
	pnpm format:check

lint: ## 執行 Web 與 API lint
	pnpm lint:check

type-check: ## 執行 Web 與 API 型別檢查
	pnpm type:check

test: ## 執行 Web 與 API 測試
	pnpm test

test-db: ## 以隔離 PostgreSQL 執行 Web 與 API 完整測試
	./scripts/test-with-postgres.sh

check: ## 執行所有品質檢查、測試與建置
	pnpm check

build: ## 建置 Web 與 API
	pnpm build
