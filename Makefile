.DEFAULT_GOAL := help

export pnpm_config_verify_deps_before_run := false

.PHONY: help init dev dev-detached dev-web dev-api stop restart logs ps \
	migrate bootstrap-admin format format-check lint type-check test test-db \
	generate-morning-reports assemble-newsroom newsroom-quality newsroom-reembed newsroom-recover \
	newsroom-replay \
	backfill-index-daily-bars \
	check-index-catalog check-nginx check-production-deployment \
	check build

help: ## 顯示可用指令
	@awk 'BEGIN {FS = ":.*## "; printf "Daily Insights 開發指令：\n\n"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

init: ## 初始化環境、安裝依賴並啟用 Git hooks
	@command -v git >/dev/null 2>&1 || { echo "找不到 git，請先安裝。"; exit 1; }
	@command -v pnpm >/dev/null 2>&1 || { echo "找不到 pnpm，請先安裝 pnpm 11。"; exit 1; }
	@command -v uv >/dev/null 2>&1 || { echo "找不到 uv，請先安裝 uv。"; exit 1; }
	@if [ ! -f .env ]; then cp .env.example .env; echo "已由 .env.example 建立 .env，啟動服務前請替換 CHANGE_ME。"; else echo "保留現有 .env。"; fi
	@if [ ! -f apps/web/.env ]; then cp apps/web/.env.example apps/web/.env; echo "已建立 apps/web/.env。"; else echo "保留現有 apps/web/.env。"; fi
	@if [ ! -f apps/api/.env ]; then cp apps/api/.env.example apps/api/.env; echo "已建立 apps/api/.env。"; else echo "保留現有 apps/api/.env。"; fi
	pnpm install
	uv sync --project apps/api
	git config core.hooksPath .githooks
	@echo "初始化完成。執行 make dev 前請確認根目錄及兩個服務的 .env。"

dev: ## 建置並以前景模式啟動完整開發環境
	@test -f .env || { echo "找不到 .env，請先執行 make init。"; exit 1; }
	@test -f apps/web/.env || { echo "找不到 apps/web/.env，請先執行 make init。"; exit 1; }
	@test -f apps/api/.env || { echo "找不到 apps/api/.env，請先執行 make init。"; exit 1; }
	docker compose up --build

dev-detached: ## 建置並在背景啟動完整開發環境
	@test -f .env || { echo "找不到 .env，請先執行 make init。"; exit 1; }
	@test -f apps/web/.env || { echo "找不到 apps/web/.env，請先執行 make init。"; exit 1; }
	@test -f apps/api/.env || { echo "找不到 apps/api/.env，請先執行 make init。"; exit 1; }
	docker compose up --build --detach

dev-web: ## 啟動 TanStack Start 開發伺服器
	@test -f apps/web/.env || { echo "找不到 apps/web/.env，請先執行 make init。"; exit 1; }
	pnpm dev

dev-api: ## 啟動 FastAPI 開發伺服器（自動重新載入）
	@test -f apps/api/.env || { echo "找不到 apps/api/.env，請先執行 make init。"; exit 1; }
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

generate-morning-reports: ## 本地使用 Twelve Data 單次產生三市場晨報（可傳 EDITION_DATE）
	@test -f .env || { echo "找不到 .env，請先執行 make init。"; exit 1; }
	docker compose run --rm --build api python -m daily_insights_api.scripts.run_morning_reports --once $(if $(EDITION_DATE),--edition-date $(EDITION_DATE),)

assemble-newsroom: ## 本地單次組出三市場重點新聞草稿（需啟用 newsroom，可傳 EDITION_DATE）
	@test -f .env || { echo "找不到 .env，請先執行 make init。"; exit 1; }
	docker compose run --rm --build newsroom-worker python -m daily_insights_api.scripts.run_newsroom_assemble $(if $(EDITION_DATE),--edition-date $(EDITION_DATE),)

newsroom-quality: ## 印出重點新聞品質指標（唯讀，可傳 START、END，預設最近 7 個版次）
	@test -f .env || { echo "找不到 .env，請先執行 make init。"; exit 1; }
	docker compose run --rm --build newsroom-worker python -m daily_insights_api.scripts.run_newsroom_quality $(if $(START),--start $(START),) $(if $(END),--end $(END),)

newsroom-reembed: ## 以目前的 embedding 模型與翻譯重算文章向量（預設收稿中的版次，可傳 START、END）
	@test -f .env || { echo "找不到 .env，請先執行 make init。"; exit 1; }
	docker compose run --rm --build newsroom-worker python -m daily_insights_api.scripts.run_newsroom_reembed $(if $(START),--start $(START),) $(if $(END),--end $(END),)

newsroom-recover: ## 停擺後的重點新聞復原（預設 dry run；CMD=requeue|reembed-untranslated|reopen，ARGS，APPLY=1 才寫入）
	@test -f .env || { echo "找不到 .env，請先執行 make init。"; exit 1; }
	@test -n "$(CMD)" || { echo "請提供 CMD，例如 make newsroom-recover CMD=requeue ARGS='--stage triage --error-code triage_provider_http_402'。"; exit 1; }
	docker compose run --rm --build newsroom-worker python -m daily_insights_api.scripts.run_newsroom_recover $(CMD) $(ARGS) $(if $(APPLY),--apply,)

# The replay runs on the host on purpose: `docker compose run --build` would
# build the production project. It only reads production (read-only exports)
# and writes to its own pgvector container `newsroom-replay-pg` on 127.0.0.1.
NEWSROOM_REPLAY_ROOT ?= $(abspath $(CURDIR)/..)/.newsroom-replay
newsroom-replay: ## 離線重播選稿 (唯讀正式庫; CMD=prepare|run|compare|teardown, START END VARIANT STAGES ARGS)
	NEWSROOM_REPLAY_ROOT="$(NEWSROOM_REPLAY_ROOT)" uv run --project apps/api python -m daily_insights_api.scripts.run_newsroom_replay \
		$(or $(CMD),run) \
		$(if $(filter prepare run,$(or $(CMD),run)),$(if $(START),--start $(START),) $(if $(END),--end $(END),),) \
		$(if $(filter run,$(or $(CMD),run)),$(if $(VARIANT),--variant $(VARIANT),) $(if $(STAGES),--stages $(STAGES),),) \
		$(ARGS)

backfill-index-daily-bars: ## 一次性把指數日 K 灌進資料庫（可傳 PERIOD，預設 2y）
	@test -f .env || { echo "找不到 .env，請先執行 make init。"; exit 1; }
	docker compose run --rm --build -e DAILY_INSIGHTS_YFINANCE_ENABLED=true api \
		python -m daily_insights_api.scripts.run_index_daily_bars --once --period $(or $(PERIOD),2y)

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

check-index-catalog: ## 驗證 Python 與 API client 的指數 catalog 契約
	uv run --project apps/api pytest -c apps/api/pyproject.toml \
		apps/api/tests/test_tracked_index_catalog_contract.py

test-db: ## 以隔離 PostgreSQL 執行 Web 與 API 完整測試
	./scripts/test-with-postgres.sh

check-nginx: ## 驗證 nginx API、signed URL 與 SSE transport 契約
	./scripts/check-nginx-contract.sh

check-production-deployment: ## 離線驗證 production Compose、nginx、GitHub env 與 Docker restart 契約
	./scripts/check-production-deployment-contract.sh

check: check-nginx check-production-deployment ## 執行所有品質檢查、測試、建置與部署契約驗證
	pnpm check

build: ## 建置 Web 與 API
	pnpm build
