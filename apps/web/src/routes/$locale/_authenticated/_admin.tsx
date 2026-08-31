import { Outlet, createFileRoute, redirect } from "@tanstack/react-router"
import { AppShell } from "#/components/AppShell"
import { canEnterBackOffice, canEnterCustomer } from "#/lib/authorization"

export const Route = createFileRoute("/$locale/_authenticated/_admin")({
  beforeLoad: ({ context }) => {
    if (!context.user) {
      throw redirect({
        to: "/$locale/admin/login",
        params: { locale: context.locale },
      })
    }
    if (context.user.must_change_password) {
      throw redirect({
        to: canEnterBackOffice(context.user)
          ? "/$locale/admin/change-password"
          : "/$locale/change-password",
        params: { locale: context.locale },
      })
    }
    if (!canEnterBackOffice(context.user)) {
      throw redirect({
        to: canEnterCustomer(context.user)
          ? "/$locale/reports"
          : "/$locale/admin/login",
        params: { locale: context.locale },
      })
    }
    return { user: context.user }
  },
  component: AdminLayout,
})

function AdminLayout() {
  const { locale, user } = Route.useRouteContext()
  return (
    <AppShell locale={locale} user={user} surface="admin">
      <Outlet />
    </AppShell>
  )
}
