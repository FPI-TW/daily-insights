import { createFileRoute, redirect } from "@tanstack/react-router"
import { MemberManagementPage } from "#/components/MemberManagementPage"
import { ErrorScreen, LoadingScreen } from "#/components/StateScreen"
import { getMemberDirectory } from "#/lib/admin-members"

export const Route = createFileRoute(
  "/$locale/_authenticated/_admin/admin/members"
)({
  beforeLoad: ({ context }) => {
    if (context.user.system_role !== "admin") {
      throw redirect({
        to: "/$locale/admin/audio",
        params: { locale: context.locale },
      })
    }
  },
  loader: () => getMemberDirectory(),
  pendingComponent: LoadingScreen,
  errorComponent: ErrorScreen,
  component: AdminMembersPage,
})

function AdminMembersPage() {
  const directory = Route.useLoaderData()
  const { locale } = Route.useRouteContext()
  return <MemberManagementPage directory={directory} locale={locale} />
}
