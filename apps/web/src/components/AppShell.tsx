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
import { marketCodes } from "#/lib/provisional-reports"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"
import { LocaleSwitcher } from "./LocaleSwitcher"
import ThemeToggle from "./ThemeToggle"

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
  const displayDate = new Intl.DateTimeFormat(locale, {
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(new Date())

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

  const localeDestination =
    surface === "customer"
      ? location.pathname.includes("/reports")
        ? "customer-reports"
        : location.pathname.endsWith("/account")
          ? "customer-account"
          : "customer-podcasts"
      : location.pathname.endsWith("/admin/members")
        ? "admin-members"
        : "admin-audio"

  return (
    <>
      <header
        className="sticky top-0 z-20 border-b border-line bg-header backdrop-blur-xl"
        data-surface={surface}
      >
        <div className="mx-auto flex min-h-[68px] w-full max-w-[1240px] items-center justify-between gap-4 px-6 py-3 max-sm:px-4 max-sm:py-2.5">
          <Link
            to={
              surface === "customer"
                ? "/$locale/reports"
                : "/$locale/admin/audio"
            }
            params={{ locale }}
            className="flex min-w-0 items-center gap-3 text-sea-ink no-underline"
          >
            <span
              className="grid h-10 w-10 shrink-0 place-items-center rounded-[10px] bg-lagoon text-lg font-black text-white shadow-[0_6px_16px_rgb(21_158_132/25%)]"
              aria-hidden="true"
            >
              DI
            </span>
            <span className="min-w-0 max-sm:hidden">
              <span className="block truncate text-[15px] font-extrabold tracking-[-0.02em]">
                {t("brand")}
              </span>
              <span className="block truncate text-[11px] font-semibold tracking-[0.08em] text-sea-ink-soft uppercase">
                {surface === "admin" ? t("adminPortal") : t("reportsEyebrow")}
              </span>
            </span>
          </Link>
          <div className="flex min-w-0 items-center justify-end gap-2">
            <span className="hidden border-r border-line pr-3 text-right text-[11px] font-semibold leading-4 text-sea-ink-soft lg:block">
              <span className="block text-sea-ink">{displayDate}</span>
              {user.display_name}
            </span>
            <LocaleSwitcher
              locale={locale}
              destination={localeDestination}
              reportMarketCode={reportMarketCode}
            />
            <ThemeToggle />
            <button
              className="min-h-9 shrink-0 px-3 py-1.5 text-xs font-extrabold"
              type="button"
              disabled={pending}
              onClick={() => void signOut()}
              aria-label={pending ? t("submitting") : t("signOut")}
            >
              <span className="max-sm:sr-only">
                {pending ? t("submitting") : t("signOut")}
              </span>
              <span
                className="hidden text-base leading-none max-sm:inline"
                aria-hidden="true"
              >
                ↗
              </span>
            </button>
          </div>
        </div>
        <div className="border-t border-line">
          <nav
            className="mx-auto flex w-full max-w-[1240px] overflow-x-auto px-6 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden max-sm:px-4"
            aria-label={t(surface === "customer" ? "customerNav" : "adminNav")}
          >
            {surface === "customer" ? (
              <>
                <Link
                  to="/$locale/reports"
                  params={{ locale }}
                  className="shrink-0 border-b-2 border-transparent px-4 py-3 text-sm font-bold text-sea-ink-soft no-underline transition-colors hover:text-sea-ink [&[aria-current=page]]:border-lagoon [&[aria-current=page]]:text-lagoon"
                >
                  {t("reportsNav")}
                </Link>
                <Link
                  to="/$locale/podcasts"
                  params={{ locale }}
                  className="shrink-0 border-b-2 border-transparent px-4 py-3 text-sm font-bold text-sea-ink-soft no-underline transition-colors hover:text-sea-ink [&[aria-current=page]]:border-lagoon [&[aria-current=page]]:text-lagoon"
                >
                  {t("podcastNav")}
                </Link>
                <Link
                  to="/$locale/account"
                  params={{ locale }}
                  className="shrink-0 border-b-2 border-transparent px-4 py-3 text-sm font-bold text-sea-ink-soft no-underline transition-colors hover:text-sea-ink [&[aria-current=page]]:border-lagoon [&[aria-current=page]]:text-lagoon"
                >
                  {t("accountNav")}
                </Link>
              </>
            ) : (
              <>
                <Link
                  to="/$locale/admin/audio"
                  params={{ locale }}
                  className="shrink-0 border-b-2 border-transparent px-4 py-3 text-sm font-bold text-sea-ink-soft no-underline transition-colors hover:text-sea-ink [&[aria-current=page]]:border-lagoon [&[aria-current=page]]:text-lagoon"
                >
                  {t("audioManagementNav")}
                </Link>
                {user.system_role === "admin" ? (
                  <Link
                    to="/$locale/admin/members"
                    params={{ locale }}
                    className="shrink-0 border-b-2 border-transparent px-4 py-3 text-sm font-bold text-sea-ink-soft no-underline transition-colors hover:text-sea-ink [&[aria-current=page]]:border-lagoon [&[aria-current=page]]:text-lagoon"
                  >
                    {t("memberManagementNav")}
                  </Link>
                ) : null}
              </>
            )}
          </nav>
        </div>
      </header>
      {signOutError ? (
        <p
          className="fixed right-4 bottom-4 z-30 rounded-lg border border-market-up/40 bg-surface px-3 py-2 text-xs font-bold text-market-up shadow-lg"
          role="alert"
        >
          {signOutError}
        </p>
      ) : null}
      {children}
    </>
  )
}
