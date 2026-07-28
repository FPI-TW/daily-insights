import type { User } from "@daily-insights/api-client"

export type Destination = "login" | "change-password" | "customer" | "admin"
export const INTERNAL_CUSTOMER_ORGANIZATION = "admin"

export function destinationFor(user: User | null): Destination {
  if (!user) return "login"
  if (user.must_change_password) return "change-password"
  return user.system_role === "org_member" ? "customer" : "admin"
}

export function canEnterCustomer(user: User) {
  return customerOrganizationScope(user) !== null
}

export function customerOrganizationScope(user: User) {
  if (user.system_role === "admin" || user.system_role === "asset_manager") {
    return INTERNAL_CUSTOMER_ORGANIZATION
  }
  return user.organization_id
}

export function canEnterBackOffice(user: User) {
  return user.system_role === "admin" || user.system_role === "asset_manager"
}

export function canEnterAdmin(user: User) {
  return user.system_role === "admin"
}
