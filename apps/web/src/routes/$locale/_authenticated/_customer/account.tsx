import { createFileRoute } from "@tanstack/react-router"
import { useState, type FormEvent } from "react"
import { useTranslation } from "react-i18next"
import {
  browserAuthClient,
  rememberCsrfToken,
  requireCsrfToken,
} from "#/lib/auth"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/account"
)({
  component: AccountPage,
})

function AccountPage() {
  const { locale, user } = Route.useRouteContext()
  const { t } = useTranslation()
  const redirectExpiredSession = useSessionExpiryRedirect(locale, "customer")
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  const [success, setSuccess] = useState(false)

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setPending(true)
    setError("")
    setSuccess(false)
    const form = event.currentTarget
    const data = new FormData(form)

    try {
      const result = await browserAuthClient().changePassword(
        {
          current_password: String(data.get("currentPassword") ?? ""),
          new_password: String(data.get("newPassword") ?? ""),
        },
        await requireCsrfToken()
      )
      rememberCsrfToken(result.csrf_token)
      form.reset()
      setSuccess(true)
    } catch (caught) {
      if (await redirectExpiredSession(caught)) return
      setError(t("accountPasswordError"))
    } finally {
      setPending(false)
    }
  }

  return (
    <main className="account-page">
      <header className="account-hero">
        <p className="eyebrow">{t("accountEyebrow")}</p>
        <h1>{t("accountTitle")}</h1>
        <p>{t("accountDescription")}</p>
      </header>
      <div className="account-layout">
        <section className="account-profile" aria-labelledby="profile-title">
          <div className="account-avatar" aria-hidden="true">
            {user.display_name.slice(0, 1).toUpperCase()}
          </div>
          <div>
            <h2 id="profile-title">{user.display_name}</h2>
            <p>{user.email}</p>
          </div>
        </section>
        <form
          className="account-security-card"
          onSubmit={event => void submit(event)}
        >
          <header>
            <p className="eyebrow">{t("accountSecurityEyebrow")}</p>
            <h2>{t("accountSecurityTitle")}</h2>
            <p>{t("accountSecurityDescription")}</p>
          </header>
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
              aria-describedby="new-password-hint"
              required
            />
          </label>
          <p id="new-password-hint" className="field-hint">
            {t("passwordMinimum")}
          </p>
          {error ? (
            <p className="form-message form-message--error" role="alert">
              {error}
            </p>
          ) : null}
          {success ? (
            <p className="form-message form-message--success" role="status">
              {t("accountPasswordSuccess")}
            </p>
          ) : null}
          <button className="primary-button" type="submit" disabled={pending}>
            {pending ? t("submitting") : t("changePassword")}
          </button>
        </form>
      </div>
    </main>
  )
}
