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
    <main className="page-shell">
      <header className="mb-8 max-w-3xl">
        <p className="eyebrow">{t("podcastEyebrow")}</p>
        <h1 className="mt-2 mb-3 text-[clamp(1.9rem,4vw,2.5rem)] leading-none font-extrabold tracking-[-0.045em]">
          {t("podcastTitle")}
        </h1>
        <span
          className="mb-4 block h-[3px] w-14 bg-lagoon"
          aria-hidden="true"
        />
        <p className="leading-7 text-sea-ink-soft">{t("podcastDescription")}</p>
      </header>
      {episodes.length === 0 ? (
        <section className="rounded-xl border border-dashed border-line bg-surface p-[clamp(2rem,6vw,4rem)] text-center">
          <h2 className="mt-0">{t("podcastEmptyTitle")}</h2>
          <p className="mb-0 text-sea-ink-soft">
            {t("podcastEmptyDescription")}
          </p>
        </section>
      ) : (
        <ol className="m-0 grid list-none gap-4 p-0">
          {episodes.map(episode => (
            <li
              key={episode.id}
              className="surface-panel grid grid-cols-[5.5rem_minmax(0,1fr)] items-start gap-5 border-l-[3px] border-l-transparent p-4 text-sea-ink transition hover:border-l-lagoon hover:border-lagoon-deep max-[42rem]:grid-cols-[4.2rem_minmax(0,1fr)] max-[42rem]:gap-3 max-[42rem]:p-3.5"
            >
              <div
                className="grid aspect-square w-[5.5rem] items-end justify-items-start rounded-[10px] bg-lagoon p-2.5 font-mono text-white max-[42rem]:w-[4.2rem]"
                aria-hidden="true"
              >
                <span className="text-xl font-extrabold max-[42rem]:text-base">
                  {episode.trading_date.slice(5)}
                </span>
              </div>
              <article className="min-w-0">
                <time
                  className="text-xs font-extrabold tracking-[0.06em] text-kicker"
                  dateTime={episode.trading_date}
                >
                  {episode.trading_date}
                </time>
                <h2 className="mt-1 mb-0 text-[clamp(1.05rem,3vw,1.35rem)] tracking-[-0.02em]">
                  {episode.title}
                </h2>
                <p className="mt-2 mb-0 line-clamp-2 text-sm leading-6 text-sea-ink-soft">
                  {episode.summary}
                </p>
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
