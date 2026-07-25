import { createFileRoute, redirect } from "@tanstack/react-router"

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/account"
)({
  beforeLoad: ({ context }) => {
    throw redirect({
      to: "/$locale/podcasts",
      params: { locale: context.locale },
      replace: true,
    })
  },
})
