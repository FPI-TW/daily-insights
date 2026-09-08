import { Outlet, createFileRoute, redirect } from "@tanstack/react-router"
import { AppShell } from "#/components/AppShell"
import { adminEntryDestination } from "#/lib/authorization"

export const Route = createFileRoute("/{-$locale}/admin/_authenticated")({
  beforeLoad: ({ context }) => {
    const destination = adminEntryDestination(context.user)
    if (destination === "admin-login") {
      throw redirect({
        to: "/{-$locale}/admin/login",
        params: { locale: context.locale },
      })
    }
    if (destination === "admin-change-password") {
      throw redirect({
        to: "/{-$locale}/admin/change-password",
        params: { locale: context.locale },
      })
    }
    if (destination === "customer-change-password") {
      throw redirect({
        to: "/{-$locale}/change-password",
        params: { locale: context.locale },
      })
    }
    if (destination === "reports") {
      throw redirect({
        to: "/{-$locale}/reports",
        params: { locale: context.locale },
      })
    }
    return { user: context.user! }
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
