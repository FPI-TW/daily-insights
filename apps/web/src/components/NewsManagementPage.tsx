import { ApiError, type Locale } from "@daily-insights/api-client"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { LoaderCircle } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Dialog } from "#/components/Dialog"
import { browserAdministrationClient } from "#/lib/admin-members"
import { requireCsrfToken } from "#/lib/auth"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"

const catalogKey = ["data-management", "catalog"] as const
const runsKey = ["data-management", "runs"] as const

export function NewsManagementPage({ locale }: { locale: Locale }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const redirectExpired = useSessionExpiryRedirect(locale, "admin")
  const [confirmOpen, setConfirmOpen] = useState(false)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const allRef = useRef<HTMLButtonElement>(null)
  const catalog = useQuery({
    queryKey: catalogKey,
    queryFn: () => browserAdministrationClient().dataManagementCatalog(),
  })
  const runs = useQuery({
    queryKey: runsKey,
    queryFn: () => browserAdministrationClient().listNewsDataManagementRuns(),
    refetchInterval: query =>
      query.state.data?.items.some(
        run =>
          run.operation.startsWith("news") &&
          ["pending", "running"].includes(run.status)
      )
        ? 2_000
        : false,
  })
  useEffect(() => {
    if (catalog.error) void redirectExpired(catalog.error)
  }, [catalog.error, redirectExpired])
  useEffect(() => {
    if (runs.error) void redirectExpired(runs.error)
  }, [redirectExpired, runs.error])
  const newsRuns =
    runs.data?.items.filter(run => run.operation.startsWith("news")) ?? []
  const manualActive = newsRuns.some(
    run =>
      run.requested_by_user_id !== null &&
      ["pending", "running"].includes(run.status)
  )
  const enqueue = useMutation({
    mutationFn: async (
      input:
        | { operation: "news_all" }
        | {
            operation: "news_market"
            market_code: "global" | "tw_equity" | "us_equity"
          }
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
  const cancelRun = useMutation({
    mutationFn: async (runId: string) =>
      browserAdministrationClient().cancelDataManagementRun(
        runId,
        await requireCsrfToken()
      ),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: runsKey }),
  })
  const errorMessage =
    enqueue.error instanceof ApiError && enqueue.error.status === 409
      ? t("newsManagementConflict")
      : enqueue.error instanceof ApiError && enqueue.error.status === 503
        ? t("newsManagementUnavailable")
        : enqueue.error
          ? t("newsManagementFailed")
          : ""
  const submit = async (
    input:
      | { operation: "news_all" }
      | {
          operation: "news_market"
          market_code: "global" | "tw_equity" | "us_equity"
        }
  ) => {
    try {
      await enqueue.mutateAsync(input)
    } catch (caught) {
      await redirectExpired(caught)
    }
  }
  const closeConfirmation = () => {
    setConfirmOpen(false)
    window.requestAnimationFrame(() => allRef.current?.focus())
  }

  if (catalog.isPending || runs.isPending) {
    return (
      <main className="page-shell" role="status" aria-live="polite">
        <span className="sr-only">{t("newsManagementLoading")}</span>
        <div className="h-10 w-72 animate-pulse rounded bg-link-hover" />
        <div className="mt-6 h-64 max-w-4xl animate-pulse rounded-xl bg-link-hover" />
      </main>
    )
  }

  if (catalog.error || runs.error) {
    return (
      <main className="page-shell">
        <p role="alert" className="font-bold text-market-up">
          {t("newsManagementLoadFailed")}
        </p>
      </main>
    )
  }

  return (
    <main className="page-shell">
      <header className="mb-8 max-w-3xl">
        <p className="eyebrow">{t("adminPortal")}</p>
        <h1 className="mt-2 text-[clamp(1.9rem,4vw,2.5rem)] leading-none font-extrabold tracking-[-0.045em]">
          {t("newsManagementTitle")}
        </h1>
        <p className="mt-3 leading-7 text-sea-ink-soft">
          {t("newsManagementDescription")}
        </p>
      </header>
      {errorMessage ? (
        <p role="alert" className="mb-4 font-bold text-market-up">
          {errorMessage}
        </p>
      ) : null}
      <div className="grid max-w-4xl gap-5 md:grid-cols-2">
        <section className="surface-panel p-5" aria-labelledby="news-all-title">
          <h2 id="news-all-title" className="m-0 text-lg font-extrabold">
            {t("newsManagementAll")}
          </h2>
          <p className="mt-2 text-sm leading-6 text-sea-ink-soft">
            {t("newsManagementAllDescription")}
          </p>
          <button
            ref={allRef}
            type="button"
            className="primary-action mt-4"
            disabled={
              manualActive ||
              enqueue.isPending ||
              !catalog.data?.daily_news_enabled
            }
            onClick={() => setConfirmOpen(true)}
          >
            {t("newsManagementAllAction")}
          </button>
        </section>
        <section
          className="surface-panel p-5"
          aria-labelledby="news-market-title"
        >
          <h2 id="news-market-title" className="m-0 text-lg font-extrabold">
            {t("newsManagementMarket")}
          </h2>
          <div className="mt-4 grid gap-2">
            {catalog.data?.news_markets.map(market => (
              <button
                key={market}
                type="button"
                className="secondary-action text-left"
                disabled={
                  manualActive ||
                  enqueue.isPending ||
                  !catalog.data?.daily_news_enabled
                }
                onClick={() =>
                  void submit({ operation: "news_market", market_code: market })
                }
              >
                {t(`newsEdition_${market}`)}
              </button>
            ))}
          </div>
        </section>
      </div>
      <section
        className="surface-panel mt-6 max-w-4xl p-5"
        aria-labelledby="news-runs-title"
      >
        <h2 id="news-runs-title" className="m-0 text-lg font-extrabold">
          {t("newsManagementLatest")}
        </h2>
        <div className="mt-4 grid gap-2">
          {newsRuns.map(run => (
            <details key={run.id} className="rounded-md border border-line p-3">
              <summary className="cursor-pointer font-bold">
                {run.status} ·{" "}
                {run.operation === "news_all"
                  ? t("newsManagementAllMarkets")
                  : t(`newsEdition_${run.market_code}`)}{" "}
                · {run.edition_date}
              </summary>
              <p className="mt-3 text-sm text-sea-ink-soft">
                {run.error ?? String(run.result?.outcome ?? "")}
              </p>
              {["pending", "running"].includes(run.status) ? (
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
      </section>
      <Dialog
        open={confirmOpen}
        onClose={closeConfirmation}
        labelledBy="news-confirmation"
        initialFocusRef={cancelRef}
        role="alertdialog"
      >
        <h2 id="news-confirmation" className="m-0 text-lg font-extrabold">
          {t("newsManagementConfirmTitle")}
        </h2>
        <p className="mt-3 text-sm leading-6 text-sea-ink-soft">
          {t("newsManagementConfirmWarning", {
            date: catalog.data?.taipei_date,
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
            onClick={() => void submit({ operation: "news_all" })}
          >
            {enqueue.isPending ? (
              <LoaderCircle
                className="mr-2 inline size-4 animate-spin"
                aria-hidden="true"
              />
            ) : null}
            {t("newsManagementConfirmAction")}
          </button>
        </div>
      </Dialog>
    </main>
  )
}
