import { createFileRoute, Link, redirect } from "@tanstack/react-router"
import { useTranslation } from "react-i18next"
import { NewsroomSourcesPage } from "#/components/newsroom-admin/NewsroomSourcesPage"

export const Route = createFileRoute(
  "/{-$locale}/admin/_authenticated/newsroom/sources"
)({
  beforeLoad: ({ context }) => {
    if (context.user.system_role !== "admin") {
      throw redirect({
        to: "/{-$locale}/admin/audio",
        params: { locale: context.locale },
      })
    }
  },
  component: NewsroomSourcesRoute,
})

function NewsroomSourcesRoute() {
  const { locale } = Route.useRouteContext()
  const { t } = useTranslation()
  return (
    <NewsroomSourcesPage
      locale={locale}
      headerAction={
        <Link
          to="/{-$locale}/admin/newsroom"
          params={{ locale }}
          className="self-center font-bold text-lagoon-deep"
        >
          {t("newsroomAdminReviewLink")}
        </Link>
      }
    />
  )
}
