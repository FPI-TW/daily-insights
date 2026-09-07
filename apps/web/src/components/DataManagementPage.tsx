import { ApiError, type Locale } from "@daily-insights/api-client"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { LoaderCircle } from "lucide-react"
import { useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Dialog } from "#/components/Dialog"
import { browserAdministrationClient } from "#/lib/admin-members"
import { requireCsrfToken } from "#/lib/auth"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"

const catalogKey = ["data-management", "catalog"] as const
const runsKey = ["data-management", "runs"] as const

export function DataManagementPage({ locale }: { locale: Locale }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const redirectExpired = useSessionExpiryRedirect(locale, "admin")
  const [confirmOpen, setConfirmOpen] = useState(false)
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
    queryKey: runsKey,
    queryFn: () => browserAdministrationClient().listDataManagementRuns(),
    refetchInterval: query =>
      query.state.data?.items.some(run =>
        ["pending", "running"].includes(run.status)
      )
        ? 2_000
        : false,
  })
  const enqueue = useMutation({
    mutationFn: async (
      input:
        | { operation: "morning_all" }
        | {
            operation: "morning_market"
            market_code: "global_macro_bonds" | "crypto" | "us_equity"
          }
        | { operation: "index_yahoo" }
    ) =>
      browserAdministrationClient().createDataManagementRun(
        input,
        await requireCsrfToken()
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: runsKey })
      setConfirmOpen(false)
    },
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
  const submit = async (
    input:
      | { operation: "morning_all" }
      | {
          operation: "morning_market"
          market_code: "global_macro_bonds" | "crypto" | "us_equity"
        }
      | { operation: "index_yahoo" }
  ) => {
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

  const activeMorning = runs.data?.items.some(
    run =>
      run.operation !== "index_yahoo" &&
      ["pending", "running"].includes(run.status)
  )
  const activeIndex = runs.data?.items.some(
    run =>
      run.operation === "index_yahoo" &&
      ["pending", "running"].includes(run.status)
  )
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
      <div className="grid max-w-5xl gap-5 lg:grid-cols-3">
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
              Boolean(activeMorning) ||
              enqueue.isPending ||
              !catalog.data?.morning_reports_enabled
            }
            onClick={() => setConfirmOpen(true)}
          >
            {t("dataManagementFullAction")}
          </button>
        </section>
        <section
          className="surface-panel p-5"
          aria-labelledby="market-rerun-title"
        >
          <h2 id="market-rerun-title" className="m-0 text-lg font-extrabold">
            {t("dataManagementMarket")}
          </h2>
          <div className="mt-4 grid gap-2">
            {catalog.data?.markets.map(market => (
              <button
                key={market}
                type="button"
                className="secondary-action text-left"
                disabled={
                  Boolean(activeMorning) ||
                  enqueue.isPending ||
                  !catalog.data?.morning_reports_enabled
                }
                onClick={() =>
                  void submit({
                    operation: "morning_market",
                    market_code: market,
                  })
                }
              >
                {t(`reportMarket_${market}`)}
              </button>
            ))}
          </div>
        </section>
        <section
          className="surface-panel p-5"
          aria-labelledby="index-rerun-title"
        >
          <h2 id="index-rerun-title" className="m-0 text-lg font-extrabold">
            {t("dataManagementIndex")}
          </h2>
          <p className="mt-2 text-sm leading-6 text-sea-ink-soft">
            {t("dataManagementIndexDescription")}
          </p>
          <button
            type="button"
            className="primary-action mt-4"
            disabled={
              Boolean(activeIndex) ||
              enqueue.isPending ||
              !catalog.data?.yfinance_enabled
            }
            onClick={() => void submit({ operation: "index_yahoo" })}
          >
            {enqueue.isPending ? (
              <LoaderCircle
                className="mr-2 inline size-4 animate-spin"
                aria-hidden="true"
              />
            ) : null}
            {t("dataManagementIndexAction")}
          </button>
        </section>
      </div>
      <section
        className="surface-panel mt-6 max-w-5xl p-5"
        aria-labelledby="latest-runs-title"
      >
        <h2 id="latest-runs-title" className="m-0 text-lg font-extrabold">
          {t("dataManagementLatest")}
        </h2>
        <div className="mt-4 grid gap-2">
          {runs.data?.items.map(run => (
            <details key={run.id} className="rounded-md border border-line p-3">
              <summary className="cursor-pointer font-bold">
                {run.status} · {run.operation} ·{" "}
                {run.market_code ?? t("dataManagementAllMarkets")} ·{" "}
                {run.edition_date}
                {run.completed_at
                  ? ` · ${new Intl.DateTimeFormat(locale, { dateStyle: "short", timeStyle: "short" }).format(new Date(run.completed_at))}`
                  : ""}
              </summary>
              <RunDetail result={run.result} error={run.error} />
            </details>
          ))}
        </div>
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
            markets: catalog.data?.markets
              .map(market => t(`reportMarket_${market}`))
              .join(", "),
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
  const markets = Array.isArray(result?.markets) ? result.markets : null
  const symbols = Array.isArray(result?.symbols) ? result.symbols : null
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
  if (symbols) {
    return (
      <ul className="mt-3 list-disc pl-5 text-sm text-sea-ink-soft">
        {symbols.map((symbol, index) => {
          const item = symbol as Record<string, unknown>
          return (
            <li key={`${String(item.symbol)}-${index}`}>
              {`${String(item.symbol)} · ${String(item.status)} · record_count: ${String(item.record_count ?? "—")} · fetched_at: ${String(item.fetched_at ?? "—")} · source_as_of: ${String(item.source_as_of ?? "—")}${item.error ? ` · error: ${String(item.error)}` : ""}`}
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
