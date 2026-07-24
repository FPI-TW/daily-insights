import { z } from "zod"
import {
  apiErrorSchema,
  authenticationSchema,
  csrfTokenSchema,
  userSchema,
} from "./schemas"

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly requestId: string | null,
    message: string
  ) {
    super(message)
    this.name = "ApiError"
  }
}

export type ApiTransport = (
  path: string,
  init?: RequestInit
) => Promise<Response>

export function createBrowserTransport(): ApiTransport {
  return (path, init) =>
    fetch(path, {
      ...init,
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        ...init?.headers,
      },
    })
}

async function parseResponse<T>(
  response: Response,
  schema: z.ZodType<T>
): Promise<T> {
  if (!response.ok) {
    const parsed = apiErrorSchema.safeParse(
      await response.json().catch(() => ({}))
    )
    const detail =
      parsed.success && typeof parsed.data.detail === "string"
        ? parsed.data.detail
        : `API request failed (${response.status})`
    throw new ApiError(
      response.status,
      response.headers.get("X-Request-ID"),
      detail
    )
  }

  const result = schema.safeParse(await response.json())
  if (!result.success) {
    throw new ApiError(
      502,
      response.headers.get("X-Request-ID"),
      "API response failed schema validation"
    )
  }
  return result.data
}

export function createAuthClient(transport: ApiTransport) {
  return {
    async login(input: { email: string; password: string }) {
      const response = await transport("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
      })
      return parseResponse(response, authenticationSchema)
    },
    async me() {
      return parseResponse(await transport("/api/auth/me"), userSchema)
    },
    async rotateCsrfToken() {
      return parseResponse(
        await transport("/api/auth/csrf", { method: "POST" }),
        csrfTokenSchema
      )
    },
    async changePassword(
      input: { current_password: string; new_password: string },
      csrfToken: string
    ) {
      const response = await transport("/api/auth/change-password", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify(input),
      })
      return parseResponse(response, authenticationSchema)
    },
    async logout(csrfToken: string) {
      const response = await transport("/api/auth/logout", {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken },
      })
      if (!response.ok) {
        throw new ApiError(
          response.status,
          response.headers.get("X-Request-ID"),
          "Unable to sign out"
        )
      }
    },
  }
}
