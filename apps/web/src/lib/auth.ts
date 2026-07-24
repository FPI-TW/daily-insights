import {
  ApiError,
  createAuthClient,
  createBrowserTransport,
  type User,
} from "@daily-insights/api-client"
import { createServerTransport } from "@daily-insights/api-client/server"
import { createServerFn } from "@tanstack/react-start"
import { getRequestHeader } from "@tanstack/react-start/server"

let csrfToken: string | null = null

export const getAuthSnapshot = createServerFn({ method: "GET" }).handler(
  async (): Promise<User | null> => {
    const apiUrl = process.env.API_INTERNAL_URL
    if (!apiUrl) {
      throw new Error("API_INTERNAL_URL is required by the web server")
    }
    const cookie = getRequestHeader("cookie")
    const requestId = getRequestHeader("x-request-id")
    const client = createAuthClient(
      createServerTransport(apiUrl, {
        ...(cookie ? { cookie } : {}),
        ...(requestId ? { requestId } : {}),
      })
    )
    try {
      return await client.me()
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return null
      throw error
    }
  }
)

export function browserAuthClient() {
  return createAuthClient(createBrowserTransport())
}

export function rememberCsrfToken(token: string | null) {
  csrfToken = token
}

export async function requireCsrfToken() {
  if (csrfToken) return csrfToken
  const result = await browserAuthClient().rotateCsrfToken()
  csrfToken = result.csrf_token
  return csrfToken
}
