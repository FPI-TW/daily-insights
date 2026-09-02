import { createRouter as createTanStackRouter } from "@tanstack/react-router"
import { routeTree } from "./routeTree.gen"

function stripLocale(pathname: string) {
  return pathname.replace(/^\/[^/]+/, "")
}

export function getRouter() {
  const router = createTanStackRouter({
    routeTree,
    scrollRestoration: true,
    defaultPreload: "intent",
    defaultPreloadStaleTime: 0,
    // Loads that finish within 200ms swap straight to the content; slower
    // ones show the route's skeleton, held briefly so it never flashes. The
    // previous 500ms default minimum made every first visit to a locale or
    // market feel stuck on the skeleton even though data arrived in ~100ms.
    defaultPendingMs: 200,
    defaultPendingMinMs: 300,
    // Route changes crossfade through the View Transitions API; the timings
    // live in styles.css next to the reduced-motion override. Switching the
    // locale keeps the same page and only swaps its text, so it updates in
    // place instead of fading the whole page out and back in.
    defaultViewTransition: {
      types: ({ fromLocation, toLocation }) =>
        fromLocation &&
        stripLocale(fromLocation.pathname) === stripLocale(toLocation.pathname)
          ? false
          : ["page"],
    },
  })

  return router
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof getRouter>
  }
}
