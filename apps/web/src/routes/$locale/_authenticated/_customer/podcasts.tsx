import { Outlet, createFileRoute } from "@tanstack/react-router"

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/podcasts"
)({
  component: Outlet,
})
