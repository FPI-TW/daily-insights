import { createFileRoute } from "@tanstack/react-router"
import { useTranslation } from "react-i18next"

export const Route = createFileRoute("/$locale/_authenticated/back-office/")({
  component: BackOfficeHome,
})

function BackOfficeHome() {
  const { user } = Route.useRouteContext()
  const { t } = useTranslation()
  return (
    <main className="shell-card">
      <p className="eyebrow">{t("backOffice")}</p>
      <h1>{t("welcome", { name: user.display_name })}</h1>
      <dl>
        <dt>{t("role")}</dt>
        <dd>{user.system_role}</dd>
      </dl>
    </main>
  )
}
