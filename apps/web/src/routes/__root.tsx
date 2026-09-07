import { localeSchema } from "@daily-insights/api-client"
import {
  HeadContent,
  Outlet,
  Scripts,
  createRootRoute,
  useParams,
} from "@tanstack/react-router"
import { MotionConfig } from "motion/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { useMemo, useState } from "react"
import { I18nextProvider } from "react-i18next"
import {
  ErrorScreen,
  LoadingScreen,
  NotFoundScreen,
} from "#/components/StateScreen"
import { createI18n } from "#/lib/i18n"
import "../styles.css"

const THEME_INIT_SCRIPT = `(function(){try{var stored=window.localStorage.getItem('theme');var mode=(stored==='light'||stored==='dark'||stored==='auto')?stored:'light';var prefersDark=window.matchMedia('(prefers-color-scheme: dark)').matches;var resolved=mode==='auto'?(prefersDark?'dark':'light'):mode;var root=document.documentElement;root.classList.remove('light','dark');root.classList.add(resolved);if(mode==='auto'){root.removeAttribute('data-theme')}else{root.setAttribute('data-theme',mode)}root.style.colorScheme=resolved;}catch(e){}})();`

export const Route = createRootRoute({
  head: () => ({
    meta: [
      { charSet: "utf-8" },
      { name: "viewport", content: "width=device-width, initial-scale=1" },
      { title: "Daily Insights" },
    ],
    links: [
      { rel: "icon", href: "/favicon.ico", type: "image/x-icon" },
      // Noto Sans TC is the design's primary face; the local CJK fonts in the
      // stack cover the page until it arrives (display=swap).
      { rel: "preconnect", href: "https://fonts.googleapis.com" },
      {
        rel: "preconnect",
        href: "https://fonts.gstatic.com",
        crossOrigin: "anonymous",
      },
      {
        rel: "stylesheet",
        href: "https://fonts.googleapis.com/css2?family=Noto+Sans:wght@400;500;600;700&family=Noto+Sans+TC:wght@400;500;600;700&display=swap",
      },
    ],
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
  // A fresh client is created for every SSR render, while useState preserves a
  // single client for the browser lifetime after hydration.
  const [queryClient] = useState(() => new QueryClient())

  return (
    <QueryClientProvider client={queryClient}>
      <I18nextProvider i18n={i18n}>
        <html lang={locale} suppressHydrationWarning>
          <head>
            <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
            <HeadContent />
          </head>
          <body>
            <MotionConfig reducedMotion="user">
              <Outlet />
            </MotionConfig>
            <Scripts />
          </body>
        </html>
      </I18nextProvider>
    </QueryClientProvider>
  )
}
