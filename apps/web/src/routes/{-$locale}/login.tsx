import { createFileRoute, redirect } from "@tanstack/react-router"
import { PortalLogin } from "#/components/PortalLogin"
import { customerEntryDestination } from "#/lib/authorization"

export const Route = createFileRoute("/{-$locale}/login")({
  beforeLoad: ({ context }) => {
    const destination = customerEntryDestination(context.user)
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
    if (destination === "reports") {
      throw redirect({
        to: "/{-$locale}/reports",
        params: { locale: context.locale },
      })
    }
  },
  component: LoginPage,
})

function LoginPage() {
  const { locale } = Route.useRouteContext()
  return <PortalLogin locale={locale} portal="customer" />
}
