import { Outlet, createFileRoute } from "@tanstack/react-router"

export const Route = createFileRoute("/{-$locale}/_customer/podcasts")({
  component: Outlet,
})
