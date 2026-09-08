import { createFileRoute, redirect } from "@tanstack/react-router"
import { customerEntryDestination } from "#/lib/authorization"

export const Route = createFileRoute("/{-$locale}/")({
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
    throw redirect({
      to:
        destination === "admin-change-password"
          ? "/{-$locale}/admin/change-password"
          : "/{-$locale}/reports",
      params: { locale: context.locale },
    })
  },
})
