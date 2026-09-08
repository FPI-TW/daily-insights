import { Outlet, createFileRoute, redirect } from "@tanstack/react-router"
import { AppShell } from "#/components/AppShell"
import { PageContextChatProvider } from "#/components/PageContextChat"
import { customerEntryDestination } from "#/lib/authorization"

export const Route = createFileRoute("/{-$locale}/_customer")({
  beforeLoad: ({ context }) => {
    const destination = customerEntryDestination(context.user)
    if (destination === "customer-login") {
      throw redirect({
        to: "/{-$locale}/login",
        params: { locale: context.locale },
      })
    }
    if (destination === "customer-change-password") {
      throw redirect({
        to: "/{-$locale}/change-password",
        params: { locale: context.locale },
      })
    }
    if (destination === "admin-change-password") {
      throw redirect({
        to: "/{-$locale}/admin/change-password",
        params: { locale: context.locale },
      })
    }
    return { user: context.user! }
  },
  component: CustomerLayout,
})

function CustomerLayout() {
  const { locale, user } = Route.useRouteContext()
  return (
    <PageContextChatProvider
      locale={locale}
      enabled={
        user.system_role === "org_member" && user.organization_id !== null
      }
    >
      <AppShell locale={locale} user={user} surface="customer">
        <Outlet />
      </AppShell>
    </PageContextChatProvider>
  )
}
