import { Outlet, createFileRoute } from "@tanstack/react-router"
import { ForbiddenScreen } from "#/components/StateScreen"
import { canEnterCustomer } from "#/lib/authorization"

export const Route = createFileRoute("/$locale/_authenticated/_customer")({
  beforeLoad: ({ context }) => {
    if (!canEnterCustomer(context.user)) {
      return { forbidden: true }
    }
    return { forbidden: false }
  },
  component: CustomerLayout,
})

function CustomerLayout() {
  const { forbidden } = Route.useRouteContext()
  return forbidden ? <ForbiddenScreen /> : <Outlet />
}
