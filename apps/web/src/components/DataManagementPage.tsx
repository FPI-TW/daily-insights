import {
  ApiError,
  type DataManagementRun,
  type Locale,
} from "@daily-insights/api-client"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Dialog } from "#/components/Dialog"
import { MacroSourceDiagnostics } from "#/components/MacroSourceDiagnostics"
import { TaiwanSourceDiagnostics } from "#/components/TaiwanSourceDiagnostics"
import { browserAdministrationClient } from "#/lib/admin-members"
import { requireCsrfToken } from "#/lib/auth"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"

const catalogKey = ["data-management", "catalog"] as const
const runsKey = ["data-management", "runs"] as const
const ACTIVE_STATUSES = ["pending", "running"]
const PROVIDERS = ["twelve_data", "yahoo_finance", "twse"] as const
type RerunnableProvider = (typeof PROVIDERS)[number]

type RunInput =
  | { operation: "morning_all" }
  | {
      operation: "provider_rerun"
      provider: RerunnableProvider
    }

export function DataManagementPage({ locale }: { locale: Locale }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const redirectExpired = useSessionExpiryRedirect(locale, "admin")
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [page, setPage] = useState(1)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const fullRerunRef = useRef<HTMLButtonElement>(null)
  const closeConfirmation = () => {
    setConfirmOpen(false)
    window.requestAnimationFrame(() => fullRerunRef.current?.focus())
  }
  const catalog = useQuery({
    queryKey: catalogKey,
    queryFn: () => browserAdministrationClient().dataManagementCatalog(),
  })
  const runs = useQuery({
    queryKey: [...runsKey, { operationGroup: "all", page }],
    queryFn: () => browserAdministrationClient().listDataManagementRuns(page),
    refetchInterval: query =>
      query.state.data?.active_runs?.some(run =>
        ["pending", "running", "cancelled"].includes(run.status)
      )
        ? 2_000
        : false,
  })
  const enqueue = useMutation({
    mutationFn: async (input: RunInput) =>
      browserAdministrationClient().createDataManagementRun(
        input,
        await requireCsrfToken()
      ),
    onSuccess: () => {
      setPage(1)
      void queryClient.invalidateQueries({ queryKey: runsKey })
      setConfirmOpen(false)
    },
  })
  const cancelRun = useMutation({
    mutationFn: async (runId: string) =>
      browserAdministrationClient().cancelDataManagementRun(
        runId,
        await requireCsrfToken()
      ),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: runsKey }),
  })

  const error = enqueue.error
  const errorMessage =
    error instanceof ApiError && error.status === 409
      ? t("dataManagementConflict")
      : error instanceof ApiError && error.status === 503
        ? t("dataManagementUnavailable")
        : error
          ? t("dataManagementFailed")
          : ""
  const submit = async (input: RunInput) => {
    try {
      await enqueue.mutateAsync(input)
    } catch (caught) {
      await redirectExpired(caught)
    }
  }

  if (catalog.isPending || runs.isPending) {
    return (
      <main className="page-shell" role="status" aria-live="polite">
        <span className="sr-only">{t("dataManagementLoading")}</span>
        <div className="h-10 w-72 animate-pulse rounded bg-link-hover" />
        <div className="mt-6 h-64 max-w-5xl animate-pulse rounded-xl bg-link-hover" />
      </main>
    )
  }

  const activeRuns =
    runs.data?.active_runs ??
    (runs.data?.items ?? []).filter(run => ACTIVE_STATUSES.includes(run.status))
  const activeRunsOutsidePage = activeRuns.filter(
    run => !(runs.data?.items ?? []).some(item => item.id === run.id)
  )
  const rerunnableProviders = (catalog.data?.rerunnable_providers ?? []).filter(
    (provider): provider is RerunnableProvider =>
      PROVIDERS.includes(provider as RerunnableProvider)
  )
  const activeProvider = (provider: RerunnableProvider) =>
    activeRuns.some(run => {
      if (run.operation === "morning_all") return true
      if (run.operation === "provider_rerun") return run.provider === provider
      if (provider === "twelve_data") return run.operation === "morning_market"
      if (provider === "yahoo_finance") return run.operation === "index_yahoo"
      return run.operation === "institutional_twse"
    })
  const activeMorningProvider = activeRuns.some(run =>
    [
      "morning_all",
      "morning_market",
      "index_yahoo",
      "institutional_twse",
      "provider_rerun",
    ].includes(run.operation)
  )
  const providerLabel = (provider: RerunnableProvider) =>
    t(`dataManagementProvider_${provider}`)
  const runScope = (run: DataManagementRun) =>
    run.operation === "provider_rerun"
      ? providerLabel(run.provider)
      : (run.market_code ?? t("dataManagementAllProviders"))
  return (
    <main className="page-shell">
      <header className="mb-8 max-w-3xl">
        <p className="eyebrow">{t("adminPortal")}</p>
        <h1 className="mt-2 text-[clamp(1.9rem,4vw,2.5rem)] leading-none font-extrabold tracking-[-0.045em]">
          {t("dataManagementTitle")}
        </h1>
        <p className="mt-3 leading-7 text-sea-ink-soft">
          {t("dataManagementDescription")}
        </p>
      </header>
      {errorMessage ? (
        <p role="alert" className="mb-4 font-bold text-market-up">
          {errorMessage}
        </p>
      ) : null}
      <div className="grid max-w-5xl gap-5 lg:grid-cols-2">
        <section
          className="surface-panel p-5"
          aria-labelledby="full-rerun-title"
        >
          <h2 id="full-rerun-title" className="m-0 text-lg font-extrabold">
            {t("dataManagementFull")}
          </h2>
          <p className="mt-2 text-sm leading-6 text-sea-ink-soft">
            {t("dataManagementFullDescription")}
          </p>
          <button
            type="button"
            ref={fullRerunRef}
            className="primary-action mt-4"
            disabled={
              Boolean(activeMorningProvider) ||
              enqueue.isPending ||
              !catalog.data?.morning_reports_enabled ||
              !catalog.data?.yfinance_enabled ||
              !catalog.data?.twse_enabled
            }
            onClick={() => setConfirmOpen(true)}
          >
            {t("dataManagementFullAction")}
          </button>
        </section>
        <section
          className="surface-panel p-5"
          aria-labelledby="provider-rerun-title"
        >
          <h2 id="provider-rerun-title" className="m-0 text-lg font-extrabold">
            {t("dataManagementProvider")}
          </h2>
          <p className="mt-2 text-sm leading-6 text-sea-ink-soft">
            {t("dataManagementProviderDescription")}
          </p>
          <div className="mt-4 grid gap-2">
            {rerunnableProviders.map(provider => {
              const enabled =
                provider === "twelve_data"
                  ? catalog.data?.morning_reports_enabled
                  : provider === "yahoo_finance"
                    ? catalog.data?.yfinance_enabled
                    : catalog.data?.twse_enabled
              return (
                <button
                  key={provider}
                  type="button"
                  className="secondary-action text-left"
                  disabled={
                    activeProvider(provider) || enqueue.isPending || !enabled
                  }
                  onClick={() =>
                    void submit({
                      operation: "provider_rerun",
                      provider,
                    })
                  }
                >
                  {providerLabel(provider)}
                </button>
              )
            })}
          </div>
        </section>
      </div>
      <section
        className="surface-panel mt-6 max-w-5xl p-5"
        aria-labelledby="latest-runs-title"
      >
        {activeRunsOutsidePage.length > 0 ? (
          <div className="mb-5 border-b border-line pb-5" aria-live="polite">
            <h2 className="m-0 text-lg font-extrabold">
              {t("dataManagementActive")}
            </h2>
            <div className="mt-3 grid gap-2">
              {activeRunsOutsidePage.map(run => (
                <div key={run.id} className="rounded-md border border-line p-3">
                  <p className="m-0 font-bold">
                    {run.status} · {run.operation} · {runScope(run)}
                  </p>
                  <button
                    type="button"
                    className="secondary-action mt-3"
                    disabled={cancelRun.isPending}
                    onClick={() =>
                      void cancelRun.mutateAsync(run.id).catch(redirectExpired)
                    }
                  >
                    {t("dataManagementCancel")}
                  </button>
                </div>
              ))}
            </div>
          </div>
        ) : null}
        <h2 id="latest-runs-title" className="m-0 text-lg font-extrabold">
          {t("dataManagementLatest")}
        </h2>
        <div className="mt-4 grid gap-2">
          {runs.data?.items.map(run => (
            <details key={run.id} className="rounded-md border border-line p-3">
              <summary className="cursor-pointer font-bold">
                {run.status} · {run.operation} · {runScope(run)} ·{" "}
                {run.edition_date}
                {run.completed_at
                  ? ` · ${new Intl.DateTimeFormat(locale, { dateStyle: "short", timeStyle: "short" }).format(new Date(run.completed_at))}`
                  : ""}
              </summary>
              {run.operation === "macro_dashboard" ? (
                <MacroSourceDiagnostics result={run.result} error={run.error} />
              ) : run.operation === "institutional_twse" ||
                (run.operation === "provider_rerun" &&
                  run.provider === "twse") ? (
                <TaiwanSourceDiagnostics
                  result={run.result}
                  error={run.error}
                />
              ) : (
                <RunDetail result={run.result} error={run.error} />
              )}
              {ACTIVE_STATUSES.includes(run.status) ? (
                <button
                  type="button"
                  className="secondary-action mt-3"
                  disabled={cancelRun.isPending}
                  onClick={() =>
                    void cancelRun.mutateAsync(run.id).catch(redirectExpired)
                  }
                >
                  {t("dataManagementCancel")}
                </button>
              ) : null}
            </details>
          ))}
        </div>
        <RunPagination
          page={runs.data?.page ?? 1}
          hasMore={runs.data?.has_more ?? false}
          onPrevious={() => setPage(current => Math.max(1, current - 1))}
          onNext={() => setPage(current => current + 1)}
          previousLabel={t("dataManagementPagePrevious")}
          nextLabel={t("dataManagementPageNext")}
          statusLabel={t("dataManagementPageStatus", {
            page: runs.data?.page ?? 1,
          })}
        />
      </section>
      <Dialog
        open={confirmOpen}
        onClose={closeConfirmation}
        labelledBy="full-rerun-confirmation"
        initialFocusRef={cancelRef}
        role="alertdialog"
      >
        <h2 id="full-rerun-confirmation" className="m-0 text-lg font-extrabold">
          {t("dataManagementConfirmTitle")}
        </h2>
        <p className="mt-3 text-sm leading-6 text-sea-ink-soft">
          {t("dataManagementConfirmWarning", {
            date: catalog.data?.taipei_date,
            providers: rerunnableProviders.map(providerLabel).join(", "),
          })}
        </p>
        <div className="mt-5 flex justify-end gap-3">
          <button
            ref={cancelRef}
            type="button"
            className="secondary-action"
            onClick={closeConfirmation}
          >
            {t("dismiss")}
          </button>
          <button
            type="button"
            className="primary-action"
            disabled={enqueue.isPending}
            onClick={() => void submit({ operation: "morning_all" })}
          >
            {t("dataManagementConfirmAction")}
          </button>
        </div>
      </Dialog>
    </main>
  )
}

function RunDetail({
  result,
  error,
}: {
  result: Record<string, unknown> | null
  error: string | null
}) {
  const { t } = useTranslation()
  const walk = (value: unknown) =>
    value && typeof value === "object"
      ? (value as Record<string, unknown>)
      : null
  const markets = Array.isArray(result?.markets) ? result.markets : null
  const symbols = Array.isArray(result?.symbols) ? result.symbols : null
  const morning = walk(result?.morning)
  const index = walk(result?.index)
  const providers = walk(result?.providers)
  if (providers) {
    return (
      <div className="mt-3 grid gap-3 text-sm text-sea-ink-soft">
        {PROVIDERS.map(provider => {
          const entry = walk(providers[provider])
          if (!entry) return null
          const details = walk(entry.details)
          return (
            <div key={provider}>
              <p className="m-0 font-bold text-sea-ink">
                {t(`dataManagementProvider_${provider}`)} ·{" "}
                {String(entry.status ?? "—")}
                {entry.error ? ` · error: ${String(entry.error)}` : ""}
              </p>
              {details ? <RunDetail result={details} error={null} /> : null}
            </div>
          )
        })}
      </div>
    )
  }
  if (markets) {
    return (
      <div className="mt-3 grid gap-3 text-sm text-sea-ink-soft">
        {markets.map((market, index) => {
          const item = market as Record<string, unknown>
          const datasets = Array.isArray(item.datasets) ? item.datasets : []
          return (
            <div key={`${String(item.market_code)}-${index}`}>
              <p className="m-0 font-bold text-sea-ink">
                {String(item.market_code)} · {String(item.publication_action)} ·
                r{String(item.revision ?? "—")}
              </p>
              <p className="mt-1">
                {`report_status: ${String(item.report_status ?? "—")} · source_date: ${String(item.source_date ?? "—")}`}
              </p>
              <ul className="mt-1 list-disc pl-5">
                {datasets.map((dataset, datasetIndex) => {
                  const entry = dataset as Record<string, unknown>
                  return (
                    <li key={`${String(entry.dataset_key)}-${datasetIndex}`}>
                      {`${String(entry.dataset_key)} · ${String(entry.status)} · record_count: ${String(entry.record_count ?? "—")} · fetched_at: ${String(entry.fetched_at ?? "—")} · source_as_of: ${String(entry.source_as_of ?? "—")}${entry.error ? ` · error: ${String(entry.error)}` : ""}`}
                    </li>
                  )
                })}
              </ul>
            </div>
          )
        })}
      </div>
    )
  }
  const stockFlows = walk(result?.stock_flows)
  const marketFlows = walk(result?.market_flows)
  if (morning || index) {
    const morningDetails = walk(morning?.details)
    const indexDetails = walk(index?.details)
    return (
      <div className="mt-3 grid gap-3 text-sm text-sea-ink-soft">
        {morning ? (
          <div>
            <p className="m-0">
              {`morning report · ${String(morning.status ?? "—")}${morning.error ? ` · error: ${String(morning.error)}` : ""}`}
            </p>
            {morningDetails ? (
              <RunDetail result={morningDetails} error={null} />
            ) : null}
          </div>
        ) : null}
        {index ? (
          <div>
            <p className="m-0">
              {`international indices · ${String(index.status)}${index.error ? ` · error: ${String(index.error)}` : ""}`}
            </p>
            {indexDetails ? (
              <RunDetail result={indexDetails} error={null} />
            ) : null}
          </div>
        ) : null}
      </div>
    )
  }
  if (stockFlows || marketFlows) {
    return (
      <div className="mt-3 grid gap-3 text-sm text-sea-ink-soft">
        {[
          ["stock_flows", stockFlows],
          ["market_flows", marketFlows],
        ].map(([key, walk]) => {
          const entry = walk as Record<string, unknown> | null
          if (!entry) return null
          const days = Array.isArray(entry.days) ? entry.days : []
          return (
            <div key={String(key)}>
              <p className="m-0 font-bold text-sea-ink">
                {`${String(key)} · covered_trading_days: ${String(entry.covered_trading_days ?? "—")} / ${String(entry.lookback_trading_days ?? "—")}${entry.aborted ? " · aborted" : ""}`}
              </p>
              <ul className="mt-1 list-disc pl-5">
                {days.map((day, index) => {
                  const item = day as Record<string, unknown>
                  return (
                    <li key={`${String(item.trade_date)}-${index}`}>
                      {`${String(item.trade_date)} · ${String(item.status)}${item.record_count === undefined ? "" : ` · record_count: ${String(item.record_count)}`}${item.error ? ` · error: ${String(item.error)}` : ""}`}
                    </li>
                  )
                })}
              </ul>
            </div>
          )
        })}
      </div>
    )
  }
  if (symbols) {
    return (
      <ul className="mt-3 list-disc pl-5 text-sm text-sea-ink-soft">
        {symbols.map((symbol, index) => {
          const item = symbol as Record<string, unknown>
          return (
            <li key={`${String(item.symbol)}-${index}`}>
              {`Yahoo Finance · ${String(item.symbol)} · ${String(item.status)} · record_count: ${String(item.record_count ?? "—")} · fetched_at: ${String(item.fetched_at ?? "—")} · source_as_of: ${String(item.source_as_of ?? "—")}${item.error ? ` · error: ${String(item.error)}` : ""}`}
            </li>
          )
        })}
      </ul>
    )
  }
  return (
    <p className="mt-3 text-sm text-sea-ink-soft">
      {error ?? JSON.stringify(result ?? {})}
    </p>
  )
}

function RunPagination({
  page,
  hasMore,
  onPrevious,
  onNext,
  previousLabel,
  nextLabel,
  statusLabel,
}: {
  page: number
  hasMore: boolean
  onPrevious: () => void
  onNext: () => void
  previousLabel: string
  nextLabel: string
  statusLabel: string
}) {
  if (page === 1 && !hasMore) return null
  return (
    <nav
      className="mt-5 flex items-center justify-between gap-3"
      aria-label={statusLabel}
    >
      <button
        type="button"
        className="secondary-action"
        disabled={page === 1}
        onClick={onPrevious}
      >
        {previousLabel}
      </button>
      <span className="text-sm font-bold text-sea-ink-soft">{statusLabel}</span>
      <button
        type="button"
        className="secondary-action"
        disabled={!hasMore}
        onClick={onNext}
      >
        {nextLabel}
      </button>
    </nav>
  )
}
