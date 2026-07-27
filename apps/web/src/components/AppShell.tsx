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
      <header className="app-header" data-surface={surface}>
        <div className="app-header-main">
          <Link
            to={
              surface === "customer"
                ? "/$locale/podcasts"
                : "/$locale/admin/audio"
            }
            params={{ locale }}
            className="brand"
          >
            <span className="brand-mark" aria-hidden="true" />
            <span>{t("brand")}</span>
            {surface === "admin" ? (
              <span className="brand-surface">{t("adminPortal")}</span>
            ) : null}
          </Link>
          <nav
            className="primary-nav"
            aria-label={t(surface === "customer" ? "customerNav" : "adminNav")}
          >
            {surface === "customer" ? (
              <>
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
        <div className="app-utilities">
          <span className="user-name">{user.display_name}</span>
          <LocaleSwitcher
            locale={locale}
            destination={
              surface === "customer"
                ? location.pathname.endsWith("/account")
                  ? "customer-account"
                  : "customer-podcasts"
                : location.pathname.endsWith("/admin/members")
                  ? "admin-members"
                  : "admin-audio"
            }
          />
          <ThemeToggle />
          <button
            className="utility-button"
            type="button"
            disabled={pending}
            onClick={() => void signOut()}
          >
            {pending ? t("submitting") : t("signOut")}
          </button>
          {signOutError ? (
            <span className="header-error" role="alert">
              {signOutError}
            </span>
          ) : null}
        </div>
      </header>
      {children}
    </>
  )
}
