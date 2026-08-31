import { createFileRoute, redirect } from "@tanstack/react-router"
import { canEnterBackOffice } from "#/lib/authorization"

export const Route = createFileRoute("/$locale/_authenticated/back-office")({
  beforeLoad: ({ context }) => {
    throw redirect({
      to:
        context.user && canEnterBackOffice(context.user)
          ? "/$locale/admin/audio"
          : context.user
            ? "/$locale/reports"
            : "/$locale/admin/login",
      params: { locale: context.locale },
      replace: true,
    })
  },
})
