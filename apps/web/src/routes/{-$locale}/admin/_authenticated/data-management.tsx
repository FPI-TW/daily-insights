import { createFileRoute, redirect } from "@tanstack/react-router"
import { DataManagementPage } from "#/components/DataManagementPage"

export const Route = createFileRoute(
  "/{-$locale}/admin/_authenticated/data-management"
)({
  beforeLoad: ({ context }) => {
    if (context.user.system_role !== "admin") {
      throw redirect({
        to: "/{-$locale}/admin/audio",
        params: { locale: context.locale },
      })
    }
  },
  component: () => (
    <DataManagementPage locale={Route.useRouteContext().locale} />
  ),
})
