import { Link, createFileRoute } from "@tanstack/react-router"
import { useTranslation } from "react-i18next"
import { PodcastPlayer } from "#/components/PodcastPlayer"
import { ErrorScreen, LoadingScreen } from "#/components/StateScreen"
import { getPodcastDetail } from "#/lib/podcasts"

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/podcasts/$episodeId"
)({
  loader: ({ context, params }) =>
    getPodcastDetail({
      data: { episodeId: params.episodeId, locale: context.locale },
    }),
  pendingComponent: LoadingScreen,
  errorComponent: ErrorScreen,
  component: PodcastDetailPage,
})

function PodcastDetailPage() {
  const episode = Route.useLoaderData()
  const { locale, user } = Route.useRouteContext()
  const { t } = useTranslation()
  return (
    <main className="podcast-page podcast-detail">
      <Link to="/$locale/podcasts" params={{ locale }} className="podcast-back">
        {t("podcastBack")}
      </Link>
      <article>
        <div className="podcast-cover" aria-hidden="true">
          <span>{episode.trading_date.slice(5)}</span>
        </div>
        <div className="podcast-copy">
          <p className="eyebrow">{t("podcastEyebrow")}</p>
          <time dateTime={episode.trading_date}>{episode.trading_date}</time>
          <h1>{episode.title}</h1>
          <p>{episode.summary}</p>
          <PodcastPlayer episodeId={episode.id} locale={locale} user={user} />
        </div>
      </article>
    </main>
  )
}
