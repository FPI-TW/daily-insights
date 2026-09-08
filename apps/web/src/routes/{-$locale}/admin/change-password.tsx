import { createFileRoute, redirect } from "@tanstack/react-router"
import { PortalChangePassword } from "#/components/PortalChangePassword"
import { adminEntryDestination } from "#/lib/authorization"

export const Route = createFileRoute("/{-$locale}/admin/change-password")({
  beforeLoad: ({ context }) => {
    const destination = adminEntryDestination(context.user)
    if (destination === "admin-login") {
      throw redirect({
        to: "/{-$locale}/admin/login",
        params: { locale: context.locale },
      })
    }
    if (destination === "admin-audio") {
      throw redirect({
        to: "/{-$locale}/admin/audio",
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
  component: AdminChangePasswordPage,
})

function AdminChangePasswordPage() {
  const { locale } = Route.useRouteContext()
  return <PortalChangePassword locale={locale} portal="admin" />
}
