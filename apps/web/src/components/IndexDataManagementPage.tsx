import {
  ApiError,
  type Locale,
  type YfinanceDailyBarsResponse,
} from "@daily-insights/api-client"
import { LoaderCircle } from "lucide-react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { browserAdministrationClient } from "#/lib/admin-members"
import { getAuthSessionEpoch, requireCsrfToken } from "#/lib/auth"
import { formatIsoDate } from "#/lib/format"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"

const COOLDOWN_MS = 1_000
const SESSION_EXPIRED = Symbol("session-expired")

type RefreshState = {
  pending: boolean
  coolingDown: boolean
  result: YfinanceDailyBarsResponse | null
  error: unknown
}

let refreshState: RefreshState = {
  pending: false,
  coolingDown: false,
  result: null,
  error: null,
}
let activeRefresh: Promise<void> | null = null
let coordinatorEpoch = getAuthSessionEpoch()
let cooldownTimer: ReturnType<typeof setTimeout> | null = null
const refreshSubscribers = new Set<(state: RefreshState) => void>()

function resetCoordinator(epoch = getAuthSessionEpoch()) {
  if (cooldownTimer !== null) clearTimeout(cooldownTimer)
  cooldownTimer = null
  coordinatorEpoch = epoch
  activeRefresh = null
  refreshState = {
    pending: false,
    coolingDown: false,
    result: null,
    error: null,
  }
  notifyRefreshSubscribers()
}

function syncCoordinatorEpoch() {
  const epoch = getAuthSessionEpoch()
  if (epoch !== coordinatorEpoch) resetCoordinator(epoch)
}

function isCurrentRefreshEpoch(epoch: number) {
  return epoch === coordinatorEpoch && epoch === getAuthSessionEpoch()
}

function clearSettledRefreshWhenUnobserved() {
  if (refreshSubscribers.size === 0 && refreshState.coolingDown) {
    resetCoordinator(coordinatorEpoch)
    return
  }
  if (
    refreshSubscribers.size === 0 &&
    !refreshState.pending &&
    !refreshState.coolingDown
  ) {
    refreshState = {
      pending: false,
      coolingDown: false,
      result: null,
      error: null,
    }
  }
}

function notifyRefreshSubscribers() {
  for (const subscriber of refreshSubscribers) subscriber(refreshState)
}

function runSharedRefresh(
  task: (
    epoch: number
  ) => Promise<YfinanceDailyBarsResponse | typeof SESSION_EXPIRED>
) {
  syncCoordinatorEpoch()
  if (activeRefresh || refreshState.coolingDown) return
  const epoch = coordinatorEpoch
  refreshState = {
    pending: true,
    coolingDown: false,
    result: null,
    error: null,
  }
  notifyRefreshSubscribers()
  activeRefresh = task(epoch)
    .then(result => {
      if (!isCurrentRefreshEpoch(epoch)) return true
      if (result === SESSION_EXPIRED) {
        refreshState = {
          pending: false,
          coolingDown: false,
          result: null,
          error: null,
        }
        return true
      }
      refreshState = { ...refreshState, pending: false, result }
      return false
    })
    .catch(error => {
      if (!isCurrentRefreshEpoch(epoch)) return true
      refreshState = { ...refreshState, pending: false, error }
      return false
    })
    .then(sessionExpired => {
      if (!isCurrentRefreshEpoch(epoch)) return
      activeRefresh = null
      if (sessionExpired) {
        notifyRefreshSubscribers()
        clearSettledRefreshWhenUnobserved()
        return
      }
      refreshState = { ...refreshState, coolingDown: true }
      notifyRefreshSubscribers()
      if (refreshSubscribers.size === 0) {
        clearSettledRefreshWhenUnobserved()
        return
      }
      cooldownTimer = setTimeout(() => {
        if (!isCurrentRefreshEpoch(epoch)) return
        refreshState = { ...refreshState, coolingDown: false }
        cooldownTimer = null
        notifyRefreshSubscribers()
        clearSettledRefreshWhenUnobserved()
      }, COOLDOWN_MS)
    })
}

export function IndexDataManagementPage({ locale }: { locale: Locale }) {
  const { t } = useTranslation()
  const redirectExpiredSession = useSessionExpiryRedirect(locale, "admin")
  const [state, setState] = useState(refreshState)

  useEffect(() => {
    syncCoordinatorEpoch()
    refreshSubscribers.add(setState)
    setState(refreshState)
    return () => {
      refreshSubscribers.delete(setState)
      clearSettledRefreshWhenUnobserved()
    }
  }, [])

  async function refresh() {
    runSharedRefresh(async epoch => {
      try {
        return await browserAdministrationClient().refreshIndexDailyBars(
          await requireCsrfToken()
        )
      } catch (caught) {
        if (
          isCurrentRefreshEpoch(epoch) &&
          (await redirectExpiredSession(caught))
        ) {
          resetCoordinator(getAuthSessionEpoch())
          return SESSION_EXPIRED
        }
        throw caught
      }
    })
  }

  const error =
    state.error instanceof ApiError && state.error.status === 503
      ? t("indexDataDisabledError")
      : state.error instanceof ApiError && state.error.status === 504
        ? t("indexDataTimeoutError")
        : state.error
          ? t("indexDataRefreshError")
          : ""

  return (
    <main className="page-shell">
      <header className="mb-8 max-w-3xl">
        <p className="eyebrow">{t("adminPortal")}</p>
        <h1 className="mt-2 mb-3 text-[clamp(1.9rem,4vw,2.5rem)] leading-none font-extrabold tracking-[-0.045em]">
          {t("indexDataAdminTitle")}
        </h1>
        <span
          className="mb-4 block h-[3px] w-14 bg-lagoon"
          aria-hidden="true"
        />
        <p className="leading-7 text-sea-ink-soft">
          {t("indexDataAdminDescription")}
        </p>
      </header>

      <section
        className="surface-panel max-w-5xl p-5"
        aria-labelledby="index-refresh-title"
      >
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="max-w-2xl">
            <h2
              id="index-refresh-title"
              className="mt-0 mb-2 text-lg font-extrabold"
            >
              {t("indexDataRefreshTitle")}
            </h2>
            <p className="m-0 text-sm leading-6 text-sea-ink-soft">
              {t("indexDataRefreshDescription")}
            </p>
          </div>
          <button
            type="button"
            className="primary-action inline-flex items-center gap-2"
            disabled={state.pending || state.coolingDown}
            aria-busy={state.pending}
            onClick={() => void refresh()}
          >
            {state.pending ? (
              <LoaderCircle
                className="size-4 animate-spin"
                aria-hidden="true"
              />
            ) : null}
            {state.pending
              ? t("indexDataRefreshing")
              : t("indexDataRefreshNow")}
          </button>
        </div>

        {error ? (
          <p
            className="mt-4 mb-0 text-sm font-bold text-market-up"
            role="alert"
          >
            {error}
          </p>
        ) : null}
        {state.result ? (
          <div className="mt-5 border-t border-line pt-5" aria-live="polite">
            <div className="flex flex-wrap gap-x-6 gap-y-2 text-sm text-sea-ink-soft">
              <p className="m-0">
                {t("indexDataFetchedAt", {
                  timestamp: new Intl.DateTimeFormat(locale, {
                    dateStyle: "medium",
                    timeStyle: "short",
                  }).format(new Date(state.result.fetched_at)),
                })}
              </p>
              <p className="m-0 font-bold text-sea-ink">
                {t("indexDataResultSummary", {
                  succeeded: state.result.succeeded.length,
                  failed: state.result.failed.length,
                })}
              </p>
            </div>
            {state.result.failed.length > 0 ? (
              <p
                className="mt-4 rounded-md border border-market-caution/40 bg-market-caution/10 p-3 text-sm text-sea-ink-soft"
                role="status"
              >
                {t("indexDataPartialWarning")}
              </p>
            ) : null}
            <div className="mt-4 min-w-0 overflow-x-auto border-y border-line">
              <table className="w-full min-w-160 text-sm">
                <thead className="bg-link-hover text-left text-xs text-sea-ink-soft">
                  <tr>
                    <th className="px-3 py-2.5">{t("indexDataSymbol")}</th>
                    <th className="px-3 py-2.5">{t("indexDataMarket")}</th>
                    <th className="px-3 py-2.5">{t("indexDataStatus")}</th>
                    <th className="px-3 py-2.5 text-right">
                      {t("indexDataStoredCount")}
                    </th>
                    <th className="px-3 py-2.5">{t("indexDataAsOf")}</th>
                    <th className="px-3 py-2.5">
                      {t("indexDataDroppedUnsettled")}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {state.result.succeeded.map(item => (
                    <tr className="border-t border-line" key={item.symbol}>
                      <td className="px-3 py-2.5 font-mono font-bold">
                        {item.symbol}
                      </td>
                      <td className="px-3 py-2.5">
                        {t(`reportMarket_${item.market}`)}
                      </td>
                      <td className="px-3 py-2.5 text-market-down">
                        {t("indexDataSuccess")}
                      </td>
                      <td className="px-3 py-2.5 text-right font-mono tabular-nums">
                        {item.stored_count}
                      </td>
                      <td className="px-3 py-2.5">
                        {formatIsoDate(item.as_of, locale)}
                      </td>
                      <td className="px-3 py-2.5">
                        {item.dropped_unsettled_trade_date
                          ? formatIsoDate(
                              item.dropped_unsettled_trade_date,
                              locale
                            )
                          : "—"}
                      </td>
                    </tr>
                  ))}
                  {state.result.failed.map(item => (
                    <tr className="border-t border-line" key={item.symbol}>
                      <td className="px-3 py-2.5 font-mono font-bold">
                        {item.symbol}
                      </td>
                      <td className="px-3 py-2.5">
                        {t(`reportMarket_${item.market}`)}
                      </td>
                      <td
                        className="px-3 py-2.5 font-bold text-market-caution"
                        title={item.error}
                      >
                        {t("indexDataFailed")}
                      </td>
                      <td className="px-3 py-2.5 text-right">—</td>
                      <td className="px-3 py-2.5">—</td>
                      <td className="px-3 py-2.5">—</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}
      </section>
    </main>
  )
}
