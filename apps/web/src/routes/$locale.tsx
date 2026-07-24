import { localeSchema } from "@daily-insights/api-client"
import { Outlet, createFileRoute, notFound } from "@tanstack/react-router"
import { getAuthSnapshot } from "#/lib/auth"
import { ErrorScreen, LoadingScreen } from "#/components/StateScreen"

export const Route = createFileRoute("/$locale")({
  beforeLoad: async ({ params }) => {
    const locale = localeSchema.safeParse(params.locale)
    if (!locale.success) throw notFound()
    return { locale: locale.data, user: await getAuthSnapshot() }
  },
  pendingComponent: LoadingScreen,
  errorComponent: ({ error }) => <ErrorScreen error={error} />,
  component: Outlet,
})
