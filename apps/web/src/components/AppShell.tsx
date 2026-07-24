import type { Locale, User } from "@daily-insights/api-client"
import { Link, useRouter } from "@tanstack/react-router"
import type { ReactNode } from "react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import {
  browserAuthClient,
  rememberCsrfToken,
  requireCsrfToken,
} from "#/lib/auth"
import ThemeToggle from "./ThemeToggle"
import { LocaleSwitcher } from "./LocaleSwitcher"

export function AppShell({
  locale,
  user,
  children,
}: {
  locale: Locale
  user: User
  children: ReactNode
}) {
  const { t } = useTranslation()
  const router = useRouter()
  const [pending, setPending] = useState(false)

  async function signOut() {
    setPending(true)
    try {
      await browserAuthClient().logout(await requireCsrfToken())
      rememberCsrfToken(null)
      await router.invalidate()
      await router.navigate({ to: "/$locale/login", params: { locale } })
    } finally {
      setPending(false)
    }
  }

  return (
    <>
      <header className="app-header">
        <Link to="/$locale" params={{ locale }} className="brand">
          {t("brand")}
        </Link>
        <nav aria-label={t("account")}>
          {user.system_role === "org_member" && (
            <Link to="/$locale/podcasts" params={{ locale }}>
              {t("podcastNav")}
            </Link>
          )}
          {user.system_role !== "org_member" && (
            <Link to="/$locale/back-office/podcasts" params={{ locale }}>
              {t("podcastNav")}
            </Link>
          )}
          <span>{user.display_name}</span>
          <LocaleSwitcher locale={locale} />
          <ThemeToggle />
          <button
            type="button"
            disabled={pending}
            onClick={() => void signOut()}
          >
            {pending ? t("submitting") : t("signOut")}
          </button>
        </nav>
      </header>
      {children}
    </>
  )
}
