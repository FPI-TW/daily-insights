import type { User } from "@daily-insights/api-client"
import { isRedirect } from "@tanstack/react-router"
import { describe, expect, it } from "vitest"
import { Route } from "./change-password"

function user(overrides: Partial<User> = {}): User {
  return {
    id: "9b2b0a80-cf47-4a12-aa3e-477d342c54f8",
    email: "member@example.com",
    display_name: "Member",
    system_role: "org_member",
    status: "active",
    must_change_password: false,
    organization_id: "b61ecb61-af15-4a57-90d3-e74a7849acc1",
    ...overrides,
  }
}

type BeforeLoadContext = Parameters<
  NonNullable<typeof Route.options.beforeLoad>
>[0]

function runBeforeLoad(user: User | null) {
  const beforeLoad = Route.options.beforeLoad
  if (!beforeLoad) throw new Error("change-password route guard is missing")

  return beforeLoad({
    context: { locale: "en", user },
  } as BeforeLoadContext)
}

function redirectOptions(user: User | null) {
  try {
    runBeforeLoad(user)
    throw new Error("expected the route guard to redirect")
  } catch (error) {
    if (!isRedirect(error)) throw error
    return error.options
  }
}

describe("admin change-password route guard", () => {
  it.each(["admin", "asset_manager"] as const)(
    "keeps %s users with temporary passwords in the admin portal",
    system_role => {
      expect(
        runBeforeLoad(
          user({
            system_role,
            must_change_password: true,
            organization_id: null,
          })
        )
      ).toEqual({
        user: expect.objectContaining({
          system_role,
          must_change_password: true,
        }),
      })
    }
  )

  it.each(["admin", "asset_manager"] as const)(
    "sends %s users with changed passwords to admin audio",
    system_role => {
      expect(
        redirectOptions(
          user({
            system_role,
            organization_id: null,
            must_change_password: false,
          })
        )
      ).toMatchObject({
        to: "/$locale/admin/audio",
        params: { locale: "en" },
      })
    }
  )

  it("sends customer users to the customer destination", () => {
    expect(redirectOptions(user({ must_change_password: true }))).toMatchObject(
      {
        to: "/$locale/change-password",
        params: { locale: "en" },
      }
    )
    expect(
      redirectOptions(user({ must_change_password: false }))
    ).toMatchObject({
      to: "/$locale/reports",
      params: { locale: "en" },
    })
  })

  it("sends unauthenticated and disallowed users to admin login", () => {
    expect(redirectOptions(null)).toMatchObject({
      to: "/$locale/admin/login",
      params: { locale: "en" },
    })
    expect(redirectOptions(user({ organization_id: null }))).toMatchObject({
      to: "/$locale/admin/login",
      params: { locale: "en" },
    })
  })
})
