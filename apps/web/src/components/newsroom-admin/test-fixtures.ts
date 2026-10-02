import type {
  NewsroomEdition,
  NewsroomEditionDay,
  NewsroomEditionDetail,
  NewsroomEvent,
  NewsroomItem,
  NewsroomSource,
} from "#/lib/newsroom-admin"

// Builders for newsroom admin responses shared by the component tests.

export const DATE = "2026-10-01"
export const EDITION_ID = "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09"

export function event(overrides: Partial<NewsroomEvent> = {}): NewsroomEvent {
  return {
    id: "0f9b6a6e-3d7f-4f4f-9a3f-2b7d3f1c9e11",
    edition_date: DATE,
    working_title: "Fed decision",
    status: "open",
    merged_into_id: null,
    created_by: "triage",
    headline_zh_hant: "聯準會維持利率不變",
    summary_zh_hant: "聯準會宣布維持政策利率。",
    headline_zh_hans: null,
    summary_zh_hans: null,
    headline_en: null,
    summary_en: null,
    related_symbols: [{ symbol: "^GSPC", kind: "index", label: "S&P 500" }],
    analysis_status: "ready",
    analysis_error_code: null,
    analyzed_at: "2026-10-01T00:10:00+00:00",
    en_status: "idle",
    en_error_code: null,
    edited_at: null,
    article_count: 3,
    source_count: 2,
    body_ok_count: 2,
    articles: [
      {
        id: "a1000000-0000-4000-8000-000000000001",
        source_name: "Reuters",
        hostname: "reuters.example",
        title: "Fed holds rates",
        url: "https://reuters.example/fed",
        published_at: null,
        body_status: "ok",
      },
    ],
    ...overrides,
  }
}

export function item(overrides: Partial<NewsroomItem> = {}): NewsroomItem {
  return {
    id: "1b000000-0000-4000-8000-000000000001",
    edition_id: EDITION_ID,
    rank: 1,
    stars: 5,
    editor_score: 80,
    origin: "model",
    why_zh_hant: "利率路徑牽動全球資金。",
    why_zh_hans: null,
    why_en: null,
    why_status: "ready",
    why_error_code: null,
    why_en_status: "idle",
    removed_at: null,
    hidden_at: null,
    abandoned_at: null,
    event: event(),
    ...overrides,
  }
}

export function edition(
  overrides: Partial<NewsroomEdition> = {}
): NewsroomEdition {
  return {
    id: EDITION_ID,
    edition_date: DATE,
    market_code: "global",
    status: "draft",
    selection_mode: "editor",
    auto_publish_at: "2026-10-01T01:00:00+00:00",
    late_fill_deadline: "2026-10-01T04:00:00+00:00",
    assembled_at: "2026-10-01T00:00:00+00:00",
    published_at: null,
    published_by_user_id: null,
    ignored_pending_triage: 0,
    late_fill_closed_at: null,
    ...overrides,
  }
}

const emptyCounts = {
  active: 0,
  removed: 0,
  hidden: 0,
  abandoned: 0,
  ready: 0,
  analysis_failed: 0,
  needs_body: 0,
}

export function day(
  globalEdition: NewsroomEdition | null = edition(),
  overrides: Partial<NewsroomEditionDay> = {}
): NewsroomEditionDay {
  return {
    edition_date: DATE,
    is_today: true,
    untriaged_articles: 4,
    triage_failed_articles: 0,
    markets: [
      {
        market_code: "global",
        edition: globalEdition,
        counts: { ...emptyCounts, active: 2 },
      },
      { market_code: "tw_equity", edition: null, counts: emptyCounts },
      { market_code: "us_equity", edition: null, counts: emptyCounts },
    ],
    ...overrides,
  }
}

export function detail(
  overrides: Partial<NewsroomEditionDetail> = {}
): NewsroomEditionDetail {
  return {
    edition_date: DATE,
    market_code: "global",
    edition: edition(),
    items: [],
    candidates: [],
    ...overrides,
  }
}

export function source(
  overrides: Partial<NewsroomSource> = {}
): NewsroomSource {
  return {
    id: "50000000-0000-4000-8000-0000000000a1",
    key: "reuters",
    name: "Reuters",
    kind: "rss",
    url: "https://reuters.example/feed",
    hostname: "reuters.example",
    link_pattern: null,
    markets: ["global", "us_equity"],
    language_filter: ["en"],
    full_text_in_feed: false,
    trust_tier: 3,
    weight: 1.5,
    poll_interval_minutes: 30,
    enabled: true,
    last_polled_at: null,
    last_success_at: "2026-10-01T00:00:00+00:00",
    next_poll_at: null,
    consecutive_failures: 0,
    last_error_code: null,
    last_error_at: null,
    articles_7d: 42,
    health: "healthy",
    ...overrides,
  }
}
