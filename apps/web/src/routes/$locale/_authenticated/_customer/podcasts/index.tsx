import { createFileRoute } from "@tanstack/react-router"
import { useTranslation } from "react-i18next"
import { PodcastPlayer } from "#/components/PodcastPlayer"
import { ErrorScreen, LoadingScreen } from "#/components/StateScreen"
import { getPodcastList } from "#/lib/podcasts"

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/podcasts/"
)({
  loader: ({ context }) => getPodcastList({ data: context.locale }),
  pendingComponent: LoadingScreen,
  errorComponent: ErrorScreen,
  component: PodcastListPage,
})

function PodcastListPage() {
  const episodes = Route.useLoaderData()
  const { locale, user } = Route.useRouteContext()
  const { t } = useTranslation()

  return (
    <main className="podcast-page">
      <header className="podcast-hero">
        <p className="eyebrow">{t("podcastEyebrow")}</p>
        <h1>{t("podcastTitle")}</h1>
        <p>{t("podcastDescription")}</p>
      </header>
      {episodes.length === 0 ? (
        <section className="podcast-empty">
          <h2>{t("podcastEmptyTitle")}</h2>
          <p>{t("podcastEmptyDescription")}</p>
        </section>
      ) : (
        <ol className="podcast-list">
          {episodes.map(episode => (
            <li key={episode.id} className="podcast-card">
              <div className="podcast-card-cover" aria-hidden="true">
                <span>{episode.trading_date.slice(5)}</span>
              </div>
              <article className="podcast-card-copy">
                <time dateTime={episode.trading_date}>
                  {episode.trading_date}
                </time>
                <h2>{episode.title}</h2>
                <p>{episode.summary}</p>
                <PodcastPlayer
                  key={`${episode.id}:${locale}`}
                  episodeId={episode.id}
                  locale={locale}
                  user={user}
                  title={episode.title}
                />
              </article>
            </li>
          ))}
        </ol>
      )}
    </main>
  )
}
