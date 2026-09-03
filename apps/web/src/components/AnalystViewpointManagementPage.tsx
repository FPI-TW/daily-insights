import type {
  AnalystViewpointSyncStatus,
  Locale,
} from "@daily-insights/api-client"
import { useRouter } from "@tanstack/react-router"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { browserAdministrationClient } from "#/lib/admin-members"
import { requireCsrfToken } from "#/lib/auth"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"

export function AnalystViewpointManagementPage({
  status,
  locale,
}: {
  status: AnalystViewpointSyncStatus
  locale: Locale
}) {
  const { t } = useTranslation()
  const router = useRouter()
  const redirectExpiredSession = useSessionExpiryRedirect(locale, "admin")
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")

  async function sync() {
    setPending(true)
    setError("")
    try {
      await browserAdministrationClient().syncAnalystViewpoints(
        await requireCsrfToken()
      )
      await router.invalidate({ sync: true })
    } catch (caught) {
      if (await redirectExpiredSession(caught)) return
      setError(t("analystViewpointsSyncError"))
    } finally {
      setPending(false)
    }
  }

  return (
    <main className="page-shell">
      <header className="mb-8 max-w-3xl">
        <p className="eyebrow">{t("adminPortal")}</p>
        <h1 className="mt-2 mb-3 text-[clamp(1.9rem,4vw,2.5rem)] leading-none font-extrabold tracking-[-0.045em]">
          {t("analystViewpointsAdminTitle")}
        </h1>
        <span
          className="mb-4 block h-[3px] w-14 bg-lagoon"
          aria-hidden="true"
        />
        <p className="leading-7 text-sea-ink-soft">
          {t("analystViewpointsAdminDescription")}
        </p>
      </header>

      <section
        className="surface-panel max-w-4xl p-5"
        aria-labelledby="sync-status-title"
      >
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h2
              id="sync-status-title"
              className="mt-0 mb-2 text-lg font-extrabold"
            >
              {t("analystViewpointsSyncStatus")}
            </h2>
            <p className="m-0 text-sm text-sea-ink-soft">
              {status.enabled
                ? t("analystViewpointsEnabled")
                : t("analystViewpointsDisabled")}
            </p>
          </div>
          <button
            type="button"
            className="primary-action"
            disabled={!status.enabled || pending}
            onClick={() => void sync()}
          >
            {pending ? t("submitting") : t("analystViewpointsManualSync")}
          </button>
        </div>
        {error ? (
          <p className="mt-4 mb-0 text-sm font-bold text-red-700" role="alert">
            {error}
          </p>
        ) : null}
        <dl className="mt-5 grid gap-4 border-t border-line pt-5 sm:grid-cols-2">
          <div>
            <dt className="text-xs font-bold text-sea-ink-soft">
              {t("analystViewpointsStoredMarkets")}
            </dt>
            <dd className="mt-1 text-2xl font-extrabold text-sea-ink">
              {status.viewpoints.length}
            </dd>
          </div>
          <div>
            <dt className="text-xs font-bold text-sea-ink-soft">
              {t("analystViewpointsLastSync")}
            </dt>
            <dd className="mt-1 text-sm font-semibold text-sea-ink">
              {status.latest_sync
                ? new Intl.DateTimeFormat(locale, {
                    dateStyle: "medium",
                    timeStyle: "short",
                  }).format(
                    new Date(
                      status.latest_sync.fetched_at ??
                        status.latest_sync.completed_at
                    )
                  )
                : t("analystViewpointsNeverSynced")}
            </dd>
            {status.latest_sync ? (
              <p className="mt-1 mb-0 text-xs text-sea-ink-soft">
                {t("analystViewpointsLatestOutcome", {
                  status: t(
                    `analystViewpointsExecution_${status.latest_sync.status}`
                  ),
                })}
              </p>
            ) : null}
          </div>
        </dl>
        {status.latest_sync ? (
          <ul className="mt-5 grid gap-2 border-t border-line pt-5 text-sm text-sea-ink-soft sm:grid-cols-2">
            {status.latest_sync.markets.map(market => (
              <li key={market.market_code}>
                {t(`reportMarket_${market.market_code}`)}:{" "}
                {t(`analystViewpointsStatus_${market.status}`)}
              </li>
            ))}
          </ul>
        ) : null}
      </section>
    </main>
  )
}
