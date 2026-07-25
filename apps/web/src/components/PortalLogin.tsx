import { ApiError, type Locale, type User } from "@daily-insights/api-client"
import { useRouter } from "@tanstack/react-router"
import { useState, type FormEvent } from "react"
import { useTranslation } from "react-i18next"
import { browserAuthClient, rememberCsrfToken } from "#/lib/auth"
import { canEnterBackOffice, canEnterCustomer } from "#/lib/authorization"
import { LocaleSwitcher } from "./LocaleSwitcher"

type Portal = "customer" | "admin"

export function PortalLogin({
  locale,
  portal,
}: {
  locale: Locale
  portal: Portal
}) {
  const { t } = useTranslation()
  const router = useRouter()
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  function canEnter(user: User) {
    return portal === "customer"
      ? canEnterCustomer(user)
      : canEnterBackOffice(user)
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setPending(true)
    setError(null)
    const data = new FormData(event.currentTarget)
    try {
      const client = browserAuthClient()
      const result = await client.login({
        email: String(data.get("email") ?? ""),
        password: String(data.get("password") ?? ""),
      })
      if (!canEnter(result.user)) {
        await client.logout(result.csrf_token)
        rememberCsrfToken(null)
        await router.invalidate()
        setError(t("portalRoleMismatch"))
        return
      }
      rememberCsrfToken(result.csrf_token)
      await router.invalidate()
      await router.navigate({
        to: result.user.must_change_password
          ? portal === "customer"
            ? "/$locale/change-password"
            : "/$locale/admin/change-password"
          : portal === "customer"
            ? "/$locale/podcasts"
            : "/$locale/admin/audio",
        params: { locale },
      })
    } catch (cause) {
      setError(
        cause instanceof ApiError && cause.status === 401
          ? t("invalidCredentials")
          : t("unexpectedError")
      )
    } finally {
      setPending(false)
    }
  }

  return (
    <main className="auth-page" data-portal={portal}>
      <section className="auth-intro">
        <p className="eyebrow">
          {t(portal === "customer" ? "customerPortal" : "adminPortal")}
        </p>
        <h1>
          {t(portal === "customer" ? "customerLoginTitle" : "adminLoginTitle")}
        </h1>
        <p>
          {t(
            portal === "customer"
              ? "customerLoginDescription"
              : "adminLoginDescription"
          )}
        </p>
      </section>
      <form className="auth-card" onSubmit={event => void submit(event)}>
        <LocaleSwitcher
          locale={locale}
          destination={portal === "customer" ? "customer-login" : "admin-login"}
        />
        <h2>{t("signIn")}</h2>
        <label>
          {t("email")}
          <input name="email" type="email" autoComplete="username" required />
        </label>
        <label>
          {t("password")}
          <input
            name="password"
            type="password"
            autoComplete="current-password"
            required
          />
        </label>
        {error ? <p role="alert">{error}</p> : null}
        <button type="submit" disabled={pending}>
          {pending ? t("submitting") : t("signIn")}
        </button>
      </form>
    </main>
  )
}
