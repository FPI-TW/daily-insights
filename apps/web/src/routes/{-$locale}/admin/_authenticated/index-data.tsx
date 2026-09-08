import { createFileRoute, redirect } from "@tanstack/react-router"

export const Route = createFileRoute(
  "/{-$locale}/admin/_authenticated/index-data"
)({
  beforeLoad: ({ context }) => {
    if (context.user.system_role !== "admin") {
      throw redirect({
        to: "/{-$locale}/admin/audio",
        params: { locale: context.locale },
      })
    }
    throw redirect({
      to: "/{-$locale}/admin/data-management",
      params: { locale: context.locale },
    })
  },
  component: () => null,
})
