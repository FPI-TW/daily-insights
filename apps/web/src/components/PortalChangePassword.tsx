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
    <main
      className="mx-auto grid min-h-svh w-[min(calc(100%-2rem),36rem)] place-items-center py-8"
      data-portal={portal}
    >
      <form
        className="surface-panel grid w-full gap-4 p-[clamp(1.5rem,4vw,2.5rem)]"
        onSubmit={event => void submit(event)}
      >
        <LocaleSwitcher
          locale={locale}
          destination={
            portal === "customer"
              ? "customer-change-password"
              : "admin-change-password"
          }
        />
        <h1 className="mb-0 text-3xl tracking-[-0.04em]">
          {t("initialPasswordTitle")}
        </h1>
        <p className="mt-0 leading-7 text-sea-ink-soft">
          {t("initialPasswordDescription")}
        </p>
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
        {error ? (
          <p className="text-sm font-bold text-red-700" role="alert">
            {error}
          </p>
        ) : null}
        <button className="primary-action" type="submit" disabled={pending}>
          {pending ? t("submitting") : t("changePassword")}
        </button>
      </form>
    </main>
  )
}
