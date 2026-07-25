import { ApiError } from "@daily-insights/api-client"
import { createFileRoute, redirect, useRouter } from "@tanstack/react-router"
import { useState, type FormEvent } from "react"
import { useTranslation } from "react-i18next"
import {
  browserAuthClient,
  rememberCsrfToken,
  requireCsrfToken,
} from "#/lib/auth"
import { LocaleSwitcher } from "#/components/LocaleSwitcher"

export const Route = createFileRoute("/$locale/change-password")({
  beforeLoad: ({ context }) => {
    if (!context.user) {
      throw redirect({
        to: "/$locale/login",
        params: { locale: context.locale },
      })
    }
    if (!context.user.must_change_password) {
      throw redirect({ to: "/$locale", params: { locale: context.locale } })
    }
    return { user: context.user }
  },
  component: ChangePasswordPage,
})

function ChangePasswordPage() {
  const { locale } = Route.useRouteContext()
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
      await router.navigate({ to: "/$locale", params: { locale } })
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) {
        rememberCsrfToken(null)
        await router.navigate({ to: "/$locale/login", params: { locale } })
        return
      }
      setError(cause instanceof Error ? cause.message : t("unexpectedError"))
    } finally {
      setPending(false)
    }
  }

  return (
    <main className="auth-page">
      <form className="auth-card" onSubmit={event => void submit(event)}>
        <LocaleSwitcher locale={locale} />
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
