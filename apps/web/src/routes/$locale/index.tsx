import { createFileRoute, redirect } from "@tanstack/react-router"
import { destinationFor } from "#/lib/authorization"

export const Route = createFileRoute("/$locale/")({
  beforeLoad: ({ context }) => {
    const destination = destinationFor(context.user)
    if (destination === "login") {
      throw redirect({
        to: "/$locale/login",
        params: { locale: context.locale },
      })
    }
    if (destination === "change-password") {
      throw redirect({
        to: "/$locale/change-password",
        params: { locale: context.locale },
      })
    }
    if (destination === "customer") {
      throw redirect({
        to: "/$locale/reports",
        params: { locale: context.locale },
      })
    }
    throw redirect({
      to: "/$locale/admin/audio",
      params: { locale: context.locale },
    })
  },
})
