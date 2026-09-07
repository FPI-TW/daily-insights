import { createServer } from "node:http"

const episodeId = "10000000-0000-4000-8000-000000000001"
const assetId = "20000000-0000-4000-8000-000000000001"
const organizationId = "30000000-0000-4000-8000-000000000001"
const adminId = "40000000-0000-4000-8000-000000000001"
const memberId = "50000000-0000-4000-8000-000000000001"
const assetManagerId = "60000000-0000-4000-8000-000000000001"
const credentials = {
  "customer@example.test": {
    password: "customer-password",
    role: "org_member",
  },
  "admin@example.test": {
    password: "admin-password",
    role: "admin",
  },
  "asset-manager@example.test": {
    password: "asset-manager-password",
    role: "asset_manager",
  },
}
const port = Number(process.argv[process.argv.indexOf("--port") + 1] || 3311)

let state

function reset(overrides = {}) {
  state = {
    podcastList: "normal",
    audio: "normal",
    passwordChange: "normal",
    sessionExpired: false,
    status: "published",
    episodeVersion: 2,
    audioVersion: 1,
    chapters: [],
    chaptersSource: "none",
    metadata: null,
    metadataSource: "derived",
    analysis: "normal",
    analysisStatus: "none",
    analysisError: null,
    analyzedAt: null,
    podcastEpisodes: "single",
    reports: "normal",
    requests: [],
    ...overrides,
  }
}

reset()

function sendJson(response, status, value, headers = {}) {
  response.writeHead(status, {
    "Content-Type": "application/json",
    "X-Request-ID": "e2e-request-id",
    ...headers,
  })
  response.end(JSON.stringify(value))
}

function readBody(request) {
  return new Promise(resolve => {
    const chunks = []
    request.on("data", chunk => chunks.push(chunk))
    request.on("end", () => resolve(Buffer.concat(chunks)))
  })
}

function roleFrom(request) {
  const match = /(?:^|;\s*)e2e-role=([^;]+)/.exec(request.headers.cookie || "")
  return ["admin", "asset_manager", "org_member"].includes(match?.[1])
    ? match[1]
    : null
}

function userFor(role) {
  const internalRole = role === "admin" || role === "asset_manager"
  return {
    id:
      role === "admin"
        ? adminId
        : role === "asset_manager"
          ? assetManagerId
          : memberId,
    email: `${role}@example.test`,
    display_name:
      role === "admin"
        ? "E2E Admin"
        : role === "asset_manager"
          ? "E2E Asset Manager"
          : "E2E Member",
    system_role: role,
    status: "active",
    must_change_password: false,
    organization_id: internalRole ? null : organizationId,
  }
}

function requireRole(request, response, allowedRoles) {
  const role = roleFrom(request)
  if (role === null) {
    sendJson(response, 401, { detail: "Authentication required" })
    return null
  }
  if (!allowedRoles.includes(role)) {
    sendJson(response, 403, { detail: "Forbidden" })
    return null
  }
  return role
}

function hasValidCsrf(request) {
  return ["e2e-csrf-token", "e2e-csrf-token-rotated"].includes(
    request.headers["x-csrf-token"]
  )
}

function requireCsrf(request, response) {
  if (hasValidCsrf(request)) return true
  sendJson(response, 403, { detail: "Invalid CSRF token" })
  return false
}

function parseJsonBody(body) {
  try {
    return JSON.parse(body.toString("utf8"))
  } catch {
    return null
  }
}

function parseMultipart(request, body) {
  const contentType = request.headers["content-type"] || ""
  const boundaryMatch = /boundary=(?:"([^"]+)"|([^;]+))/.exec(contentType)
  const boundary = boundaryMatch?.[1] || boundaryMatch?.[2]
  if (!boundary) return null

  const fields = {}
  const files = {}
  for (const part of body.toString("latin1").split(`--${boundary}`)) {
    const normalized = part.replace(/^\r\n/, "").replace(/\r\n$/, "")
    const separator = normalized.indexOf("\r\n\r\n")
    if (separator === -1) continue
    const headers = normalized.slice(0, separator)
    const content = normalized.slice(separator + 4).replace(/\r\n$/, "")
    const disposition = /content-disposition:\s*form-data;([^\r\n]+)/i.exec(
      headers
    )?.[1]
    const name = /name="([^"]+)"/.exec(disposition || "")?.[1]
    if (!name) continue
    const filename = /filename="([^"]*)"/.exec(disposition || "")?.[1]
    if (filename !== undefined) {
      files[name] = {
        filename,
        contentType:
          /content-type:\s*([^\r\n]+)/i.exec(headers)?.[1]?.trim() || null,
        size: Buffer.byteLength(content, "latin1"),
      }
    } else {
      fields[name] = content
    }
  }
  return { fields, files }
}

function recordRequest(request, url, role, facts) {
  state.requests.push({
    method: request.method,
    path: url.pathname,
    role,
    ...(facts ? { facts } : {}),
  })
}

function adminEpisode({
  id = episodeId,
  tradingDate = "2026-07-24",
  status = state.status,
} = {}) {
  return {
    id,
    trading_date: tradingDate,
    status,
    version: state.episodeVersion,
    metadata: state.metadata ?? [
      {
        locale: "zh-hant",
        title: "市場晨間簡報",
        summary: "測試用繁體中文摘要。",
      },
      {
        locale: "zh-hans",
        title: "市场晨间简报",
        summary: "测试用简体中文摘要。",
      },
      {
        locale: "en",
        title: "Market Morning Brief",
        summary: "An English summary for browser testing.",
      },
    ],
    metadata_source: state.metadataSource,
    audio_variants: [
      {
        asset_id: assetId,
        locale: "zh-hant",
        version: state.audioVersion,
        is_active: true,
        duration_seconds: 490,
        chapters: state.chapters,
        chapters_source: state.chaptersSource,
        analysis_status: state.analysisStatus,
        analysis_error: state.analysisError,
        analyzed_at: state.analyzedAt,
      },
    ],
    cover_asset_id: null,
    published_at:
      status === "published" ? `${tradingDate}T08:00:00+08:00` : null,
  }
}

function adminEpisodes() {
  if (state.podcastEpisodes !== "grouped") return [adminEpisode()]
  return [
    adminEpisode({
      id: "10000000-0000-4000-8000-000000000002",
      tradingDate: "2026-06-30",
    }),
    adminEpisode(),
    adminEpisode({
      id: "10000000-0000-4000-8000-000000000003",
      tradingDate: "2026-07-26",
    }),
    adminEpisode({
      id: "10000000-0000-4000-8000-000000000004",
      tradingDate: "2026-05-02",
    }),
  ]
}

function localizedEpisode(locale) {
  const english = locale === "en"
  return {
    id: episodeId,
    trading_date: "2026-07-24",
    title: english ? "Market Morning Brief" : "市場晨間簡報",
    summary: english
      ? "An English summary for browser testing."
      : "測試用繁體中文摘要。",
    locale,
    cover_asset_id: null,
    duration_seconds: 490,
    audio_created_at: "2026-07-24T07:30:00+08:00",
    chapters: [
      { start_seconds: 0, title: english ? "Fed decision" : "聯準會決議" },
      { start_seconds: 130, title: english ? "Foreign flows" : "外資動向" },
      { start_seconds: 285, title: english ? "Currency" : "匯率觀察" },
      { start_seconds: 410, title: english ? "Watch list" : "今日觀察清單" },
    ],
  }
}

// Six more published days for the "past episodes" column; ids are stable so
// tests can address them.
const pastEpisodeDates = [
  "2026-07-23",
  "2026-07-22",
  "2026-07-21",
  "2026-07-20",
  "2026-07-17",
  "2026-07-16",
]

function pastEpisodeId(index) {
  return `10000000-0000-4000-8000-0000000000${String(index + 11).padStart(2, "0")}`
}

function pastEpisodes(locale) {
  const english = locale === "en"
  return pastEpisodeDates.map((tradingDate, index) => ({
    id: pastEpisodeId(index),
    trading_date: tradingDate,
    title: english ? `Morning brief ${tradingDate}` : `晨間簡報 ${tradingDate}`,
    summary: english
      ? `Summary for ${tradingDate}.`
      : `${tradingDate} 的摘要。`,
    locale,
    cover_asset_id: null,
    duration_seconds: 400 + index * 30,
    audio_created_at: `${tradingDate}T07:30:00+08:00`,
    chapters: [],
  }))
}

const reportMarkets = ["global_macro_bonds", "crypto", "us_equity"]

function reportSummary(marketCode, locale) {
  return {
    publication_id: `${reportMarkets.indexOf(marketCode) + 7}0000000-0000-4000-8000-000000000001`,
    report_key: "daily-market",
    market_code: marketCode,
    edition_date: "2026-08-30",
    revision: 1,
    source_as_of: "2026-08-29",
    published_at: "2026-08-30T08:00:00+08:00",
    stale: false,
    stale_reason: null,
    status: "complete",
    title: marketCode,
    summary: `Official ${marketCode} report`,
    locale,
  }
}

function reportDetail(marketCode, locale) {
  const summary = reportSummary(marketCode, locale)
  const block = {
    id: "macro.commodities",
    kind: "metric",
    status: "ok",
    source_as_of: "2026-08-29",
    caveat: null,
    metrics: [
      { id: "wti", value: "68.4", change: "0.7", unit_code: "usd" },
      { id: "brent", value: "72.4", change: "0.8", unit_code: "usd" },
      { id: "gold", value: "2418", change: "0.3", unit_code: "usd" },
      { id: "silver", value: "28.4", change: "0.1", unit_code: "usd" },
      { id: "copper", value: "4.18", change: "-0.2", unit_code: "usd" },
    ],
  }
  const commodityPerformance = {
    id: "macro.commodity_ratios",
    kind: "series",
    status: "ok",
    source_as_of: "2026-08-29",
    caveat: null,
    unit_code: "ratio",
    series: [
      {
        id: "oil_gold_ratio",
        points: [
          { x: "2026-08-28", value: "0.029934" },
          { x: "2026-08-29", value: "0.029868" },
        ],
      },
      {
        id: "copper_gold_ratio",
        points: [
          { x: "2026-08-28", value: "0.001728" },
          { x: "2026-08-29", value: "0.001729" },
        ],
      },
    ],
  }
  return {
    ...summary,
    manifest_version: "three-market.v4",
    manifest_hash: "a".repeat(64),
    content: {
      schema_version: "three-market.v1",
      market_code: marketCode,
      as_of: "2026-08-29",
      status: "complete",
      caveat: null,
      blocks:
        marketCode === "global_macro_bonds"
          ? [block, commodityPerformance]
          : [block],
      metrics: [],
      charts: [],
    },
    presentation: {
      schema_version: "three-market.v1",
      locale,
      title: marketCode,
      summary: `Official ${marketCode} report`,
      labels: {
        "macro.commodities": {
          title: "Commodity snapshot",
          description: null,
          unit_label: null,
          series_labels: {},
        },
        "macro.commodity_ratios": {
          title: "Oil-Gold / Copper-Gold Ratios",
          description: null,
          unit_label: "Ratio",
          series_labels: {
            oil_gold_ratio: "Oil-Gold Ratio",
            copper_gold_ratio: "Copper-Gold Ratio",
          },
        },
      },
    },
  }
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url || "/", `http://127.0.0.1:${port}`)

  if (url.pathname === "/__e2e/health") {
    sendJson(response, 200, { ok: true })
    return
  }

  if (url.pathname === "/__e2e/reset" && request.method === "POST") {
    const raw = await readBody(request)
    reset(raw.length ? JSON.parse(raw.toString("utf8")) : {})
    sendJson(response, 200, { ok: true })
    return
  }

  if (url.pathname === "/__e2e/state") {
    sendJson(response, 200, state)
    return
  }

  if (
    state.sessionExpired &&
    url.pathname.startsWith("/api/") &&
    url.pathname !== "/api/auth/login"
  ) {
    recordRequest(request, url, roleFrom(request), { sessionExpired: true })
    sendJson(response, 401, { detail: "Session expired" })
    return
  }

  if (url.pathname === "/api/markets" && request.method === "GET") {
    const role = requireRole(request, response, ["org_member"])
    if (!role) return
    sendJson(
      response,
      200,
      [...reportMarkets, "forex", "tw_equity", "tw_index_derivatives"].map(
        code => ({
          code,
          is_visible: true,
          name_en: code,
          name_zh_hant: code,
          name_zh_hans: code,
        })
      )
    )
    return
  }
  if (
    url.pathname === "/api/analyst-viewpoints/today" &&
    request.method === "GET"
  ) {
    const role = requireRole(request, response, ["org_member"])
    if (!role) return
    sendJson(response, 200, [])
    return
  }

  if (url.pathname === "/api/reports" && request.method === "GET") {
    const role = requireRole(request, response, ["org_member"])
    if (!role) return
    const locale = url.searchParams.get("locale") || "zh-hant"
    recordRequest(request, url, role)
    sendJson(
      response,
      200,
      reportMarkets.map(market => reportSummary(market, locale))
    )
    return
  }

  if (
    url.pathname === "/api/reports/global_macro_bonds/dashboard" &&
    request.method === "GET"
  ) {
    const role = requireRole(request, response, ["org_member"])
    if (!role) return
    // Deterministic test-only histories; production always calls provider adapters.
    const assets = [
      ["brent", "USD/bbl", 72],
      ["wti", "USD/bbl", 69],
      ["gold", "USD/oz", 2648],
      ["silver", "USD/oz", 31],
      ["copper", "USD/lb", 4.28],
      ["dxy", "index", 101],
      ["eur_usd", "USD", 1.17],
      ["gbp_usd", "USD", 1.35],
      ["aud_usd", "USD", 0.67],
      ["nzd_usd", "USD", 0.59],
      ["usd_jpy", "JPY", 145],
      ["usd_chf", "CHF", 0.81],
      ["usd_cad", "CAD", 1.38],
      ["usd_twd", "TWD", 30.5],
      ["3m", "percent", 4.42],
      ["2y", "percent", 3.98],
      ["5y", "percent", 4.08],
      ["10y", "percent", 4.28],
      ["30y", "percent", 4.55],
      ["sofr", "percent", 4.58],
    ]
    sendJson(response, 200, {
      fetched_at: "2026-09-04T00:00:00Z",
      histories: assets.map(([id, unit, value], index) => ({
        id,
        symbol: id,
        unit,
        source: "E2E fixture",
        status: "ok",
        points: Array.from({ length: 400 }, (_, day) => ({
          date: new Date(Date.UTC(2026, 8, 4) - (399 - day) * 86400000)
            .toISOString()
            .slice(0, 10),
          value: (
            value *
            (1 + Math.sin(day / 12 + index) * 0.02 + day / 15000)
          ).toFixed(6),
        })),
      })),
      calendar: {
        date: "2026-09-04",
        status: "ok",
        events: [
          {
            date: "2026-09-04T12:30:00Z",
            country: "US",
            event: "Nonfarm payrolls",
            currency: "USD",
            impact: "High",
            estimate: "185000",
            previous: "272000",
            actual: null,
            unit: null,
          },
          {
            date: "2026-09-04T12:30:00Z",
            country: "US",
            event: "Unemployment rate",
            currency: "USD",
            impact: "High",
            estimate: "4.1",
            previous: "4.0",
            actual: null,
            unit: "%",
          },
        ],
      },
    })
    return
  }

  const reportMatch = /^\/api\/reports\/([^/]+)\/latest$/.exec(url.pathname)
  if (reportMatch && request.method === "GET") {
    const role = requireRole(request, response, ["org_member"])
    if (!role) return
    const marketCode = reportMatch[1]
    if (!reportMarkets.includes(marketCode)) {
      sendJson(response, 404, { detail: "report not found" })
      return
    }
    if (state.reports === "not_generated") {
      sendJson(response, 404, {
        detail: {
          code: "report_not_generated",
          message: "report has not been generated",
        },
      })
      return
    }
    const locale = url.searchParams.get("locale") || "zh-hant"
    recordRequest(request, url, role)
    sendJson(response, 200, reportDetail(marketCode, locale))
    return
  }

  if (url.pathname === "/api/auth/login" && request.method === "POST") {
    const input = parseJsonBody(await readBody(request))
    if (
      input === null ||
      typeof input.email !== "string" ||
      typeof input.password !== "string"
    ) {
      sendJson(response, 400, { detail: "Invalid login request" })
      return
    }
    const credential = credentials[input.email]
    if (!credential || credential.password !== input.password) {
      recordRequest(request, url, null, {
        email: input.email,
        credentialAccepted: false,
      })
      sendJson(response, 401, { detail: "Invalid credentials" })
      return
    }
    recordRequest(request, url, null, {
      email: input.email,
      credentialAccepted: true,
      authenticatedRole: credential.role,
    })
    sendJson(
      response,
      200,
      {
        user: userFor(credential.role),
        csrf_token: "e2e-csrf-token",
      },
      {
        "Set-Cookie": `e2e-role=${credential.role}; Path=/; HttpOnly; SameSite=Lax`,
      }
    )
    return
  }

  if (url.pathname === "/api/auth/logout" && request.method === "POST") {
    const role = requireRole(request, response, [
      "admin",
      "asset_manager",
      "org_member",
    ])
    if (!role || !requireCsrf(request, response)) return
    const csrfToken = request.headers["x-csrf-token"]
    recordRequest(request, url, role, {
      csrf: "valid",
      ...(csrfToken === "e2e-csrf-token-rotated" ? { csrfToken } : {}),
    })
    response.writeHead(204, {
      "X-Request-ID": "e2e-request-id",
      "Set-Cookie": "e2e-role=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
    })
    response.end()
    return
  }

  if (url.pathname === "/api/auth/me") {
    const role = requireRole(request, response, [
      "admin",
      "asset_manager",
      "org_member",
    ])
    if (!role) return
    recordRequest(request, url, role)
    sendJson(response, 200, userFor(role))
    return
  }

  if (url.pathname === "/api/auth/csrf" && request.method === "POST") {
    const role = requireRole(request, response, [
      "admin",
      "asset_manager",
      "org_member",
    ])
    if (!role) return
    recordRequest(request, url, role)
    sendJson(response, 200, { csrf_token: "e2e-csrf-token" })
    return
  }

  if (
    url.pathname === "/api/auth/change-password" &&
    request.method === "POST"
  ) {
    const role = requireRole(request, response, [
      "admin",
      "asset_manager",
      "org_member",
    ])
    if (!role || !requireCsrf(request, response)) return
    const input = parseJsonBody(await readBody(request))
    recordRequest(request, url, role, {
      csrf: "valid",
      currentPassword: input?.current_password,
      newPassword: input?.new_password,
    })
    if (
      state.passwordChange === "error" ||
      input?.current_password !==
        (role === "admin"
          ? "admin-password"
          : role === "asset_manager"
            ? "asset-manager-password"
            : "customer-password")
    ) {
      sendJson(response, 400, { detail: "Current password is incorrect" })
      return
    }
    sendJson(response, 200, {
      user: userFor(role),
      csrf_token: "e2e-csrf-token-rotated",
    })
    return
  }

  if (url.pathname === "/api/admin/podcasts" && request.method === "GET") {
    const role = requireRole(request, response, ["admin", "asset_manager"])
    if (!role) return
    recordRequest(request, url, role)
    sendJson(response, 200, adminEpisodes())
    return
  }

  if (
    url.pathname === `/api/admin/podcasts/${episodeId}/unpublish` &&
    request.method === "POST"
  ) {
    const role = requireRole(request, response, ["admin", "asset_manager"])
    if (!role || !requireCsrf(request, response)) return
    const input = parseJsonBody(await readBody(request))
    if (
      input === null ||
      !Number.isInteger(input.expected_version) ||
      input.expected_version !== state.episodeVersion
    ) {
      sendJson(response, 409, { detail: "Expected version mismatch" })
      return
    }
    recordRequest(request, url, role, {
      csrf: "valid",
      expectedVersion: input.expected_version,
    })
    state.status = "draft"
    state.episodeVersion += 1
    sendJson(response, 200, adminEpisode())
    return
  }

  const chaptersMatch = new RegExp(
    `^/api/admin/podcasts/${episodeId}/audio/([^/]+)/chapters$`
  ).exec(url.pathname)
  if (chaptersMatch && request.method === "PUT") {
    const role = requireRole(request, response, ["admin", "asset_manager"])
    if (!role || !requireCsrf(request, response)) return
    const input = parseJsonBody(await readBody(request))
    if (input === null || !Array.isArray(input.chapters)) {
      sendJson(response, 422, { detail: "Invalid chapters" })
      return
    }
    if (input.expected_version !== state.episodeVersion) {
      sendJson(response, 409, { detail: "Expected version mismatch" })
      return
    }
    recordRequest(request, url, role, {
      csrf: "valid",
      locale: chaptersMatch[1],
      expectedVersion: input.expected_version,
      chapters: input.chapters,
      reason: input.reason,
    })
    state.chapters = input.chapters
    state.chaptersSource = input.chapters.length > 0 ? "manual" : "none"
    state.episodeVersion += 1
    sendJson(response, 200, adminEpisode())
    return
  }

  const analyzeMatch = new RegExp(
    `^/api/admin/podcasts/${episodeId}/audio/([^/]+)/analyze$`
  ).exec(url.pathname)
  if (analyzeMatch && request.method === "POST") {
    const role = requireRole(request, response, ["admin", "asset_manager"])
    if (!role || !requireCsrf(request, response)) return
    recordRequest(request, url, role, {
      csrf: "valid",
      locale: analyzeMatch[1],
    })
    if (state.analysis === "disabled") {
      sendJson(response, 409, { detail: { code: "podcast_analysis_disabled" } })
      return
    }
    state.analysisStatus = "pending"
    state.analysisError = null
    // The real analysis runs in the background; finish it shortly after.
    setTimeout(() => {
      if (state.analysisStatus !== "pending") return
      state.analysisStatus = "succeeded"
      state.analyzedAt = "2026-07-24T08:05:00+08:00"
      if (state.chaptersSource !== "manual") {
        state.chapters = [
          { start_seconds: 0, title: "AI 開場" },
          { start_seconds: 120, title: "AI 外資" },
          { start_seconds: 300, title: "AI 清單" },
        ]
        state.chaptersSource = "ai"
      }
      if (state.metadataSource !== "manual") {
        state.metadata = [
          { locale: "zh-hant", title: "AI 標題", summary: "AI 摘要" },
          { locale: "zh-hans", title: "AI 标题", summary: "AI 摘要" },
          { locale: "en", title: "AI title", summary: "AI summary" },
        ]
        state.metadataSource = "ai"
      }
    }, 400)
    sendJson(response, 202, adminEpisode())
    return
  }

  if (
    url.pathname === `/api/admin/podcasts/${episodeId}` &&
    request.method === "PUT"
  ) {
    const role = requireRole(request, response, ["admin"])
    if (!role || !requireCsrf(request, response)) return
    const input = parseJsonBody(await readBody(request))
    if (input === null || !Array.isArray(input.metadata?.values)) {
      sendJson(response, 422, { detail: "Invalid metadata" })
      return
    }
    if (input.expected_version !== state.episodeVersion) {
      sendJson(response, 409, { detail: "Expected version mismatch" })
      return
    }
    recordRequest(request, url, role, {
      csrf: "valid",
      expectedVersion: input.expected_version,
      metadata: input.metadata.values,
      reason: input.reason,
    })
    state.metadata = input.metadata.values
    state.metadataSource = "manual"
    state.episodeVersion += 1
    sendJson(response, 200, adminEpisode())
    return
  }

  if (
    url.pathname === `/api/admin/podcasts/${episodeId}/publish` &&
    request.method === "POST"
  ) {
    const role = requireRole(request, response, ["admin", "asset_manager"])
    if (!role || !requireCsrf(request, response)) return
    const input = parseJsonBody(await readBody(request))
    if (
      input === null ||
      !Number.isInteger(input.expected_version) ||
      input.expected_version !== state.episodeVersion
    ) {
      sendJson(response, 409, { detail: "Expected version mismatch" })
      return
    }
    recordRequest(request, url, role, {
      csrf: "valid",
      expectedVersion: input.expected_version,
    })
    state.status = "published"
    state.episodeVersion += 1
    sendJson(response, 200, adminEpisode())
    return
  }

  if (
    url.pathname === "/api/admin/podcasts/uploads" &&
    request.method === "POST"
  ) {
    const role = requireRole(request, response, ["admin", "asset_manager"])
    if (!role || !requireCsrf(request, response)) return
    const multipart = parseMultipart(request, await readBody(request))
    if (!multipart) {
      sendJson(response, 400, { detail: "Multipart body required" })
      return
    }
    const { fields, files } = multipart
    const confirmed =
      fields.confirm_replacement === "true"
        ? true
        : fields.confirm_replacement === "false"
          ? false
          : null
    let expectedVersions
    try {
      expectedVersions = JSON.parse(fields.expected_versions)
    } catch {
      expectedVersions = null
    }
    const validDate = /^\d{4}-\d{2}-\d{2}$/.test(fields.trading_date || "")
    const validReason = ["initial_upload", "update_file", "other"].includes(
      fields.reason
    )
    const zhHantFile = files.zh_hant
    const validFile =
      zhHantFile &&
      zhHantFile.filename === "briefing.mp3" &&
      zhHantFile.contentType === "audio/mpeg" &&
      zhHantFile.size > 0
    const validExpectedVersions =
      expectedVersions !== null &&
      typeof expectedVersions === "object" &&
      !Array.isArray(expectedVersions)
    if (
      !validDate ||
      !validReason ||
      confirmed === null ||
      !validExpectedVersions ||
      !validFile
    ) {
      sendJson(response, 422, { detail: "Invalid Podcast upload contract" })
      return
    }
    const facts = {
      csrf: "valid",
      tradingDate: fields.trading_date,
      reason: fields.reason,
      confirmReplacement: confirmed,
      expectedVersions,
      files,
    }
    recordRequest(request, url, role, facts)
    if (!confirmed) {
      sendJson(response, 409, {
        detail: {
          code: "replacement_confirmation_required",
          current_versions: { "zh-hant": state.audioVersion },
        },
      })
      return
    }
    if (expectedVersions["zh-hant"] !== state.audioVersion) {
      sendJson(response, 409, { detail: "Expected audio version mismatch" })
      return
    }
    state.status = "published"
    state.audioVersion += 1
    state.episodeVersion += 1
    sendJson(response, 200, adminEpisode())
    return
  }

  if (url.pathname === "/api/podcasts" && request.method === "GET") {
    const role = requireRole(request, response, [
      "admin",
      "asset_manager",
      "org_member",
    ])
    if (!role) return
    recordRequest(request, url, role, {
      locale: url.searchParams.get("locale"),
    })
    if (state.podcastList === "error") {
      sendJson(response, 503, { detail: "Podcast service unavailable" })
      return
    }
    const locale = url.searchParams.get("locale") || "zh-hant"
    sendJson(
      response,
      200,
      state.podcastList === "empty" || state.status !== "published"
        ? []
        : state.podcastList === "multiple"
          ? [localizedEpisode(locale), ...pastEpisodes(locale)]
          : [localizedEpisode(locale)]
    )
    return
  }

  if (
    url.pathname === `/api/podcasts/${episodeId}` &&
    request.method === "GET"
  ) {
    const role = requireRole(request, response, [
      "admin",
      "asset_manager",
      "org_member",
    ])
    if (!role) return
    recordRequest(request, url, role, {
      locale: url.searchParams.get("locale"),
    })
    const locale = url.searchParams.get("locale") || "zh-hant"
    sendJson(response, 200, {
      ...localizedEpisode(locale),
      published_at: "2026-07-24T08:00:00+08:00",
    })
    return
  }

  const audioUrlMatch = /^\/api\/podcasts\/([^/]+)\/audio-url$/.exec(
    url.pathname
  )
  if (audioUrlMatch && request.method === "POST") {
    const role = requireRole(request, response, [
      "admin",
      "asset_manager",
      "org_member",
    ])
    if (!role) return
    recordRequest(request, url, role, {
      locale: url.searchParams.get("locale"),
    })
    if (state.audio === "delayed") {
      await new Promise(resolve => setTimeout(resolve, 700))
    }
    if (state.audio === "error") {
      sendJson(response, 503, { detail: "Audio unavailable" })
      return
    }
    sendJson(response, 200, {
      episode_id: audioUrlMatch[1],
      requested_locale: url.searchParams.get("locale") || "zh-hant",
      resolved_locale: "zh-hant",
      asset_id: assetId,
      url: `http://127.0.0.1:${port}/media/podcast.wav`,
      expires_in_seconds: 300,
    })
    return
  }

  if (
    /^\/api\/podcasts\/[^/]+$/.test(url.pathname) &&
    request.method === "GET"
  ) {
    const role = requireRole(request, response, [
      "admin",
      "asset_manager",
      "org_member",
    ])
    if (!role) return
    recordRequest(request, url, role)
    sendJson(response, 404, { detail: "Podcast episode not found" })
    return
  }

  if (url.pathname === "/media/podcast.wav") {
    const wav = Buffer.from(
      "UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=",
      "base64"
    )
    response.writeHead(200, {
      "Content-Type": "audio/wav",
      "Content-Length": wav.length,
      "Accept-Ranges": "bytes",
    })
    response.end(wav)
    return
  }

  sendJson(response, 404, { detail: "Not found" })
})

server.listen(port, "127.0.0.1", () => {
  process.stdout.write(`Mock API listening on http://127.0.0.1:${port}\n`)
})

function shutdown() {
  server.close(() => process.exit(0))
}

process.on("SIGINT", shutdown)
process.on("SIGTERM", shutdown)
