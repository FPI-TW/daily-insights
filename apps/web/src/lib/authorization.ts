import type { User } from "@daily-insights/api-client"

export type CustomerEntryDestination =
  | "customer-login"
  | "customer-change-password"
  | "admin-change-password"
  | "reports"
export type AdminEntryDestination =
  | "admin-login"
  | "admin-change-password"
  | "admin-audio"
  | "customer-change-password"
  | "reports"
export const INTERNAL_CUSTOMER_ORGANIZATION = "admin"

export function customerEntryDestination(
  user: User | null
): CustomerEntryDestination {
  if (!user || !canEnterCustomer(user)) return "customer-login"
  if (!user.must_change_password) return "reports"
  return canEnterBackOffice(user)
    ? "admin-change-password"
    : "customer-change-password"
}

export function adminEntryDestination(
  user: User | null
): AdminEntryDestination {
  if (!user) return "admin-login"
  if (canEnterBackOffice(user)) {
    return user.must_change_password ? "admin-change-password" : "admin-audio"
  }
  if (canEnterCustomer(user)) {
    return user.must_change_password ? "customer-change-password" : "reports"
  }
  return "admin-login"
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
