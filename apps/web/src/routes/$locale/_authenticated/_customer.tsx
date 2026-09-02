import { Outlet, createFileRoute, redirect } from "@tanstack/react-router"
import { AppShell } from "#/components/AppShell"
import { PageContextChatProvider } from "#/components/PageContextChat"
import { canEnterBackOffice, canEnterCustomer } from "#/lib/authorization"

export const Route = createFileRoute("/$locale/_authenticated/_customer")({
  beforeLoad: ({ context }) => {
    if (!context.user) {
      throw redirect({
        to: "/$locale/login",
        params: { locale: context.locale },
      })
    }
    if (context.user.must_change_password) {
      throw redirect({
        to: canEnterCustomer(context.user)
          ? "/$locale/change-password"
          : "/$locale/admin/change-password",
        params: { locale: context.locale },
      })
    }
    if (!canEnterCustomer(context.user)) {
      throw redirect({
        to: canEnterBackOffice(context.user)
          ? "/$locale/admin/audio"
          : "/$locale/login",
        params: { locale: context.locale },
      })
    }
    return { user: context.user }
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
