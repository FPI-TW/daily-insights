import { createFileRoute, redirect } from "@tanstack/react-router"
import { IndexDataManagementPage } from "#/components/IndexDataManagementPage"

export const Route = createFileRoute(
  "/$locale/_authenticated/_admin/admin/index-data"
)({
  beforeLoad: ({ context }) => {
    if (context.user.system_role !== "admin") {
      throw redirect({
        to: "/$locale/admin/audio",
        params: { locale: context.locale },
      })
    }
  },
  component: AdminIndexDataPage,
})

function AdminIndexDataPage() {
  const { locale } = Route.useRouteContext()
  return <IndexDataManagementPage locale={locale} />
}
