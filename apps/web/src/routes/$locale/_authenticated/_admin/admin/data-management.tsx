import { createFileRoute, redirect } from "@tanstack/react-router"
import { DataManagementPage } from "#/components/DataManagementPage"

export const Route = createFileRoute(
  "/$locale/_authenticated/_admin/admin/data-management"
)({
  beforeLoad: ({ context }) => {
    if (context.user.system_role !== "admin") {
      throw redirect({
        to: "/$locale/admin/audio",
        params: { locale: context.locale },
      })
    }
  },
  component: () => (
    <DataManagementPage locale={Route.useRouteContext().locale} />
  ),
})
