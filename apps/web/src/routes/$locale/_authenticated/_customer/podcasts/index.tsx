import { createFileRoute } from "@tanstack/react-router"
import { z } from "zod"
import { PodcastPage, PodcastPageSkeleton } from "#/components/PodcastPage"
import { ErrorScreen } from "#/components/StateScreen"
import { getPodcastList } from "#/lib/podcasts"

// `?episode=<id>` opens the page on that episode (shared links, the legacy
// detail URL); anything else lands on the latest episode.
const searchSchema = z.object({
  episode: z.uuid().optional().catch(undefined),
})

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/podcasts/"
)({
  validateSearch: searchSchema,
  loader: async ({ context }) => {
    const episodes = await getPodcastList({ data: context.locale })
    // Newest first: the first entry is "today's" episode.
    return [...episodes].sort((a, b) =>
      b.trading_date.localeCompare(a.trading_date)
    )
  },
  pendingComponent: PodcastPageSkeleton,
  errorComponent: ErrorScreen,
  component: PodcastListPage,
})

function PodcastListPage() {
  const episodes = Route.useLoaderData()
  const { locale, user } = Route.useRouteContext()
  const { episode } = Route.useSearch()

  return (
    <PodcastPage
      key={`${locale}:${episode ?? ""}`}
      episodes={episodes}
      locale={locale}
      user={user}
      initialEpisodeId={episode}
    />
  )
}
