import type { Locale } from "@daily-insights/api-client"
import {
  type QueryKey,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { requireCsrfToken } from "#/lib/auth"
import {
  browserNewsroomAdminClient,
  newsroomAdminErrorKey,
} from "#/lib/newsroom-admin"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"

type Client = ReturnType<typeof browserNewsroomAdminClient>

/**
 * One admin write: fetches the CSRF token, runs the request, then
 * invalidates only the queries the action can change. A 401 sends the
 * editor to the admin login; any other failure becomes a translated message.
 */
export function useNewsroomAction<TVariables, TResult>({
  locale,
  run,
  invalidates,
  onSuccess,
}: {
  locale: Locale
  run: (
    client: Client,
    variables: TVariables,
    csrfToken: string
  ) => Promise<TResult>
  invalidates: (variables: TVariables, result: TResult) => QueryKey[]
  onSuccess?: (result: TResult, variables: TVariables) => void
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const redirectExpired = useSessionExpiryRedirect(locale, "admin")
  const mutation = useMutation({
    retry: false,
    mutationFn: async (variables: TVariables) =>
      run(browserNewsroomAdminClient(), variables, await requireCsrfToken()),
    onSuccess: async (result, variables) => {
      await Promise.all(
        invalidates(variables, result).map(queryKey =>
          queryClient.invalidateQueries({ queryKey })
        )
      )
      onSuccess?.(result, variables)
    },
  })
  return {
    isPending: mutation.isPending,
    error: mutation.error ? t(newsroomAdminErrorKey(mutation.error)) : "",
    reset: mutation.reset,
    async run(variables: TVariables) {
      try {
        return await mutation.mutateAsync(variables)
      } catch (caught) {
        await redirectExpired(caught)
        return undefined
      }
    },
  }
}
