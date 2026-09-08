import { createFileRoute, redirect } from "@tanstack/react-router"
import { NewsManagementPage } from "#/components/NewsManagementPage"

export const Route = createFileRoute(
  "/{-$locale}/admin/_authenticated/news-management"
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
    <NewsManagementPage locale={Route.useRouteContext().locale} />
  ),
})
