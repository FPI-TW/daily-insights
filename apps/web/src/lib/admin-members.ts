import {
  createAdministrationClient,
  createBrowserTransport,
} from "@daily-insights/api-client"
import { createServerTransport } from "@daily-insights/api-client/server"
import { createServerFn } from "@tanstack/react-start"
import {
  getRequestHeader,
  setResponseHeader,
} from "@tanstack/react-start/server"

export const getMemberDirectory = createServerFn({ method: "GET" }).handler(
  async () => {
    setResponseHeader("Cache-Control", "no-store")
    const apiUrl = process.env.API_INTERNAL_URL
    if (!apiUrl) {
      throw new Error("API_INTERNAL_URL is required by the web server")
    }
    const cookie = getRequestHeader("cookie")
    const requestId = getRequestHeader("x-request-id")
    const client = createAdministrationClient(
      createServerTransport(apiUrl, {
        ...(cookie ? { cookie } : {}),
        ...(requestId ? { requestId } : {}),
      })
    )
    const organizations = await client.listOrganizations()
    return Promise.all(
      organizations.map(async organization => ({
        organization,
        members: await client.listMembers(organization.id),
      }))
    )
  }
)

export function browserAdministrationClient() {
  return createAdministrationClient(createBrowserTransport())
}
