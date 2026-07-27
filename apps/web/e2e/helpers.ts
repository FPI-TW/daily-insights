import type { APIRequestContext, BrowserContext, Page } from "@playwright/test"

export const episodeId = "10000000-0000-4000-8000-000000000001"
export const customerCredentials = {
  email: "customer@example.test",
  password: "customer-password",
}
export const adminCredentials = {
  email: "admin@example.test",
  password: "admin-password",
}

type MockState = {
  podcastList?: "normal" | "empty" | "error"
  audio?: "normal" | "error" | "delayed"
  passwordChange?: "normal" | "error"
  sessionExpired?: boolean
}

export type RecordedRequest = {
  method: string
  path: string
  role: "admin" | "org_member" | null
  facts?: Record<string, unknown>
}

export type MockApiState = MockState & {
  status: "draft" | "published"
  episodeVersion: number
  audioVersion: number
  requests: RecordedRequest[]
}

export async function resetMockApi(
  request: APIRequestContext,
  state: MockState = {}
) {
  const response = await request.post("http://127.0.0.1:3311/__e2e/reset", {
    data: state,
  })
  if (!response.ok()) {
    throw new Error(`Unable to reset mock API (${response.status()})`)
  }
}

export async function getMockApiState(request: APIRequestContext) {
  const response = await request.get("http://127.0.0.1:3311/__e2e/state")
  if (!response.ok()) {
    throw new Error(`Unable to read mock API state (${response.status()})`)
  }
  return (await response.json()) as MockApiState
}

export async function authenticateAs(
  context: BrowserContext,
  role: "admin" | "org_member"
) {
  await context.addCookies([
    {
      name: "e2e-role",
      value: role,
      domain: "127.0.0.1",
      path: "/",
      httpOnly: true,
      sameSite: "Lax",
    },
  ])
}

export async function openHydrated(
  page: Page,
  path: string,
  targetSelector: string
) {
  await page.goto(path)
  await page.waitForFunction(selector => {
    const element = document.querySelector(selector)
    return (
      element !== null &&
      Object.keys(element).some(key => key.startsWith("__reactProps$"))
    )
  }, targetSelector)
}
