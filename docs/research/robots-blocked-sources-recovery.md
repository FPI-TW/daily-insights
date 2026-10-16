# robots 擋住的媒體：可用端點（2026-10-15）

後台曾出現 `robots_disallowed` 的來源，在 `DAILY_INSIGHTS_NEWS_IGNORE_ROBOTS=true`
（預設）下可改用下列端點。SSRF 白名單仍依來源的 feed host + `hostname` 運作。

| 來源               | 建議 kind      | 建議 URL                                                     | hostname              | 備註                                                                                                                                                                                         |
| ------------------ | -------------- | ------------------------------------------------------------ | --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| MarketWatch        | `rss`          | `https://feeds.content.dowjones.io/public/rss/mw_topstories` | `www.marketwatch.com` | 另有 `mw_realtimeheadlines`、`mw_bulletins`。文章頁被 DataDome 擋（401），全文多半 `unavailable`；可用 feed 標題／摘要初篩。Dow Jones 聲明禁止未經授權的自動化蒐集，上線前自行評估合規風險。 |
| Yahoo Finance      | `news_sitemap` | `https://finance.yahoo.com/news-sitemap.xml`                 | `finance.yahoo.com`   | 也可用 `https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC&region=US&lang=en-US`（`rss`），但該 host 的 robots 全站 Disallow，且項目常連到第三方站。                                 |
| Seeking Alpha      | `rss`          | `https://seekingalpha.com/market_currents.xml`               | `seekingalpha.com`    | `feed.xml` 亦可。略過 robots 後 HTTP 可直接取。                                                                                                                                              |
| TechNews／財經新報 | `rss`          | `https://cdn.technews.tw/feed/`                              | `finance.technews.tw` | seed 已有；robots 本來就允許，若仍失敗請查 URL／白名單。                                                                                                                                     |
| The Motley Fool    | `news_sitemap` | `https://www.fool.com/news-sitemap.xml`                      | `www.fool.com`        | `/feeds` 路徑被 robots Disallow 且要 API key；sitemap 與文章頁以 bot UA 可取。                                                                                                               |
| MoneyDJ            | `html_list`    | `https://www.moneydj.com/KMDJ/News/NewsRealList.aspx?a=MB00` | `www.moneydj.com`     | `options`: `{"href_contains":"NewsViewer.aspx","keep_query":true}`；`link_pattern`: `(?i)^https://www\.moneydj\.com/kmdj/news/newsviewer\.aspx\?a=[0-9a-f-]+`。文章頁可取。                  |

## CloakBrowser 實測

- MoneyDJ、Fool、Yahoo 列表／文章：一般 HTTP（或 headed browser）即可，不需 anti-detect。
- MarketWatch 文章頁：CloakBrowser（headless／humanize／headed）仍回 DataDome 401；RSS 路徑才穩。

## 後台／部署

migration `20261015_0044` 會：

1. 依上表插入標準 key（若不存在）：`marketwatch-topstories`、`yahoo-finance-news`、`seeking-alpha-currents`、`motley-fool-news`。
2. 把名稱剛好是截圖英文標籤（MarketWatch、Yahoo Finance、Seeking Alpha、The Motley Fool、TechNews、MoneyDJ）的既有列改成上表端點。
3. 校正 `technews-finance`、`moneydj-mb00`，並清掉輪詢錯誤、把 `next_poll_at` 設為 NULL 以便立刻重試。

部署後確認 `DAILY_INSIGHTS_NEWS_IGNORE_ROBOTS=true`（預設）。若同時留下手動列與標準 key，可能暫時有重複來源，可在後台刪除多餘的那一筆。

手動微調時：`html_list` 的進階選項需含 `href_contains`；MarketWatch 的 feed host `feeds.content.dowjones.io` 會由來源 URL 自動進白名單。
