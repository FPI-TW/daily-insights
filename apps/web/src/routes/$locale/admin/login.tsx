import { createFileRoute, redirect } from "@tanstack/react-router"
import { PortalLogin } from "#/components/PortalLogin"
import { canEnterBackOffice } from "#/lib/authorization"

export const Route = createFileRoute("/$locale/admin/login")({
  beforeLoad: ({ context }) => {
    if (
      context.user?.must_change_password &&
      canEnterBackOffice(context.user)
    ) {
      throw redirect({
        to: "/$locale/admin/change-password",
        params: { locale: context.locale },
      })
    }
    if (context.user && canEnterBackOffice(context.user)) {
      throw redirect({
        to: "/$locale/admin/audio",
        params: { locale: context.locale },
      })
    }
  },
  component: AdminLoginPage,
})

function AdminLoginPage() {
  const { locale } = Route.useRouteContext()
  return <PortalLogin locale={locale} portal="admin" />
}
