import type { Locale, User } from "@daily-insights/api-client"
import { Link, useLocation, useRouter } from "@tanstack/react-router"
import type { ReactNode } from "react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import {
  browserAuthClient,
  rememberCsrfToken,
  requireCsrfToken,
} from "#/lib/auth"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"
import { marketCodes } from "#/lib/provisional-reports"
import ThemeToggle from "./ThemeToggle"
import { LocaleSwitcher } from "./LocaleSwitcher"

export function AppShell({
  locale,
  user,
  surface,
  children,
}: {
  locale: Locale
  user: User
  surface: "customer" | "admin"
  children: ReactNode
}) {
  const { t } = useTranslation()
  const router = useRouter()
  const location = useLocation()
  const redirectExpiredSession = useSessionExpiryRedirect(locale, surface)
  const [pending, setPending] = useState(false)
  const [signOutError, setSignOutError] = useState("")
  const reportMarketCode = marketCodes.find(code =>
    location.pathname.endsWith(`/reports/${code}`)
  )

  async function signOut() {
    setPending(true)
    setSignOutError("")
    try {
      await browserAuthClient().logout(await requireCsrfToken())
      rememberCsrfToken(null)
      await router.invalidate()
      await router.navigate({
        to: surface === "customer" ? "/$locale/login" : "/$locale/admin/login",
        params: { locale },
      })
    } catch (caught) {
      if (await redirectExpiredSession(caught)) return
      setSignOutError(t("unexpectedError"))
    } finally {
      setPending(false)
    }
  }

  return (
    <>
      <header
        className="sticky top-0 z-20 flex min-h-16 items-center justify-between gap-4 border-b border-line bg-header px-[clamp(1rem,4vw,3rem)] py-2.5 shadow-[0_10px_35px_rgb(15_23_42/6%)] backdrop-blur-xl max-[52rem]:flex-col max-[52rem]:items-stretch max-[42rem]:px-3"
        data-surface={surface}
      >
        <div className="flex min-w-0 items-center gap-5 max-[42rem]:w-full max-[42rem]:justify-between max-[42rem]:gap-3">
          <Link
            to={
              surface === "customer"
                ? "/$locale/reports"
                : "/$locale/admin/audio"
            }
            params={{ locale }}
            className="flex min-w-fit items-center gap-2.5 text-base font-extrabold tracking-[-0.02em] text-sea-ink no-underline"
          >
            <span
              className="h-6 w-6 rounded-md bg-[linear-gradient(135deg,var(--palm),var(--lagoon))] shadow-[0_5px_14px_color-mix(in_oklab,var(--lagoon-deep)_24%,transparent)]"
              aria-hidden="true"
            />
            <span>{t("brand")}</span>
            {surface === "admin" ? (
              <span className="rounded-full border border-chip-line bg-chip px-2 py-1 text-[0.65rem] font-extrabold tracking-[0.08em] text-sea-ink-soft uppercase">
                {t("adminPortal")}
              </span>
            ) : null}
          </Link>
          <nav
            className={`flex items-center gap-1 rounded-lg bg-link-hover p-1 [&>a]:rounded-md [&>a]:px-3 [&>a]:py-1.5 [&>a]:text-sm [&>a]:font-bold [&>a]:text-sea-ink-soft [&>a]:no-underline [&>a]:transition-colors [&>a:hover]:bg-surface-strong [&>a:hover]:text-sea-ink [&>a[aria-current=page]]:bg-surface-strong [&>a[aria-current=page]]:text-lagoon-deep [&>a[aria-current=page]]:shadow-sm max-[42rem]:flex-1 max-[42rem]:justify-end ${surface === "customer" ? "max-[42rem]:grid max-[42rem]:grid-cols-3 max-[42rem]:gap-0 max-[42rem]:[&>a]:px-2 max-[42rem]:[&>a]:text-center max-[42rem]:[&>a]:text-xs max-[42rem]:[&>a]:whitespace-nowrap" : ""}`}
            aria-label={t(surface === "customer" ? "customerNav" : "adminNav")}
          >
            {surface === "customer" ? (
              <>
                <Link to="/$locale/reports" params={{ locale }}>
                  {t("reportsNav")}
                </Link>
                <Link to="/$locale/podcasts" params={{ locale }}>
                  {t("podcastNav")}
                </Link>
                <Link to="/$locale/account" params={{ locale }}>
                  {t("accountNav")}
                </Link>
              </>
            ) : (
              <>
                <Link to="/$locale/admin/audio" params={{ locale }}>
                  {t("audioManagementNav")}
                </Link>
                {user.system_role === "admin" ? (
                  <Link to="/$locale/admin/members" params={{ locale }}>
                    {t("memberManagementNav")}
                  </Link>
                ) : null}
              </>
            )}
          </nav>
        </div>
        <div className="flex items-center justify-end gap-2 max-[42rem]:flex-nowrap">
          <span className="text-sm font-bold text-sea-ink-soft max-[42rem]:hidden">
            {user.display_name}
          </span>
          <LocaleSwitcher
            locale={locale}
            destination={
              surface === "customer"
                ? location.pathname.includes("/reports")
                  ? "customer-reports"
                  : location.pathname.endsWith("/account")
                    ? "customer-account"
                    : "customer-podcasts"
                : location.pathname.endsWith("/admin/members")
                  ? "admin-members"
                  : "admin-audio"
            }
            reportMarketCode={reportMarketCode}
          />
          <ThemeToggle />
          <button
            className="min-h-9 px-3 py-1.5 text-xs font-extrabold"
            type="button"
            disabled={pending}
            onClick={() => void signOut()}
          >
            {pending ? t("submitting") : t("signOut")}
          </button>
          {signOutError ? (
            <span className="text-xs font-bold text-red-700" role="alert">
              {signOutError}
            </span>
          ) : null}
        </div>
      </header>
      {children}
    </>
  )
}
