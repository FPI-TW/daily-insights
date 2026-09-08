import { describe, expect, it } from "vitest"
import type { User } from "@daily-insights/api-client"
import {
  adminEntryDestination,
  canEnterAdmin,
  canEnterBackOffice,
  canEnterCustomer,
  customerEntryDestination,
  customerOrganizationScope,
  INTERNAL_CUSTOMER_ORGANIZATION,
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
  it("uses separate customer and admin entry destinations", () => {
    expect(customerEntryDestination(null)).toBe("customer-login")
    expect(adminEntryDestination(null)).toBe("admin-login")
    expect(customerEntryDestination(user())).toBe("reports")
    expect(adminEntryDestination(user())).toBe("reports")
    expect(customerEntryDestination(user({ must_change_password: true }))).toBe(
      "customer-change-password"
    )
    expect(adminEntryDestination(user({ must_change_password: true }))).toBe(
      "customer-change-password"
    )
  })

  it("allows back-office roles to use the virtual admin customer organization", () => {
    const member = user()
    const assetManager = user({
      system_role: "asset_manager",
      organization_id: null,
    })
    const admin = user({ system_role: "admin", organization_id: null })

    expect(customerEntryDestination(member)).toBe("reports")
    expect(customerEntryDestination(assetManager)).toBe("reports")
    expect(customerEntryDestination(admin)).toBe("reports")
    expect(adminEntryDestination(assetManager)).toBe("admin-audio")
    expect(adminEntryDestination(admin)).toBe("admin-audio")
    expect(
      customerEntryDestination(
        user({
          system_role: "admin",
          organization_id: null,
          must_change_password: true,
        })
      )
    ).toBe("admin-change-password")
    expect(
      adminEntryDestination(
        user({
          system_role: "asset_manager",
          organization_id: null,
          must_change_password: true,
        })
      )
    ).toBe("admin-change-password")
    expect(canEnterCustomer(member)).toBe(true)
    expect(canEnterBackOffice(member)).toBe(false)
    expect(canEnterCustomer(assetManager)).toBe(true)
    expect(canEnterCustomer(admin)).toBe(true)
    expect(customerOrganizationScope(assetManager)).toBe(
      INTERNAL_CUSTOMER_ORGANIZATION
    )
    expect(customerOrganizationScope(admin)).toBe(
      INTERNAL_CUSTOMER_ORGANIZATION
    )
    expect(canEnterBackOffice(assetManager)).toBe(true)
    expect(canEnterAdmin(assetManager)).toBe(false)
    expect(canEnterAdmin(admin)).toBe(true)
  })

  it("rejects an org member without an active organization context", () => {
    expect(canEnterCustomer(user({ organization_id: null }))).toBe(false)
    expect(customerOrganizationScope(user({ organization_id: null }))).toBe(
      null
    )
    expect(customerEntryDestination(user({ organization_id: null }))).toBe(
      "customer-login"
    )
    expect(adminEntryDestination(user({ organization_id: null }))).toBe(
      "admin-login"
    )
  })
})
