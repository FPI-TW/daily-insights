import { createFileRoute } from "@tanstack/react-router"
import { AudioManagementPage } from "#/components/AudioManagementPage"
import { ErrorScreen, LoadingScreen } from "#/components/StateScreen"
import { getAdminPodcastList } from "#/lib/admin-podcasts"

export const Route = createFileRoute(
  "/$locale/_authenticated/_admin/admin/audio"
)({
  loader: () => getAdminPodcastList(),
  pendingComponent: LoadingScreen,
  errorComponent: ErrorScreen,
  component: AdminAudioPage,
})

function AdminAudioPage() {
  const episodes = Route.useLoaderData()
  const { locale, user } = Route.useRouteContext()
  return (
    <AudioManagementPage
      episodes={episodes}
      canPublish={
        user.system_role === "admin" || user.system_role === "asset_manager"
      }
      locale={locale}
    />
  )
}
