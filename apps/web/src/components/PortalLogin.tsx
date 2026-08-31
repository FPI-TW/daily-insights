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
            ? "/$locale/reports"
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
    <main
      className={
        portal === "customer"
          ? "mx-auto grid min-h-svh w-[min(calc(100%-3rem),1240px)] grid-cols-[minmax(0,1fr)_minmax(19rem,28rem)] items-center gap-[clamp(2rem,8vw,9rem)] py-8 max-[42rem]:w-[min(calc(100%-2rem),32rem)] max-[42rem]:grid-cols-1 max-[42rem]:gap-6"
          : "mx-auto grid min-h-svh w-[min(calc(100%-3rem),1240px)] grid-cols-[minmax(0,0.8fr)_minmax(19rem,26rem)] items-center gap-[clamp(2rem,8vw,9rem)] py-8 max-[42rem]:w-[min(calc(100%-2rem),32rem)] max-[42rem]:grid-cols-1 max-[42rem]:gap-6"
      }
      data-portal={portal}
    >
      <section className="max-w-2xl border-l-[3px] border-l-lagoon pl-5 max-[42rem]:pl-4">
        <p className="eyebrow">
          {t(portal === "customer" ? "brand" : "adminPortal")}
        </p>
        <h1
          className={
            portal === "customer"
              ? "my-3 font-display text-[clamp(2.4rem,5vw,4.25rem)] leading-[0.98] tracking-[-0.05em] max-[42rem]:text-[clamp(2.1rem,12vw,3.2rem)]"
              : "my-3 text-[clamp(2rem,4vw,3rem)] leading-none font-extrabold tracking-[-0.045em] max-[42rem]:text-[clamp(1.9rem,10vw,2.7rem)]"
          }
        >
          {t(portal === "customer" ? "customerLoginTitle" : "adminLoginTitle")}
        </h1>
        <p className="max-w-xl text-lg leading-7 text-sea-ink-soft">
          {t(
            portal === "customer"
              ? "customerLoginDescription"
              : "adminLoginDescription"
          )}
        </p>
      </section>
      <div
        className={
          portal === "customer"
            ? "relative isolate w-[min(100%,28rem)]"
            : "w-[min(100%,26rem)]"
        }
      >
        <form
          className={
            portal === "customer"
              ? "surface-panel grid gap-4 overflow-hidden border-t-[3px] border-t-lagoon p-[clamp(1.5rem,4vw,2rem)]"
              : "surface-panel grid gap-4 border-t-[3px] border-t-lagoon p-[clamp(1.5rem,4vw,2rem)]"
          }
          onSubmit={event => void submit(event)}
        >
          <div className="flex items-center justify-between gap-4">
            {portal === "customer" ? (
              <span
                className="flex h-10 items-end gap-1 rounded-[10px] bg-lagoon px-2.5 py-2"
                aria-hidden="true"
              >
                <i className="h-2 w-1 rounded-full bg-white/90" />
                <i className="h-5 w-1 rounded-full bg-white/90" />
                <i className="h-3 w-1 rounded-full bg-white/90" />
                <i className="h-6 w-1 rounded-full bg-white/90" />
              </span>
            ) : null}
            <LocaleSwitcher
              locale={locale}
              destination={
                portal === "customer" ? "customer-login" : "admin-login"
              }
            />
          </div>
          <header>
            <h2 className="mb-1 text-2xl tracking-[-0.035em]">
              {t(portal === "customer" ? "loginWelcome" : "signIn")}
            </h2>
            {portal === "customer" ? (
              <p className="mt-0 text-sm leading-6 text-sea-ink-soft">
                {t("loginPrompt")}
              </p>
            ) : null}
          </header>
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
          {error ? (
            <p
              className="m-0 rounded-lg border border-market-up/35 bg-market-up/10 px-3 py-2 text-sm font-bold text-market-up"
              role="alert"
            >
              {error}
            </p>
          ) : null}
          <button
            className={
              portal === "customer"
                ? "primary-action flex items-center justify-between"
                : "primary-action"
            }
            type="submit"
            disabled={pending}
          >
            <span>{pending ? t("submitting") : t("signIn")}</span>
            {portal === "customer" ? <span aria-hidden="true">→</span> : null}
          </button>
        </form>
      </div>
    </main>
  )
}
