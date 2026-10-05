import { ApiError, type Locale } from "@daily-insights/api-client"
import { useQueryClient, type QueryClient } from "@tanstack/react-query"
import { useRouter } from "@tanstack/react-router"
import { useCallback } from "react"
import { getAuthSessionEpoch, rememberCsrfToken } from "./auth"

export function clearMarketQueries(client: QueryClient) {
  void client.cancelQueries({ queryKey: ["market"] })
  client.removeQueries({ queryKey: ["market"] })
}

const redirects = new WeakMap<
  object,
  { epoch: number; promise: Promise<boolean> }
>()
export function isSessionExpiryPending(router: object) {
  return redirects.get(router)?.epoch === getAuthSessionEpoch()
}

export function useSessionExpiryRedirect(
  locale: Locale,
  surface: "customer" | "admin"
) {
  const router = useRouter()
  const queryClient = useQueryClient()

  return useCallback(
    async (error: unknown) => {
      if (!(error instanceof ApiError) || error.status !== 401) return false

      const previous = redirects.get(router)
      if (previous?.epoch === getAuthSessionEpoch()) return previous.promise
      rememberCsrfToken(null)
      const promise = Promise.resolve().then(async () => {
        try {
          await router.invalidate({ sync: true })
        } catch {
          // The explicit portal navigation below remains the recovery path.
        }
        if (surface === "customer") {
          await router.navigate({
            to: "/{-$locale}/login",
            params: { locale },
            replace: true,
          })
        } else {
          await router.navigate({
            to: "/{-$locale}/admin/login",
            params: { locale },
            replace: true,
          })
        }
        return true
      })
      redirects.set(router, { epoch: getAuthSessionEpoch(), promise })
      clearMarketQueries(queryClient)
      return promise
    },
    [locale, queryClient, router, surface]
  )
}
