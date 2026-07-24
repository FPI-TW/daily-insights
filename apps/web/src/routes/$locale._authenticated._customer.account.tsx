import { createFileRoute } from "@tanstack/react-router"
import { useTranslation } from "react-i18next"

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/account"
)({
  component: CustomerAccount,
})

function CustomerAccount() {
  const { user } = Route.useRouteContext()
  const { t } = useTranslation()
  return (
    <main className="shell-card">
      <p className="eyebrow">{t("customerArea")}</p>
      <h1>{t("welcome", { name: user.display_name })}</h1>
      <dl>
        <dt>{t("organization")}</dt>
        <dd>{user.organization_id}</dd>
        <dt>{t("role")}</dt>
        <dd>{user.system_role}</dd>
      </dl>
    </main>
  )
}
