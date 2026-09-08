import { createFileRoute, redirect } from "@tanstack/react-router"

export const Route = createFileRoute("/{-$locale}/back-office/podcasts")({
  beforeLoad: ({ context }) => {
    throw redirect({
      to: "/{-$locale}/admin/audio",
      params: { locale: context.locale },
      replace: true,
    })
  },
})
