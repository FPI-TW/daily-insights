import { ApiError, type Locale } from "@daily-insights/api-client"
import { useRouter } from "@tanstack/react-router"
import { useCallback } from "react"
import { rememberCsrfToken } from "./auth"

export function useSessionExpiryRedirect(
  locale: Locale,
  surface: "customer" | "admin"
) {
  const router = useRouter()

  return useCallback(
    async (error: unknown) => {
      if (!(error instanceof ApiError) || error.status !== 401) return false

      rememberCsrfToken(null)
      try {
        await router.invalidate({ sync: true })
      } catch {
        // The explicit portal navigation below remains the recovery path.
      }
      if (surface === "customer") {
        await router.navigate({
          to: "/$locale/login",
          params: { locale },
          replace: true,
        })
      } else {
        await router.navigate({
          to: "/$locale/admin/login",
          params: { locale },
          replace: true,
        })
      }
      return true
    },
    [locale, router, surface]
  )
}
