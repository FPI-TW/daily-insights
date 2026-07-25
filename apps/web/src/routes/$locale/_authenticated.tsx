import { Outlet, createFileRoute, redirect } from "@tanstack/react-router"
import { AppShell } from "#/components/AppShell"

export const Route = createFileRoute("/$locale/_authenticated")({
  beforeLoad: ({ context }) => {
    if (!context.user) {
      throw redirect({
        to: "/$locale/login",
        params: { locale: context.locale },
      })
    }
    if (context.user.must_change_password) {
      throw redirect({
        to: "/$locale/change-password",
        params: { locale: context.locale },
      })
    }
    return { user: context.user }
  },
  component: AuthenticatedLayout,
})

function AuthenticatedLayout() {
  const { locale, user } = Route.useRouteContext()
  return (
    <AppShell locale={locale} user={user}>
      <Outlet />
    </AppShell>
  )
}
