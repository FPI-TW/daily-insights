import { localeSchema } from "@daily-insights/api-client"
import {
  Outlet,
  createFileRoute,
  notFound,
  redirect,
} from "@tanstack/react-router"
import { getAuthSnapshot } from "#/lib/auth"
import { ErrorScreen, LoadingScreen } from "#/components/StateScreen"
import { brandTitleFor } from "#/lib/brand"

const defaultLocale = "zh-hant"

export const Route = createFileRoute("/{-$locale}")({
  beforeLoad: async ({ location, params }) => {
    if (!params.locale) {
      throw redirect({
        href: `/${defaultLocale}${location.href}`,
        replace: true,
      })
    }
    const locale = localeSchema.safeParse(params.locale)
    if (!locale.success) throw notFound()
    return { locale: locale.data, user: await getAuthSnapshot() }
  },
  head: ({ params }) => ({
    meta: [{ title: brandTitleFor(params.locale ?? defaultLocale) }],
  }),
  pendingComponent: LoadingScreen,
  errorComponent: ({ error }) => <ErrorScreen error={error} />,
  component: Outlet,
})
