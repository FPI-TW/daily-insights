import { createFileRoute, useRouter } from "@tanstack/react-router"
import { Check } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { useEffect, useRef, useState, type ReactNode } from "react"
import { useTranslation } from "react-i18next"
import { ChangePasswordDialog } from "#/components/ChangePasswordDialog"
import { LocaleSwitcher } from "#/components/LocaleSwitcher"
import { ThemeModePicker } from "#/components/ThemeToggle"
import {
  browserAuthClient,
  rememberCsrfToken,
  requireCsrfToken,
} from "#/lib/auth"
import { fadeIn, reveal, toast, useEnterAnimation } from "#/lib/motion"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"

export const Route = createFileRoute("/{-$locale}/_customer/account")({
  component: AccountPage,
})

const savedIndicatorMs = 1500
const toastMs = 3000

function SettingRow({
  title,
  hint,
  children,
  first = false,
  danger = false,
}: {
  title: string
  hint: string
  children: ReactNode
  first?: boolean
  danger?: boolean
}) {
  return (
    <div
      className={`flex items-center justify-between gap-5 px-[22px] py-4 max-sm:flex-col max-sm:items-start ${first ? "" : "border-t border-line"} ${danger ? "bg-chip" : ""}`}
    >
      <div className="max-w-[420px]">
        <p className="m-0 text-sm font-bold text-sea-ink">{title}</p>
        <p className="mt-[3px] mb-0 text-xs leading-[1.6] text-sea-ink-soft">
          {hint}
        </p>
      </div>
      {children}
    </div>
  )
}

function SectionLabel({
  children,
  divided = false,
}: {
  children: ReactNode
  divided?: boolean
}) {
  return (
    <div
      className={`px-[22px] pb-1.5 ${divided ? "border-t border-line pt-[18px]" : "pt-4"}`}
    >
      <p className="m-0 text-[11px] font-extrabold tracking-[0.12em] text-kicker uppercase">
        {children}
      </p>
    </div>
  )
}

function AccountPage() {
  const { locale, user } = Route.useRouteContext()
  const { t } = useTranslation()
  const router = useRouter()
  const animate = useEnterAnimation()
  const redirectExpiredSession = useSessionExpiryRedirect(locale, "customer")
  const [passwordDialogOpen, setPasswordDialogOpen] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [languageSaved, setLanguageSaved] = useState(false)
  const [signingOut, setSigningOut] = useState(false)
  const [signOutError, setSignOutError] = useState<string | null>(null)
  const changePasswordButtonRef = useRef<HTMLButtonElement>(null)
  const previousLocale = useRef(locale)

  // The page stays mounted across a locale switch, so a change in the
  // route param is the moment the new preference has been applied.
  useEffect(() => {
    if (previousLocale.current === locale) return
    previousLocale.current = locale
    setLanguageSaved(true)
    const timer = window.setTimeout(
      () => setLanguageSaved(false),
      savedIndicatorMs
    )
    return () => window.clearTimeout(timer)
  }, [locale])

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(null), toastMs)
    return () => window.clearTimeout(timer)
  }, [notice])

  function closePasswordDialog() {
    setPasswordDialogOpen(false)
    window.requestAnimationFrame(() => changePasswordButtonRef.current?.focus())
  }

  async function signOut() {
    setSigningOut(true)
    setSignOutError(null)
    try {
      await browserAuthClient().logout(await requireCsrfToken())
      rememberCsrfToken(null)
      await router.invalidate()
      await router.navigate({ to: "/{-$locale}/login", params: { locale } })
    } catch (caught) {
      if (await redirectExpiredSession(caught)) return
      setSignOutError(t("unexpectedError"))
    } finally {
      setSigningOut(false)
    }
  }

  return (
    <main className="page-shell">
      <header className="mb-6 max-w-[620px]">
        <p className="eyebrow">{t("accountEyebrow")}</p>
        <h1 className="mt-2 mb-3 text-[clamp(1.6rem,3.4vw,2rem)] leading-none font-extrabold tracking-[-0.045em]">
          {t("account")}
        </h1>
        <span className="block h-[3px] w-14 bg-lagoon" aria-hidden="true" />
      </header>

      <motion.section
        className="surface-panel overflow-hidden"
        aria-labelledby="profile-title"
        {...reveal(animate)}
      >
        <div className="flex items-center gap-[18px] border-b border-line p-6">
          <span
            className="grid size-[60px] shrink-0 place-items-center rounded-[10px] bg-lagoon text-2xl font-extrabold text-white shadow-[0_8px_18px_rgb(21_158_132/25%)]"
            aria-hidden="true"
          >
            {user.display_name.slice(0, 1).toUpperCase()}
          </span>
          <div className="min-w-0">
            <div className="flex items-center gap-2.5">
              <h2
                className="m-0 truncate text-xl font-extrabold tracking-[-0.02em] text-sea-ink"
                id="profile-title"
              >
                {user.display_name}
              </h2>
              <span className="inline-flex shrink-0 items-center rounded-full bg-secondary px-2.5 py-1 text-[11px] font-extrabold tracking-[0.02em] text-palm">
                {t(`role_${user.system_role}`)}
              </span>
            </div>
            <p className="mt-1.5 mb-0 truncate font-mono text-[13.5px] text-sea-ink-soft">
              {user.email}
            </p>
          </div>
        </div>

        <SectionLabel>{t("accountPreferencesEyebrow")}</SectionLabel>
        <SettingRow title={t("language")} hint={t("accountLanguageHint")} first>
          <div className="flex items-center gap-2.5">
            <AnimatePresence>
              {languageSaved ? (
                <motion.span
                  className="inline-flex items-center gap-[5px] text-[11px] font-bold text-lagoon-deep"
                  role="status"
                  variants={fadeIn}
                  initial="hidden"
                  animate="visible"
                  exit="hidden"
                >
                  <Check
                    className="size-3"
                    strokeWidth={3}
                    aria-hidden="true"
                  />
                  {t("saved")}
                </motion.span>
              ) : null}
            </AnimatePresence>
            <LocaleSwitcher
              locale={locale}
              destination="customer-account"
              variant="row"
            />
          </div>
        </SettingRow>
        <SettingRow title={t("theme")} hint={t("accountThemeHint")}>
          <ThemeModePicker variant="row" />
        </SettingRow>

        <SectionLabel divided>{t("accountSecurityEyebrow")}</SectionLabel>
        <SettingRow
          title={t("password")}
          hint={t("accountSecurityDescription")}
          first
        >
          <button
            className="min-h-[38px] shrink-0 rounded-lg border border-chip-line bg-surface px-4 py-2 text-[12.5px] font-extrabold text-sea-ink hover:bg-link-hover"
            type="button"
            ref={changePasswordButtonRef}
            onClick={() => setPasswordDialogOpen(true)}
            aria-haspopup="dialog"
          >
            {t("changePassword")}
          </button>
        </SettingRow>
        <SettingRow title={t("signOut")} hint={t("signOutHint")} danger>
          <button
            className="min-h-9 shrink-0 rounded-lg border border-market-up/40 bg-surface px-[18px] py-2 text-[12.5px] font-extrabold text-market-up hover:bg-market-up/8"
            type="button"
            disabled={signingOut}
            onClick={() => void signOut()}
          >
            {signingOut ? t("submitting") : t("signOut")}
          </button>
        </SettingRow>
      </motion.section>

      <ChangePasswordDialog
        open={passwordDialogOpen}
        locale={locale}
        onClose={closePasswordDialog}
        onSuccess={() => {
          closePasswordDialog()
          setNotice(t("accountPasswordSuccess"))
        }}
      />

      <AnimatePresence>
        {notice ? (
          <motion.p
            className="fixed right-4 bottom-4 z-30 rounded-lg border border-lagoon/40 bg-surface px-3 py-2 text-xs font-bold text-lagoon-deep shadow-lg"
            role="status"
            variants={toast}
            initial="hidden"
            animate="visible"
            exit="hidden"
          >
            {notice}
          </motion.p>
        ) : null}
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
    </main>
  )
}
