import { ApiError, type Locale } from "@daily-insights/api-client"
import { useRouter } from "@tanstack/react-router"
import { AnimatePresence, motion } from "motion/react"
import { useState, type FormEvent } from "react"
import { useTranslation } from "react-i18next"
import { rememberCsrfToken } from "#/lib/auth"
import { changePassword } from "#/lib/change-password"
import { toast } from "#/lib/motion"
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
      await changePassword(
        String(data.get("currentPassword") ?? ""),
        String(data.get("newPassword") ?? "")
      )
      await router.invalidate()
      await router.navigate({
        to: portal === "customer" ? "/$locale/reports" : "/$locale/admin/audio",
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
      className="mx-auto grid min-h-svh w-[min(calc(100%-2rem),1240px)] place-items-center py-8"
      data-portal={portal}
    >
      <form
        className="surface-panel grid w-[min(100%,32rem)] gap-4 border-t-[3px] border-t-lagoon p-[clamp(1.5rem,4vw,2rem)]"
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
        <div>
          <p className="eyebrow">
            {t(portal === "customer" ? "brand" : "adminPortal")}
          </p>
          <h1 className="mt-2 mb-0 text-3xl tracking-[-0.04em]">
            {t("initialPasswordTitle")}
          </h1>
        </div>
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
        <AnimatePresence>
          {error ? (
            <motion.p
              className="m-0 rounded-lg border border-market-up/35 bg-market-up/10 px-3 py-2 text-sm font-bold text-market-up"
              role="alert"
              variants={toast}
              initial="hidden"
              animate="visible"
              exit="hidden"
            >
              {error}
            </motion.p>
          ) : null}
        </AnimatePresence>
        <button className="primary-action" type="submit" disabled={pending}>
          {pending ? t("submitting") : t("changePassword")}
        </button>
      </form>
    </main>
  )
}
