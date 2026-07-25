import { createFileRoute, redirect } from "@tanstack/react-router"
import { PortalChangePassword } from "#/components/PortalChangePassword"
import { canEnterBackOffice, canEnterCustomer } from "#/lib/authorization"

export const Route = createFileRoute("/$locale/admin/change-password")({
  beforeLoad: ({ context }) => {
    if (!context.user) {
      throw redirect({
        to: "/$locale/admin/login",
        params: { locale: context.locale },
      })
    }
    if (canEnterCustomer(context.user)) {
      throw redirect({
        to: context.user.must_change_password
          ? "/$locale/change-password"
          : "/$locale/podcasts",
        params: { locale: context.locale },
      })
    }
    if (!canEnterBackOffice(context.user)) {
      throw redirect({
        to: "/$locale/admin/login",
        params: { locale: context.locale },
      })
    }
    if (!context.user.must_change_password) {
      throw redirect({
        to: "/$locale/admin/audio",
        params: { locale: context.locale },
      })
    }
    return { user: context.user }
  },
  component: AdminChangePasswordPage,
})

function AdminChangePasswordPage() {
  const { locale } = Route.useRouteContext()
  return <PortalChangePassword locale={locale} portal="admin" />
}
