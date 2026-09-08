import { createFileRoute, redirect } from "@tanstack/react-router"
import { PortalChangePassword } from "#/components/PortalChangePassword"
import { customerEntryDestination } from "#/lib/authorization"

export const Route = createFileRoute("/{-$locale}/change-password")({
  beforeLoad: ({ context }) => {
    const destination = customerEntryDestination(context.user)
    if (destination === "customer-login") {
      throw redirect({
        to: "/{-$locale}/login",
        params: { locale: context.locale },
      })
    }
    if (destination === "admin-change-password") {
      throw redirect({
        to: "/{-$locale}/admin/change-password",
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
  component: ChangePasswordPage,
})

function ChangePasswordPage() {
  const { locale } = Route.useRouteContext()
  return <PortalChangePassword locale={locale} portal="customer" />
}
