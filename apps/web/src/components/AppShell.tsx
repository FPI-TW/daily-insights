import { formatDateStamp } from "#/lib/format"
import type { Locale, User } from "@daily-insights/api-client"
import { Link, useLocation, useRouter } from "@tanstack/react-router"
import { Settings, X } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { Dialog } from "./Dialog"
import type { ReactNode } from "react"
import { useCallback, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import {
  browserAuthClient,
  rememberCsrfToken,
  requireCsrfToken,
} from "#/lib/auth"
import { toast } from "#/lib/motion"
import { marketCodes } from "#/lib/provisional-reports"
import { ActiveIndicator } from "./ActiveIndicator"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"
import { LocaleSwitcher } from "./LocaleSwitcher"
import { ThemeModePicker } from "./ThemeToggle"

const customerNavLinkClass =
  "shrink-0 rounded-md px-3 py-2 text-sm font-bold text-sea-ink-soft no-underline transition-colors hover:text-sea-ink [&:not([aria-current=page])]:hover:bg-link-hover [&[aria-current=page]]:text-lagoon"
const adminNavLinkClass =
  "shrink-0 border-b-2 border-transparent px-4 py-3 text-sm font-bold text-sea-ink-soft no-underline transition-colors hover:text-sea-ink [&[aria-current=page]]:text-lagoon"

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
  const [settingsOpen, setSettingsOpen] = useState(false)
  const settingsButtonRef = useRef<HTMLButtonElement>(null)
  const closeSettingsButtonRef = useRef<HTMLButtonElement>(null)
  const pathname = location.pathname
  const customerSection = pathname.includes("/reports")
    ? "reports"
    : pathname.includes("/podcasts")
      ? "podcasts"
      : pathname.endsWith("/account")
        ? "account"
        : null
  const adminSection = pathname.includes("/admin/audio")
    ? "audio"
    : pathname.includes("/admin/members")
      ? "members"
      : pathname.includes("/admin/index-data")
        ? "index-data"
        : pathname.includes("/admin/analyst-viewpoints")
          ? "analyst-viewpoints"
          : pathname.includes("/admin/conversations")
            ? "conversations"
            : null
  const reportMarketCode = marketCodes.find(code =>
    location.pathname.endsWith(`/reports/${code}`)
  )
  const displayDate = formatDateStamp(new Date())

  const closeSettings = useCallback(() => {
    setSettingsOpen(false)
    window.requestAnimationFrame(() => settingsButtonRef.current?.focus())
  }, [])

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
        : location.pathname.endsWith("/admin/index-data")
          ? "admin-index-data"
          : location.pathname.endsWith("/admin/analyst-viewpoints")
            ? "admin-analyst-viewpoints"
            : "admin-audio"

  return (
    <>
      {/* layoutRoot: the header is sticky, so the active-marker layout
          animation must measure against the header itself, not the page.
          Otherwise a route change that resets the scroll position makes the
          marker appear to travel the scrolled distance. */}
      <motion.header
        className="sticky top-0 z-20 border-b border-line bg-header backdrop-blur-xl [view-transition-name:app-header]"
        data-surface={surface}
        layoutRoot
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
            <span className="min-w-0">
              <span className="block truncate text-[15px] font-extrabold tracking-[-0.02em]">
                {t("brand")}
              </span>
            </span>
          </Link>
          <div className="flex min-w-0 flex-1 items-center justify-end gap-2">
            {surface === "customer" ? (
              <nav
                className="relative isolate flex min-w-0 items-center gap-1 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
                aria-label={t("customerNav")}
              >
                <ActiveIndicator
                  activeKey={`${locale}:${customerSection}`}
                  variant="pill"
                />
                <Link
                  to="/$locale/reports"
                  params={{ locale }}
                  className={customerNavLinkClass}
                >
                  {t("reportsNav")}
                </Link>
                <Link
                  to="/$locale/podcasts"
                  params={{ locale }}
                  className={customerNavLinkClass}
                >
                  {t("podcastNav")}
                </Link>
                <Link
                  to="/$locale/account"
                  params={{ locale }}
                  className={customerNavLinkClass}
                >
                  {t("accountNav")}
                </Link>
              </nav>
            ) : (
              <span className="hidden text-right text-[11px] font-semibold text-sea-ink-soft lg:block">
                {displayDate}
              </span>
            )}
            <button
              className="grid min-h-9 min-w-9 shrink-0 place-items-center rounded-md px-2 text-sea-ink-soft transition-colors hover:bg-link-hover hover:text-sea-ink"
              type="button"
              ref={settingsButtonRef}
              onClick={() => setSettingsOpen(true)}
              aria-label={t("settings")}
              aria-haspopup="dialog"
            >
              <Settings className="size-4" aria-hidden="true" />
            </button>
          </div>
        </div>
        {surface === "admin" ? (
          <div className="border-t border-line">
            <nav
              className="relative isolate mx-auto flex w-full max-w-[1240px] overflow-x-auto px-6 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden max-sm:px-4"
              aria-label={t("adminNav")}
            >
              <ActiveIndicator
                activeKey={`${locale}:${adminSection}`}
                variant="underline"
              />
              <Link
                to="/$locale/admin/audio"
                params={{ locale }}
                className={adminNavLinkClass}
              >
                {t("audioManagementNav")}
              </Link>
              {user.system_role === "admin" ? (
                <>
                  <Link
                    to="/$locale/admin/members"
                    params={{ locale }}
                    className={adminNavLinkClass}
                  >
                    {t("memberManagementNav")}
                  </Link>
                  <Link
                    to="/$locale/admin/index-data"
                    params={{ locale }}
                    className={adminNavLinkClass}
                  >
                    {t("indexDataAdminNav")}
                  </Link>
                  <Link
                    to="/$locale/admin/analyst-viewpoints"
                    params={{ locale }}
                    className="shrink-0 border-b-2 border-transparent px-4 py-3 text-sm font-bold text-sea-ink-soft no-underline transition-colors hover:text-sea-ink [&[aria-current=page]]:border-lagoon [&[aria-current=page]]:text-lagoon"
                  >
                    {t("analystViewpointsAdminNav")}
                  </Link>
                  <Link
                    to="/$locale/admin/conversations"
                    params={{ locale }}
                    search={{ history: [] }}
                    className={adminNavLinkClass}
                  >
                    {t("conversationsNav")}
                  </Link>
                </>
              ) : null}
            </nav>
          </div>
        ) : null}
      </motion.header>
      <Dialog
        open={settingsOpen}
        onClose={closeSettings}
        labelledBy="settings-title"
        initialFocusRef={closeSettingsButtonRef}
      >
        <div className="flex items-center justify-between gap-4">
          <h2
            className="m-0 text-lg font-extrabold tracking-[-0.02em] text-sea-ink"
            id="settings-title"
          >
            {t("settings")}
          </h2>
          <button
            className="grid min-h-9 min-w-9 place-items-center rounded-md text-sea-ink-soft transition-colors hover:bg-link-hover hover:text-sea-ink"
            type="button"
            ref={closeSettingsButtonRef}
            onClick={closeSettings}
            aria-label={t("dismiss")}
          >
            <X className="size-4" aria-hidden="true" />
          </button>
        </div>
        <div className="mt-5 grid gap-[18px]">
          <div className="grid gap-2">
            <p className="eyebrow tracking-[0.08em] text-sea-ink-soft">
              {t("language")}
            </p>
            <LocaleSwitcher
              locale={locale}
              destination={localeDestination}
              reportMarketCode={reportMarketCode}
              variant="dialog"
            />
          </div>
          <div className="grid gap-[9px] border-t border-line pt-[18px]">
            <p className="eyebrow tracking-[0.08em] text-sea-ink-soft">
              {t("theme")}
            </p>
            <ThemeModePicker />
          </div>
          <div className="grid gap-[9px] border-t border-line pt-[18px]">
            <p className="m-0 truncate text-[11px] font-semibold text-sea-ink-soft">
              {t("signedInAs", { email: user.email })}
            </p>
            <button
              className="min-h-[38px] w-full rounded-lg border border-market-up/40 bg-surface px-3.5 py-2 text-[12.5px] font-extrabold text-market-up hover:bg-market-up/8"
              type="button"
              disabled={pending}
              onClick={() => void signOut()}
            >
              {pending ? t("submitting") : t("signOut")}
            </button>
          </div>
        </div>
      </Dialog>
      <AnimatePresence>
        {signOutError ? (
          <motion.p
            className="fixed right-4 bottom-4 z-30 rounded-lg border border-market-up/40 bg-surface px-3 py-2 text-xs font-bold text-market-up shadow-lg"
            role="alert"
            variants={toast}
            initial="hidden"
            animate="visible"
            exit="hidden"
          >
            {signOutError}
          </motion.p>
        ) : null}
      </AnimatePresence>
      {children}
    </>
  )
}
