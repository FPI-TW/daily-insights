import { createServerFn } from "@tanstack/react-start"
import {
  getRequestHeader,
  setResponseHeader,
} from "@tanstack/react-start/server"
import { z } from "zod"

const conversationSchema = z.object({
  id: z.uuid(),
  organization_id: z.uuid(),
  user_id: z.uuid(),
  member_email: z.string(),
  title: z.string().nullable(),
  created_at: z.iso.datetime({ offset: true }),
  latest_message_at: z.iso.datetime({ offset: true }).nullable(),
  message_count: z.number().int(),
})
const listSchema = z.object({
  items: z.array(conversationSchema),
  next_cursor: z.string().nullable(),
})
export const adminConversationSearchSchema = z.object({
  organization_id: z.uuid().optional(),
  member_id: z.uuid().optional(),
  created_after: z.iso.date().optional(),
  created_before: z.iso.date().optional(),
  cursor: z.string().min(1).optional(),
  history: z.array(z.string().min(1)).catch([]),
})
export type AdminConversationSearch = z.infer<
  typeof adminConversationSearchSchema
>
export const detailSchema = z.object({
  id: z.uuid(),
  organization_id: z.uuid(),
  user_id: z.uuid(),
  messages: z.array(
    z.object({
      id: z.uuid(),
      role: z.enum(["user", "assistant", "system"]),
      content: z.string(),
      status: z.enum(["pending", "complete", "partial", "error"]),
      reply_to_message_id: z.uuid().nullable(),
      created_at: z.iso.datetime({ offset: true }),
    })
  ),
  generations: z.array(
    z.object({
      id: z.uuid(),
      provider: z.string(),
      requested_model: z.string(),
      resolved_model: z.string().nullable(),
      prompt_version: z.string(),
      context_digest: z.string().nullable(),
      context_truncated: z.boolean(),
      input_tokens: z.number().int().nullable(),
      output_tokens: z.number().int().nullable(),
      latency_ms: z.number().int().nullable(),
      status: z.enum(["pending", "complete", "partial", "error"]),
      error_code: z.string().nullable(),
      created_at: z.iso.datetime({ offset: true }),
    })
  ),
})
export type AdminConversation = z.infer<typeof detailSchema>

async function api(path: string) {
  const apiUrl = process.env.API_INTERNAL_URL
  if (!apiUrl) throw new Error("API_INTERNAL_URL is required by the web server")
  const cookie = getRequestHeader("cookie")
  const requestId = getRequestHeader("x-request-id")
  const response = await fetch(`${apiUrl}${path}`, {
    headers: {
      Accept: "application/json",
      ...(cookie ? { cookie } : {}),
      ...(requestId ? { "X-Request-ID": requestId } : {}),
    },
    cache: "no-store",
  })
  if (!response.ok) throw new Error("admin chat request failed")
  return response.json()
}

export const getAdminConversations = createServerFn({ method: "GET" })
  .validator(adminConversationSearchSchema)
  .handler(async ({ data }) => {
    setResponseHeader("Cache-Control", "no-store")
    const params = new URLSearchParams()
    if (data.organization_id)
      params.set("organization_id", data.organization_id)
    if (data.member_id) params.set("member_id", data.member_id)
    if (data.created_after)
      params.set("created_after", `${data.created_after}T00:00:00Z`)
    if (data.created_before)
      params.set("created_before", `${data.created_before}T23:59:59Z`)
    if (data.cursor) params.set("cursor", data.cursor)
    const query = params.size ? `?${params}` : ""
    return listSchema.parse(await api(`/api/admin/conversations${query}`))
  })

export const getAdminConversation = createServerFn({ method: "GET" })
  .validator(z.object({ id: z.uuid() }))
  .handler(async ({ data }) => {
    setResponseHeader("Cache-Control", "no-store")
    return detailSchema.parse(await api(`/api/admin/conversations/${data.id}`))
  })
