import { createFileRoute, redirect } from "@tanstack/react-router"
import { PortalLogin } from "#/components/PortalLogin"
import { canEnterCustomer } from "#/lib/authorization"

export const Route = createFileRoute("/$locale/login")({
  beforeLoad: ({ context }) => {
    if (context.user?.must_change_password && canEnterCustomer(context.user)) {
      throw redirect({
        to: "/$locale/change-password",
        params: { locale: context.locale },
      })
    }
    if (context.user && canEnterCustomer(context.user)) {
      throw redirect({
        to: "/$locale/podcasts",
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
