import { createFileRoute } from "@tanstack/react-router"
import { useTranslation } from "react-i18next"

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/account"
)({
  component: AccountPage,
})

function AccountPage() {
  const { user } = Route.useRouteContext()
  const { t } = useTranslation()

  return (
    <main className="account-page">
      <header className="account-hero">
        <p className="eyebrow">{t("accountEyebrow")}</p>
        <h1>{t("accountTitle")}</h1>
        <p>{t("accountDescription")}</p>
      </header>
      <section className="account-profile" aria-labelledby="profile-title">
        <div className="account-avatar" aria-hidden="true">
          {user.display_name.slice(0, 1).toUpperCase()}
        </div>
        <div>
          <h2 id="profile-title">{user.display_name}</h2>
          <p>{user.email}</p>
        </div>
      </section>
    </main>
  )
}
