import { ApiError, type Locale } from "@daily-insights/api-client"
import { useRouter } from "@tanstack/react-router"
import { useState, type FormEvent } from "react"
import { useTranslation } from "react-i18next"
import {
  browserAuthClient,
  rememberCsrfToken,
  requireCsrfToken,
} from "#/lib/auth"
import { LocaleSwitcher } from "./LocaleSwitcher"

export function PortalChangePassword({
  locale,
  portal,
}: {
  locale: Locale
  portal: "customer" | "admin"
}) {
  const { t } = useTranslation()
  const router = useRouter()
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setPending(true)
    setError(null)
    const data = new FormData(event.currentTarget)
    try {
      const result = await browserAuthClient().changePassword(
        {
          current_password: String(data.get("currentPassword") ?? ""),
          new_password: String(data.get("newPassword") ?? ""),
        },
        await requireCsrfToken()
      )
      rememberCsrfToken(result.csrf_token)
      await router.invalidate()
      await router.navigate({
        to:
          portal === "customer" ? "/$locale/podcasts" : "/$locale/admin/audio",
        params: { locale },
      })
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) {
        rememberCsrfToken(null)
        await router.navigate({
          to: portal === "customer" ? "/$locale/login" : "/$locale/admin/login",
          params: { locale },
        })
        return
      }
      setError(cause instanceof Error ? cause.message : t("unexpectedError"))
    } finally {
      setPending(false)
    }
  }

  return (
    <main className="auth-page" data-portal={portal}>
      <form className="auth-card" onSubmit={event => void submit(event)}>
        <LocaleSwitcher
          locale={locale}
          destination={
            portal === "customer"
              ? "customer-change-password"
              : "admin-change-password"
          }
        />
        <h1>{t("initialPasswordTitle")}</h1>
        <p>{t("initialPasswordDescription")}</p>
        <label>
          {t("currentPassword")}
          <input
            name="currentPassword"
            type="password"
            autoComplete="current-password"
            required
          />
        </label>
        <label>
          {t("newPassword")}
          <input
            name="newPassword"
            type="password"
            autoComplete="new-password"
            minLength={8}
            required
          />
        </label>
        {error ? <p role="alert">{error}</p> : null}
        <button type="submit" disabled={pending}>
          {pending ? t("submitting") : t("changePassword")}
        </button>
      </form>
    </main>
  )
}
