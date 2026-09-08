import { createFileRoute, redirect } from "@tanstack/react-router"
import { adminEntryDestination } from "#/lib/authorization"

export const Route = createFileRoute("/{-$locale}/admin/")({
  beforeLoad: ({ context }) => {
    const destination = adminEntryDestination(context.user)
    const to =
      destination === "admin-login"
        ? "/{-$locale}/admin/login"
        : destination === "admin-change-password"
          ? "/{-$locale}/admin/change-password"
          : destination === "admin-audio"
            ? "/{-$locale}/admin/audio"
            : destination === "customer-change-password"
              ? "/{-$locale}/change-password"
              : "/{-$locale}/reports"
    throw redirect({ to, params: { locale: context.locale } })
  },
})
