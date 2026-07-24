import { z } from "zod"

export const localeSchema = z.enum(["zh-TW", "zh-CN", "en"])
export type Locale = z.infer<typeof localeSchema>

export const systemRoleSchema = z.enum(["admin", "asset_manager", "org_member"])
export type SystemRole = z.infer<typeof systemRoleSchema>

export const userSchema = z.object({
  id: z.uuid(),
  email: z.string(),
  display_name: z.string(),
  system_role: systemRoleSchema,
  status: z.literal("active"),
  must_change_password: z.boolean(),
  organization_id: z.uuid().nullable(),
})
export type User = z.infer<typeof userSchema>

export const authenticationSchema = z.object({
  user: userSchema,
  csrf_token: z.string().min(1),
})

export const csrfTokenSchema = z.object({
  csrf_token: z.string().min(1),
})

export const apiErrorSchema = z.object({
  detail: z.unknown().optional(),
})
