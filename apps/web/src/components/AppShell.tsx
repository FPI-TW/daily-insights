import type { Locale, User } from "@daily-insights/api-client"
import { Link, useLocation, useRouter } from "@tanstack/react-router"
import { Settings, X } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import type { ReactNode } from "react"
import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import {
  browserAuthClient,
  rememberCsrfToken,
  requireCsrfToken,
} from "#/lib/auth"
import { backdrop, dialogPanel, toast } from "#/lib/motion"
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
  const [settingsOpen, setSettingsOpen] = useState(false)
  const settingsButtonRef = useRef<HTMLButtonElement>(null)
  const settingsDialogRef = useRef<HTMLElement>(null)
  const closeSettingsButtonRef = useRef<HTMLButtonElement>(null)
  const reportMarketCode = marketCodes.find(code =>
    location.pathname.endsWith(`/reports/${code}`)
  )
  const displayDate = new Intl.DateTimeFormat(locale, {
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(new Date())

  useEffect(() => {
    if (!settingsOpen) return

    closeSettingsButtonRef.current?.focus()

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        closeSettings()
        return
      }
      if (event.key !== "Tab") return

      const focusableElements =
        settingsDialogRef.current?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'
        )
      if (!focusableElements?.length) return

      const first = focusableElements[0]
      const last = focusableElements[focusableElements.length - 1]
      if (!first || !last) return
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener("keydown", handleKeyDown)
    return () => document.removeEventListener("keydown", handleKeyDown)
  }, [settingsOpen])

  function closeSettings() {
    setSettingsOpen(false)
    window.requestAnimationFrame(() => settingsButtonRef.current?.focus())
  }

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
            <span className="min-w-0">
              <span className="block truncate text-[15px] font-extrabold tracking-[-0.02em]">
                {t("brand")}
              </span>
            </span>
          </Link>
          <div className="flex min-w-0 flex-1 items-center justify-end gap-2">
            {surface === "customer" ? (
              <nav
                className="flex min-w-0 items-center gap-1 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
                aria-label={t("customerNav")}
              >
                <Link
                  to="/$locale/reports"
                  params={{ locale }}
                  className="shrink-0 rounded-md px-3 py-2 text-sm font-bold text-sea-ink-soft no-underline transition-colors hover:bg-link-hover hover:text-sea-ink [&[aria-current=page]]:bg-lagoon/10 [&[aria-current=page]]:text-lagoon"
                >
                  {t("reportsNav")}
                </Link>
                <Link
                  to="/$locale/podcasts"
                  params={{ locale }}
                  className="shrink-0 rounded-md px-3 py-2 text-sm font-bold text-sea-ink-soft no-underline transition-colors hover:bg-link-hover hover:text-sea-ink [&[aria-current=page]]:bg-lagoon/10 [&[aria-current=page]]:text-lagoon"
                >
                  {t("podcastNav")}
                </Link>
                <Link
                  to="/$locale/account"
                  params={{ locale }}
                  className="shrink-0 rounded-md px-3 py-2 text-sm font-bold text-sea-ink-soft no-underline transition-colors hover:bg-link-hover hover:text-sea-ink [&[aria-current=page]]:bg-lagoon/10 [&[aria-current=page]]:text-lagoon"
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
              className="mx-auto flex w-full max-w-[1240px] overflow-x-auto px-6 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden max-sm:px-4"
              aria-label={t("adminNav")}
            >
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
            </nav>
          </div>
        ) : null}
      </header>
      <AnimatePresence>
        {settingsOpen ? (
          <motion.div
            className="fixed inset-0 z-30 grid place-items-center bg-sea-ink/35 p-4"
            role="presentation"
            onMouseDown={closeSettings}
            variants={backdrop}
            initial="hidden"
            animate="visible"
            exit="hidden"
          >
            <motion.section
              className="w-full max-w-sm rounded-[13px] border border-line bg-surface p-5 shadow-xl"
              role="dialog"
              aria-modal="true"
              aria-labelledby="settings-title"
              ref={settingsDialogRef}
              onMouseDown={event => event.stopPropagation()}
              variants={dialogPanel}
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
              <div className="mt-5 space-y-5">
                <div className="space-y-2">
                  <p className="m-0 text-xs font-extrabold tracking-[0.08em] text-sea-ink-soft uppercase">
                    {t("language")}
                  </p>
                  <LocaleSwitcher
                    locale={locale}
                    destination={localeDestination}
                    reportMarketCode={reportMarketCode}
                  />
                </div>
                <div className="flex items-center justify-between gap-4 border-t border-line pt-5">
                  <p className="m-0 text-sm font-bold text-sea-ink">
                    {t("theme")}
                  </p>
                  <ThemeToggle />
                </div>
                <div className="border-t border-line pt-5">
                  <button
                    className="min-h-9 w-full border border-market-up/40 px-3 py-1.5 text-xs font-extrabold text-market-up"
                    type="button"
                    disabled={pending}
                    onClick={() => void signOut()}
                  >
                    {pending ? t("submitting") : t("signOut")}
                  </button>
                </div>
              </div>
            </motion.section>
          </motion.div>
        ) : null}
      </AnimatePresence>
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
