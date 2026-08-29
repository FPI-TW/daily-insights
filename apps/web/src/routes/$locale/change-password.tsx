import { createFileRoute, redirect } from "@tanstack/react-router"
import { PortalChangePassword } from "#/components/PortalChangePassword"
import { canEnterBackOffice, canEnterCustomer } from "#/lib/authorization"

export const Route = createFileRoute("/$locale/change-password")({
  beforeLoad: ({ context }) => {
    if (!context.user) {
      throw redirect({
        to: "/$locale/login",
        params: { locale: context.locale },
      })
    }
    if (canEnterBackOffice(context.user)) {
      throw redirect({
        to: context.user.must_change_password
          ? "/$locale/admin/change-password"
          : "/$locale/admin/audio",
        params: { locale: context.locale },
      })
    }
    if (!canEnterCustomer(context.user)) {
      throw redirect({
        to: "/$locale/login",
        params: { locale: context.locale },
      })
    }
    if (!context.user.must_change_password) {
      throw redirect({
        to: "/$locale/reports",
        params: { locale: context.locale },
      })
    }
    return { user: context.user }
  },
  component: ChangePasswordPage,
})

function ChangePasswordPage() {
  const { locale } = Route.useRouteContext()
  return <PortalChangePassword locale={locale} portal="customer" />
}
