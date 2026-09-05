import { createFileRoute, redirect } from "@tanstack/react-router"

// Legacy detail URL: the list page hosts the player, so the episode is
// carried over as the selected one.
export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/podcasts/$episodeId"
)({
  beforeLoad: ({ context, params }) => {
    throw redirect({
      to: "/$locale/podcasts",
      params: { locale: context.locale },
      search: { episode: params.episodeId },
      replace: true,
    })
  },
})
