import { createFileRoute } from "@tanstack/react-router"
import { useTranslation } from "react-i18next"
import { ForbiddenScreen } from "#/components/StateScreen"
import { canEnterAdmin } from "#/lib/authorization"

export const Route = createFileRoute(
  "/$locale/_authenticated/back-office/admin"
)({
  beforeLoad: ({ context }) => ({
    forbidden: !canEnterAdmin(context.user),
  }),
  component: AdminAccess,
})

function AdminAccess() {
  const { forbidden, user } = Route.useRouteContext()
  const { t } = useTranslation()
  if (forbidden) return <ForbiddenScreen />
  return (
    <main className="shell-card">
      <p className="eyebrow">{t("adminAccess")}</p>
      <h1>{user.display_name}</h1>
      <p>{user.email}</p>
    </main>
  )
}
