import {
  createFileRoute,
  Link,
  redirect,
  useNavigate,
} from "@tanstack/react-router"
import { useTranslation } from "react-i18next"
import { NewsroomReviewPage } from "#/components/newsroom-admin/NewsroomReviewPage"
import { reviewSearchSchema, taipeiToday } from "#/lib/newsroom-admin"

export const Route = createFileRoute(
  "/{-$locale}/admin/_authenticated/newsroom/"
)({
  beforeLoad: ({ context }) => {
    if (context.user.system_role !== "admin") {
      throw redirect({
        to: "/{-$locale}/admin/audio",
        params: { locale: context.locale },
      })
    }
  },
  // ?date=YYYY-MM-DD (default: today in Taipei) — the Slack "draft ready"
  // notification links here — and ?market= for the open tab.
  validateSearch: reviewSearchSchema,
  component: NewsroomReviewRoute,
})

function NewsroomReviewRoute() {
  const { locale } = Route.useRouteContext()
  const search = Route.useSearch()
  const navigate = useNavigate({ from: Route.fullPath })
  const { t } = useTranslation()
  return (
    <NewsroomReviewPage
      locale={locale}
      date={search.date ?? taipeiToday()}
      market={search.market ?? "global"}
      onDateChange={date =>
        void navigate({ search: previous => ({ ...previous, date }) })
      }
      onMarketChange={market =>
        void navigate({
          search: previous => ({ ...previous, market }),
          replace: true,
        })
      }
      headerAction={
        <Link
          to="/{-$locale}/admin/newsroom/sources"
          params={{ locale }}
          className="font-bold text-lagoon-deep"
        >
          {t("newsroomAdminSourcesLink")}
        </Link>
      }
    />
  )
}
