import { describe, expect, it } from "vitest"
import type { User } from "@daily-insights/api-client"
import {
  canEnterAdmin,
  canEnterBackOffice,
  canEnterCustomer,
  destinationFor,
} from "./authorization"

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

describe("route authorization decisions", () => {
  it("gates unauthenticated and temporary-password sessions", () => {
    expect(destinationFor(null)).toBe("login")
    expect(destinationFor(user({ must_change_password: true }))).toBe(
      "change-password"
    )
  })

  it("keeps customer and back-office roles separated", () => {
    const member = user()
    const assetManager = user({
      system_role: "asset_manager",
      organization_id: null,
    })
    const admin = user({ system_role: "admin", organization_id: null })

    expect(destinationFor(member)).toBe("customer")
    expect(destinationFor(assetManager)).toBe("admin")
    expect(destinationFor(admin)).toBe("admin")
    expect(canEnterCustomer(member)).toBe(true)
    expect(canEnterBackOffice(member)).toBe(false)
    expect(canEnterCustomer(assetManager)).toBe(false)
    expect(canEnterBackOffice(assetManager)).toBe(true)
    expect(canEnterAdmin(assetManager)).toBe(false)
    expect(canEnterAdmin(admin)).toBe(true)
  })

  it("rejects an org member without an active organization context", () => {
    expect(canEnterCustomer(user({ organization_id: null }))).toBe(false)
  })
})
