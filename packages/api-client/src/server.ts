import type { ApiTransport } from "./client"

export function createServerTransport(
  baseUrl: string,
  headers: { cookie?: string; requestId?: string }
): ApiTransport {
  const origin = new URL(baseUrl).origin
  return (path, init) => {
    const approvedHeaders = new Headers({ Accept: "application/json" })
    const requestedHeaders = new Headers(init?.headers)
    for (const name of ["accept", "content-type", "x-csrf-token"]) {
      const value = requestedHeaders.get(name)
      if (value) approvedHeaders.set(name, value)
    }
    if (headers.cookie) approvedHeaders.set("Cookie", headers.cookie)
    if (headers.requestId) {
      approvedHeaders.set("X-Request-ID", headers.requestId)
    }
    return fetch(new URL(path, origin), {
      ...init,
      headers: approvedHeaders,
    })
  }
}
