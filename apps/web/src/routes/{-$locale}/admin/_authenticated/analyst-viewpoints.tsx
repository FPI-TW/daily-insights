import { createFileRoute, redirect } from "@tanstack/react-router"
import { AnalystViewpointManagementPage } from "#/components/AnalystViewpointManagementPage"
import { ErrorScreen, LoadingScreen } from "#/components/StateScreen"
import { getAnalystViewpointStatus } from "#/lib/admin-analyst-viewpoints"

export const Route = createFileRoute(
  "/{-$locale}/admin/_authenticated/analyst-viewpoints"
)({
  beforeLoad: ({ context }) => {
    if (context.user.system_role !== "admin") {
      throw redirect({
        to: "/{-$locale}/admin/audio",
        params: { locale: context.locale },
      })
    }
  },
  loader: () => getAnalystViewpointStatus(),
  pendingComponent: LoadingScreen,
  errorComponent: ErrorScreen,
  component: AdminAnalystViewpointsPage,
})

function AdminAnalystViewpointsPage() {
  const status = Route.useLoaderData()
  const { locale } = Route.useRouteContext()
  return <AnalystViewpointManagementPage status={status} locale={locale} />
}
