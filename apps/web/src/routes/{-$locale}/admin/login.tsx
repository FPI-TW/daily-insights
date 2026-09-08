import { createFileRoute, redirect } from "@tanstack/react-router"
import { PortalLogin } from "#/components/PortalLogin"
import { adminEntryDestination } from "#/lib/authorization"

export const Route = createFileRoute("/{-$locale}/admin/login")({
  beforeLoad: ({ context }) => {
    const destination = adminEntryDestination(context.user)
    if (destination === "admin-change-password") {
      throw redirect({
        to: "/{-$locale}/admin/change-password",
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
  },
  component: AdminLoginPage,
})

function AdminLoginPage() {
  const { locale } = Route.useRouteContext()
  return <PortalLogin locale={locale} portal="admin" />
}
