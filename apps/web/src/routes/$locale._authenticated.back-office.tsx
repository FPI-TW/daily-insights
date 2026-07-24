import { Outlet, createFileRoute } from "@tanstack/react-router"
import { ForbiddenScreen } from "#/components/StateScreen"
import { canEnterBackOffice } from "#/lib/authorization"

export const Route = createFileRoute("/$locale/_authenticated/back-office")({
  beforeLoad: ({ context }) => ({
    forbidden: !canEnterBackOffice(context.user),
  }),
  component: BackOfficeLayout,
})

function BackOfficeLayout() {
  const { forbidden } = Route.useRouteContext()
  return forbidden ? <ForbiddenScreen /> : <Outlet />
}
