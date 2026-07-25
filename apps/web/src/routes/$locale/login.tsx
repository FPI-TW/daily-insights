import { ApiError } from "@daily-insights/api-client"
import { createFileRoute, redirect, useRouter } from "@tanstack/react-router"
import { useState, type FormEvent } from "react"
import { useTranslation } from "react-i18next"
import { browserAuthClient, rememberCsrfToken } from "#/lib/auth"
import { destinationFor } from "#/lib/authorization"
import { LocaleSwitcher } from "#/components/LocaleSwitcher"

export const Route = createFileRoute("/$locale/login")({
  beforeLoad: ({ context }) => {
    if (context.user) {
      throw redirect({ to: "/$locale", params: { locale: context.locale } })
    }
  },
  component: LoginPage,
})

function LoginPage() {
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
      const result = await browserAuthClient().login({
        email: String(data.get("email") ?? ""),
        password: String(data.get("password") ?? ""),
      })
      rememberCsrfToken(result.csrf_token)
      await router.invalidate()
      const destination = destinationFor(result.user)
      await router.navigate({
        to:
          destination === "change-password"
            ? "/$locale/change-password"
            : destination === "customer"
              ? "/$locale/account"
              : "/$locale/back-office",
        params: { locale },
      })
    } catch (cause) {
      setError(
        cause instanceof ApiError && cause.status === 401
          ? t("sessionExpired")
          : t("unexpectedError")
      )
    } finally {
      setPending(false)
    }
  }

  return (
    <main className="auth-page">
      <form className="auth-card" onSubmit={event => void submit(event)}>
        <LocaleSwitcher locale={locale} />
        <h1>{t("signIn")}</h1>
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
