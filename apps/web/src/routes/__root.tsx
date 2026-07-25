import { localeSchema } from "@daily-insights/api-client"
import {
  HeadContent,
  Outlet,
  Scripts,
  createRootRoute,
  useParams,
} from "@tanstack/react-router"
import { useMemo } from "react"
import { I18nextProvider } from "react-i18next"
import {
  ErrorScreen,
  LoadingScreen,
  NotFoundScreen,
} from "#/components/StateScreen"
import { createI18n } from "#/lib/i18n"
import appCss from "../styles.css?url"

const THEME_INIT_SCRIPT = `(function(){try{var stored=window.localStorage.getItem('theme');var mode=(stored==='light'||stored==='dark'||stored==='auto')?stored:'auto';var prefersDark=window.matchMedia('(prefers-color-scheme: dark)').matches;var resolved=mode==='auto'?(prefersDark?'dark':'light'):mode;var root=document.documentElement;root.classList.remove('light','dark');root.classList.add(resolved);if(mode==='auto'){root.removeAttribute('data-theme')}else{root.setAttribute('data-theme',mode)}root.style.colorScheme=resolved;}catch(e){}})();`

export const Route = createRootRoute({
  head: () => ({
    meta: [
      { charSet: "utf-8" },
      { name: "viewport", content: "width=device-width, initial-scale=1" },
      { title: "Daily Insights" },
    ],
    links: [{ rel: "stylesheet", href: appCss }],
  }),
  component: RootComponent,
  pendingComponent: LoadingScreen,
  errorComponent: ({ error }) => <ErrorScreen error={error} />,
  notFoundComponent: NotFoundScreen,
})

function RootComponent() {
  const params = useParams({ strict: false })
  const parsedLocale = localeSchema.safeParse(params.locale)
  const locale = parsedLocale.success ? parsedLocale.data : "zh-hant"
  const i18n = useMemo(() => createI18n(locale), [locale])

  return (
    <I18nextProvider i18n={i18n}>
      <html lang={locale} suppressHydrationWarning>
        <head>
          <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
          <HeadContent />
        </head>
        <body>
          <Outlet />
          <Scripts />
        </body>
      </html>
    </I18nextProvider>
  )
}
