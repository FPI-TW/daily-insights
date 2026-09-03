# 新聞來源擴充實作規格

> 給實作者（Claude Code）的完整交辦文件。撰寫日期 2026-09-03。
> 所有列出的 endpoint 都經過實測，並在下文標註實測佐證。

## 0. 這份規格要解決什麼

`apps/api/src/daily_insights_api/modules/news` 目前的新聞發現層有兩條路徑：GDELT
（`sources.py::discover_candidates`）與 feed 註冊表（`feeds.py::FEED_SOURCES`）。
本規格要**移除 GDELT，改為單一的 feed 註冊表路徑，並把註冊表大幅擴充**。

- **GDELT 判定為不穩定來源，整條路徑移除**（Phase 0）。它有約 15 分鐘延遲、
  官方文件未載明任何 rate limit、且 `feeds.py` 的模組註解本身就寫著
  「GDELT's HTTPS endpoint is slow and regularly unreachable」——
  feed 註冊表當初就是為了補它的不穩定才加的。
- `FEED_SOURCES` 只有 7 個來源，其中 **CNBC 與 BBC 實測已無法取得**（見 §5）。
- 沒有任何來源新鮮度監控。實測發現大量 feed 回 HTTP 200、XML 合法，但內容停在數個月至數年前。

移除 GDELT 之後，發現層只剩一個機制，**沒有後備**。因此 Phase 1 的新鮮度監控
從「有比較好」變成「必須有」，且選稿前要有候選數量下限檢查（見 Phase 1）。

## 1. 不要做的事（重要）

這份規格是**擴充**，不是重寫。以下既有設計是刻意的，保持不動：

- `NewsItem` 不儲存文章正文（`models.py` 已有註解說明）。新增來源不得改變這點。
  feed 自帶的全文只在記憶體中傳給 LLM，落庫的仍只有 metadata 與 digest。
- `discover_feed_candidates` 的「每個 feed 失敗互相隔離、不讓單一發佈者拖垮整份 edition」
  的錯誤處理模型。
- `Candidate` / `SelectedCandidate` / `Selection` 契約（`contracts.py`）。
- `EditionSpec` / `SelectionPolicy` 的分版設計（`editions.py`）。
- SSRF 防護（`sources.py::_AllowlistedNetworkBackend`）與 `robots_allowed` 檢查。
- **`sources.py` 裡除了 GDELT 以外的所有東西。** 這個檔案的名字取壞了 —— 它同時裝著
  GDELT 發現層**與**整套文章萃取機制，而 `feeds.py`、`llm.py`、`service.py`
  三個檔案都依賴後者。移除 GDELT 時**絕對不能整檔刪除**，詳見 Phase 0 的保留清單。
- `MAX_FEED_BYTES` 的位元組上限與串流讀取。

不要為了擴充來源而放寬 lint、型別或測試（見 `AGENTS.md`）。

## 2. 工作分期

建議分成四個 PR，每個獨立可驗收。

### Phase 0 — 移除 GDELT（先做，理由見下）

放在最前面是因為 `service.py::DERIVATION_VERSION` 目前的值是 `"gdelt-deepseek-news.v3"`，
移除 GDELT 必須改這個常數，而**改動它會改變 `input_digest`，讓既有 edition 產生新的 revision**。
先做這件事，整個專案只需要 bump 一次版本；留到最後做就會 bump 兩次。

新的值建議 `"feeds-deepseek-news.v4"`。

**要刪除的（僅這些）**

`modules/news/sources.py`：

- `GDELT_DOC_URL` 常數
- `discover_candidates()` 整個函式
- `_seen_at()`（只被 GDELT 的 `seendate` 解析使用，格式 `%Y%m%d%H%M%S`；
  刪除前請確認沒有其他呼叫者）
- 模組 docstring 中的 GDELT 描述

`modules/news/editions.py`：

- `EditionSpec.uses_gdelt` 欄位，以及三個 spec 中的 `uses_gdelt=` 引數

`modules/news/service.py`：

- `discover()` 閉包、`audit_discovery_failure()`、`if spec.uses_gdelt:` 分支
- 對 `discover_candidates` 的 import
- 已無用的事件：`news.candidates.attempt_failed`、`news.candidates.failed`
- `news.candidates.discovered` 保留，但改為計算 feed candidates 的數量
- `_retry()` 若移除後不再有其他呼叫者，一併刪除；**先 grep 確認**

`modules/news/feeds.py`：

- 模組 docstring 中「GDELT's HTTPS endpoint is slow and regularly unreachable, so
  discovery also reads...」的描述，改為說明這是唯一的發現路徑

`core/config.py`：

- `news_discovery_timeout_seconds` 的預設值 60 秒是為 GDELT 設的
  （註解寫「GDELT's HTTPS front end regularly needs 20-30 seconds to answer」）。
  改為 **20–30 秒**並更新註解。同步檢查 `.env.example`。

**必須保留的（`sources.py` 內，全部有外部依賴）**

| 保留項目                                                                                                                                     | 誰在用                                 |
| -------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| `SOURCE_NAMES`                                                                                                                               | `feeds.py::_candidate`                 |
| `_dedupe_candidates`                                                                                                                         | `feeds.py`、`service.py`               |
| `allowed_hostname`、`robots_allowed`                                                                                                         | `feeds.py::discover_feed_candidates`   |
| `configured_hostnames`                                                                                                                       | `service.py::run_news_edition`         |
| `FetchedCandidate`                                                                                                                           | `llm.py`、`service.py`                 |
| `fetch_article`、`safe_article_client`                                                                                                       | `service.py::_fetch_usable_candidates` |
| `_AllowlistedNetworkBackend`、`_SafeArticleTransport`、`_ArticleTextExtractor`、`assert_public_hostname`、`validate_https_url`、`_title_key` | 上述函式的內部依賴                     |

**順手做的兩件事**

1. `sources.py` 移除 GDELT 後，內容只剩「SSRF-safe 文章萃取」。
   建議**改名為 `extraction.py`** 並更新三個 import 端，讓檔名對得上內容。
   若擔心 diff 過大，可留到獨立 PR，但不要留著誤導性的檔名太久。
2. `SOURCE_NAMES` 目前只有 GDELT 時代的 6 個 hostname，而 Phase 3 會加入約 45 個來源。
   建議把顯示名稱移到 `FeedSource` 上新增的 `display_name: str` 欄位，
   `_candidate` 改讀 `source.display_name`，然後刪除 `SOURCE_NAMES`。
   這樣新增來源時只需要改一個地方。

**驗收**

- 全專案 grep `gdelt`（不分大小寫）應無殘留，`DERIVATION_VERSION` 已更新。
- `make check` 通過。
- 既有的 edition 生成流程在只有 feed 來源的情況下仍能產出 `complete` 狀態。

### Phase 1 — 新鮮度監控與候選下限（會揭露 Phase 3 的問題）

在 `FeedSource` 加上新欄位：

```python
@dataclass(frozen=True)
class FeedSource:
    ...
    max_age_hours: int = 24      # 最新一則超過此值即視為失效
    provides_full_text: bool = False
```

在 `discover_feed_candidates` 中，取得 candidates 後檢查最新的 `seen_at`：

- 若該 feed 所有 candidate 的 `seen_at` 皆為 `None`（例如 listing 與部分 sitemap），略過檢查。
- 若最新 `seen_at` 距 `now` 超過 `max_age_hours`，`emit_event("news.feed.stale", hostname=..., age_hours=...)`
  並**仍然回傳** candidates（由選稿階段決定要不要用），不要直接丟棄。
- 新增 `news.feed.ok` 事件帶 `count` 與 `newest_age_minutes`，讓維運能看出哪個 feed 正在退化。

另外新增**候選數量下限檢查**。移除 GDELT 後發現層沒有後備，若註冊表整批退化
（例如共用的 feedburner 掛掉），現行流程會安靜地產出一份很薄的 edition。
在選稿前檢查去重後的 candidate 數，低於 `spec.target_items * 2` 時
`emit_event("news.candidates.below_floor", market=..., count=..., floor=...)`，
並讓 edition 狀態走既有的 `partial` / `unavailable` 判定。

驗收：對一個固定的舊 payload fixture，`parse_rss` 後應觸發 `news.feed.stale`；
候選數不足時應觸發 `news.candidates.below_floor`。

### Phase 2 — 新增 adapter 種類

`FeedSource.kind` 目前是 `"rss" | "cnyes_json" | "listing"`。新增三種：

| kind           | 說明                                             | 解析目標                                                |
| -------------- | ------------------------------------------------ | ------------------------------------------------------- |
| `news_sitemap` | Google News sitemap XML                          | `<url><loc>`、`<news:title>`、`<news:publication_date>` |
| `json_list`    | 通用 JSON 列表，用 JSONPath 式設定指定欄位       | 見下                                                    |
| `rss_full`     | 與 `rss` 相同，但另取 `content:encoded` 作為全文 | 同 `rss` 加 body                                        |

`json_list` 需要在 `FeedSource` 上帶欄位映射設定，建議新增：

```python
@dataclass(frozen=True)
class JsonListMapping:
    items_path: tuple[str, ...]      # 例如 ("data", "roll_data")
    id_field: str | None             # 有些來源用 id 組 URL
    url_field: str | None            # 直接給 URL 的來源用這個
    url_template: str | None         # 需要用 id 組 URL 時用這個
    title_field: str
    time_field: str
    time_format: str                 # "unix_s" | "unix_ms" | "iso" | "datetime_str"
    body_field: str | None = None    # 快訊全文欄位，有的話設 provides_full_text=True
```

欄位名一律支援 **dot-notation** 以存取巢狀結構（例如 Guardian 的 `fields.bodyText`），
`items_path` 同樣可指向巢狀陣列（例如 `("response", "results")`）。

需要金鑰的來源（目前只有 Guardian）另外在 `FeedSource` 上加：

```python
    api_key_setting: str | None = None   # Settings 上的屬性名，例如 "guardian_api_key"
    api_key_param: str = "api-key"       # 金鑰要放進哪個 query 參數
```

金鑰**不得寫死在 `FEED_SOURCES`**，由 `discover_feed_candidates` 在組 URL 時
從 `get_settings()` 取值注入；設定缺漏時該來源直接略過並 `emit_event("news.feed.skipped",
reason="missing_credential")`，不得讓整份 edition 失敗。

`news_sitemap` 的 XML 命名空間是 `http://www.google.com/schemas/sitemap-news/0.9`，
解析時要處理命名空間前綴。沿用既有的 `defusedxml` 解析路徑，不要換 parser。

驗收：每個新 kind 至少一組 fixture 測試，涵蓋正常解析、欄位缺漏、時間格式異常。

### Phase 3 — feed 註冊表擴充

按 §4 的清單填入 `FEED_SOURCES`。注意：

- `news_allowed_hostnames` 設定值會變得很長。建議改為**從 `FEED_SOURCES` 推導**
  預設允許清單，設定值只保留「額外允許」與「強制排除」兩個覆寫用途。
  若改動 `core/config.py`，記得同步 `.env.example` 與 production 驗證邏輯。
- 每個來源都要填 `link_pattern`，維持既有的 URL 白名單約束。
- `provides_full_text=True` 的來源，`service.py::_fetch_usable_candidates`
  應跳過 `fetch_article`，直接用 feed 帶的 body 組 `FetchedCandidate`
  （`content_digest` 一樣用 sha256(body)）。這能大幅降低 article fetch 的失敗率與延遲。

### Phase 4 — 新增 edition（可選，視產品排程決定）

若要新增 `hk_equity` / `cn_equity` edition，需要：

1. `editions.py` 新增 `EditionSpec` 與 `market_focus` 文字。
2. **Alembic migration**：`news_editions.market_code` 的 CHECK constraint
   目前是 `market_code IN ('global','tw_equity','us_equity')`，必須修改。
3. `reports` 與 web 端的 market 對應同步。
4. §4 的註冊表中，港中來源的 `markets` 欄位加上對應的 market code。

## 3. 輪詢頻率分組

新增 `FeedSource.poll_group`，值域 `"flash" | "fast" | "normal"`，供排程器決定
呼叫頻率。edition 生成時一律讀全部，但若之後要做常駐 poller，這個欄位是依據。

| group    | 建議間隔   | 用於                                 |
| -------- | ---------- | ------------------------------------ |
| `flash`  | 1–2 分鐘   | 中文快訊 API、GlobeNewswire Earnings |
| `fast`   | 10–15 分鐘 | 台股、港股主力媒體                   |
| `normal` | 30 分鐘    | 日韓、英文綜合、新聞稿               |

## 4. Feed 註冊表（實測清單）

實測時間 2026-09-03 上午（台北）。「佐證」欄是當下讀到的最新一則，用來確認
實作後解析結果合理；不是要求實作時比對這個值。

### 4.1 中文快訊 API — `poll_group="flash"`，全部 `provides_full_text` 視欄位而定

| hostname                | url                                                                                          | kind        | full text | 佐證                                           |
| ----------------------- | -------------------------------------------------------------------------------------------- | ----------- | --------- | ---------------------------------------------- |
| `m.cls.cn`              | `https://m.cls.cn/nodeapi/telegraphs?app=CailianpressWap&os=web&sv=1&rn=30`                  | `json_list` | ✅        | errno=0，ctime 1788403311 = 09-03 10:41:51     |
| `www.jin10.com`         | `https://www.jin10.com/flash_newest.js`                                                      | `json_list` | ✅        | 09-03 07:38:52，純 GET 不需 header             |
| `api-one.wallstcn.com`  | `https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&client=pc&limit=30` | `json_list` | ✅        | code 20000，display_time 1788402711 = 10:31:51 |
| `newsapi.eastmoney.com` | `https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html?r={rand}`        | `json_list` | ❌        | showtime 09-03 10:39:09                        |
| `feed.mix.sina.com.cn`  | `https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num=30&page=1`                | `json_list` | ❌        | 回應 timestamp Thu Sep 03 10:41:49 +0800 2026  |
| `cache.thepaper.cn`     | `https://cache.thepaper.cn/contentapi/wwwIndex/rightSidebar`                                 | `json_list` | ❌        | resultCode=1，trackPublishTime = 10:41:26      |
| `feedx.net`             | `https://feedx.net/rss/jiemian.xml`                                                          | `rss_full`  | ✅        | 09-03 10:22（界面新聞第三方全文 feed）         |

**實作陷阱：**

- **東方財富必須帶 cache-buster `?r={隨機數}`**。不帶會靜默取得 CDN 舊快取，
  實測拿到 2026-08-05 的資料，回傳結構完全正常不會報錯。實作時每次請求都要換 `r`。
- 財聯社 **PC 站** `www.cls.cn/nodeapi/telegraphList` 需要 `sign` 簽名參數，缺了一律 404。
  上表的 **m 站** 端點不需要簽名，用這個。
- 金十的 `flash-api.jin10.com/get_flash_list` 需要 `x-app-id` / `x-version` header；
  上表的 `flash_newest.js` 不需要，優先用它。
- `flash_newest.js` 回傳的是 JS 賦值語句不是純 JSON，需要先剝掉變數宣告前綴再 parse。

### 4.2 台灣 — `poll_group="fast"`，`markets` 含 `tw_equity`

| hostname                   | url                                                                  | kind           | full text | 佐證                                           |
| -------------------------- | -------------------------------------------------------------------- | -------------- | --------- | ---------------------------------------------- |
| `news.cnyes.com`           | `https://news.cnyes.com/rss/v1/news/category/tw_stock`               | `rss_full`     | ✅        | 09-03 10:20:06，30 則                          |
| `news.cnyes.com`           | `https://news.cnyes.com/rss/v1/news/category/headline`               | `rss_full`     | ✅        | 40 則                                          |
| `news.cnyes.com`           | `https://news.cnyes.com/rss/v1/news/category/wd_stock`               | `rss_full`     | ✅        | 國際股市                                       |
| `money.udn.com`            | `https://money.udn.com/rssfeed/news/1001/5590?ch=money`              | `rss`          | ❌        | 09-03 10:16:59（要聞）                         |
| `money.udn.com`            | `https://money.udn.com/rssfeed/news/1001/5591?ch=money`              | `rss`          | ❌        | 產業                                           |
| `feeds.feedburner.com`     | `https://feeds.feedburner.com/rsscna/finance`                        | `rss`          | ❌        | 09-03 00:55:15（中央社財經）                   |
| `feeds.feedburner.com`     | `https://feeds.feedburner.com/ettoday/finance`                       | `rss`          | ❌        | 09-03 08:15:00，50 則                          |
| `cdn.technews.tw`          | `https://cdn.technews.tw/feed/`                                      | `rss`          | ❌        | 09-03 10:35（`technews.tw/feed/` 會 302 到此） |
| `news.ltn.com.tw`          | `https://news.ltn.com.tw/rss/all.xml`                                | `rss`          | ❌        | 09-03 10:35:10                                 |
| `www.inside.com.tw`        | `https://www.inside.com.tw/feed/rss`                                 | `rss_full`     | ✅        | 09-03 07:34，僅 11 則                          |
| `www.gvm.com.tw`           | `https://www.gvm.com.tw/rss`                                         | `rss`          | ❌        | 09-02 21:18                                    |
| `www.chinatimes.com`       | `https://www.chinatimes.com/sitemaps/sitemap_wantrich_todaynews.xml` | `news_sitemap` | ❌        | 09-03 10:09:01，200+ 條（中時財經專版）        |
| `www.ctee.com.tw`          | `https://www.ctee.com.tw/sitemaps/sitemap_newstoday.xml`             | `news_sitemap` | ❌        | 09-03 10:09:05，150+ 條                        |
| `www.businesstoday.com.tw` | `https://www.businesstoday.com.tw/news-sitemap.xml`                  | `news_sitemap` | ❌        | 09-03 10:00:00，200 條                         |
| `www.storm.mg`             | `https://www.storm.mg/feed/sitemap/news`                             | `news_sitemap` | ❌        | 09-03 10:35，200+ 條                           |

**注意事項：**

- **既有的 `cnyes_json` 來源可以保留或改用上表的 `rss_full`**。RSS 版直接帶
  `content:encoded` 全文，改用之後 cnyes 的文章不必再走 `fetch_article`。
  建議改用 RSS 版並移除 `cnyes_json` adapter，但要先確認 RSS 的分類覆蓋度符合需求。
- **`money.udn.com` 分類 `5588` 已死**（停在 06-21 且內容是國際新聞），不要加。
  udn 主站的 `/rssfeed/news/2/6644` 回傳 20 則全空白、pubDate 全為 1970-01-01。
- **自由時報財經分類 `/rss/business.xml` 落後四天**，用 `all.xml` 再自行過濾。
- news sitemap 只給標題、URL、時間戳，一律需要 `fetch_article` 取內文。
  實測 ctee、chinatimes、storm.mg 的文章頁直接回完整內文、無付費牆、不需 JS 渲染。
- 部分站台的 robots.txt 針對特定 AI crawler UA 設限。本專案的 UA 是
  `DailyInsightsNewsBot/1.0`，既有的 `robots_allowed` 會依此判斷。若某來源被拒，
  會走既有的 `news.feed.failed` 事件路徑，不要為此繞過 robots 檢查。

### 4.3 香港與日韓 — `poll_group="fast"` / `"normal"`

| hostname             | url                                                              | kind  | 佐證                                         |
| -------------------- | ---------------------------------------------------------------- | ----- | -------------------------------------------- |
| `www.etnet.com.hk`   | `https://www.etnet.com.hk/www/tc/news/rss.php?section=editor`    | `rss` | 09-03 09:24:00（精選新聞）                   |
| `www.etnet.com.hk`   | `…rss.php?section=rumour`                                        | `rss` | 股市傳聞                                     |
| `www.etnet.com.hk`   | `…rss.php?section=commentary`                                    | `rss` | 09-03 10:42:00（股票評論）                   |
| `www.etnet.com.hk`   | `…rss.php?section=special`                                       | `rss` | 09-03 10:00:00（焦點專題）                   |
| `rthk9.rthk.hk`      | `https://rthk9.rthk.hk/rthk/news/rss/c_expressnews_cfinance.xml` | `rss` | 09-03 06:28:31「港股ADR普遍上升」            |
| `www.stheadline.com` | `https://www.stheadline.com/rss`                                 | `rss` | 09-03 10:31:25（綜合，需過濾）               |
| `toyokeizai.net`     | `https://toyokeizai.net/list/feed/rss`                           | `rss` | 09-03 06:30:00                               |
| `diamond.jp`         | `https://diamond.jp/list/feed/rss/dol`                           | `rss` | 09-02 13:15:00                               |
| `www.kyodo.co.jp`    | `https://www.kyodo.co.jp/feed/`                                  | `rss` | 09-03 02:30 GMT（綜合，需過濾）              |
| `assets.wor.jp`      | `https://assets.wor.jp/rss/rdf/nikkei/news.rdf`                  | `rss` | 09-03 08:09:06（日經速報第三方鏡像，僅標題） |
| `www.hankyung.com`   | `https://www.hankyung.com/feed/finance`                          | `rss` | 09-03 10:49:33（증권）                       |
| `www.hankyung.com`   | `https://www.hankyung.com/feed/economy`                          | `rss` | 09-02 08:36:52                               |

- `assets.wor.jp` 是 RSS 1.0 / RDF 格式，`parse_rss` 目前只 iter `item`；
  RDF 的 item 在 `rdf:RDF` 下且用 `dc:date` 而非 `pubDate`，需要在 adapter 中處理。
- hankyung 的 feed **只有標題、連結、作者、日期，沒有摘要**，一律需要 fetch。
- 舊網域 `rss.hankyung.com/feed/*.xml` 會 302 到新網域，直接用新的。

### 4.4 英文與新聞稿 — `poll_group="normal"`，Earnings 用 `"flash"`

| hostname                    | url                                                                                                     | kind       | full text | 佐證                                                   |
| --------------------------- | ------------------------------------------------------------------------------------------------------- | ---------- | --------- | ------------------------------------------------------ |
| `feeds.content.dowjones.io` | `https://feeds.content.dowjones.io/public/rss/RSSMarketsMain`                                           | `rss`      | ❌        | 09-01 21:25 GMT，**73 則**，量最大                     |
| `feeds.content.dowjones.io` | `https://feeds.content.dowjones.io/public/rss/mw_topstories`                                            | `rss`      | ❌        | 09-02 00:19 GMT，10 則                                 |
| `www.investing.com`         | `https://www.investing.com/rss/news.rss`                                                                | `rss`      | ❌        | 09-02 12:16:09，10 則純標題                            |
| `www.investing.com`         | `https://www.investing.com/rss/news_25.rss`                                                             | `rss`      | ❌        | 股市分類                                               |
| `www.nasdaq.com`            | `https://www.nasdaq.com/feed/rssoutbound?category=Markets`                                              | `rss`      | ❌        | 09-02 02:51:32，item 帶 ticker 標籤                    |
| `www.forbes.com`            | `https://www.forbes.com/business/feed/`                                                                 | `rss`      | ❌        | 09-01 12:14:04，30 則                                  |
| `www.thestreet.com`         | `https://www.thestreet.com/.rss/full/`                                                                  | `rss_full` | ✅        | 09-03 00:17 GMT，完整 HTML 全文                        |
| `www.cityam.com`            | `https://www.cityam.com/feed/`                                                                          | `rss_full` | ✅        | 09-02 05:00，800–2000+ 字                              |
| `www.mining.com`            | `https://www.mining.com/feed/`                                                                          | `rss_full` | ✅        | 09-02 20:30，20 則                                     |
| `www.benzinga.com`          | `https://www.benzinga.com/feed/`                                                                        | `rss_full` | ✅        | ⚠️ **一次只回 2 則**，需 `poll_group="flash"` 才不漏稿 |
| `www.globenewswire.com`     | Earnings（代碼 13，見下）                                                                               | `rss`      | ❌        | 09-02 21:00 GMT，20 則                                 |
| `www.globenewswire.com`     | Mergers and Acquisitions（代碼 27）                                                                     | `rss`      | ❌        | 08-28 15:00 GMT                                        |
| `www.globenewswire.com`     | Company Announcement（代碼 9）                                                                          | `rss`      | ❌        | 09-02 17:38 GMT                                        |
| `www.prnewswire.com`        | `https://www.prnewswire.com/rss/news-releases-list.rss`                                                 | `rss`      | ❌        | 09-01 06:39，20 則                                     |
| `www.prnewswire.com`        | `https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss` | `rss`      | ❌        | 25 則                                                  |

**GlobeNewswire URL 結構**（空格必須 encode 成 `%20`）：

```
https://www.globenewswire.com/RssFeed/subjectcode/{code}-{name}/feedTitle/GlobeNewswire%20-%20{name}
```

Earnings 完整範例：

```
https://www.globenewswire.com/RssFeed/subjectcode/13-Earnings%20Releases%20and%20Operating%20Results/feedTitle/GlobeNewswire%20-%20Earnings%20Releases%20and%20Operating%20Results
```

- 每個主題 feed 只回 **20 則**。財報尖峰時段視窗不夠，Earnings 那支必須高頻輪詢。
- **不要加 Analyst Recommendations（代碼 3）與 Bond Market News（代碼 6）**：
  實測分別落後 10 天與 3 週，發稿量本來就低，加了只會觸發 stale 告警。
- PR Newswire feed **混雜多國語言稿件**（實測看到斯洛伐克文），需在選稿前做語言過濾。
- Forbes 的 `content:encoded` 標籤存在但**內容只有摘要長度**，設 `provides_full_text=False`。
  判斷全文不能只看標籤有無，要看實際長度。

### 4.5 Guardian Content API — `poll_group="normal"`，需免費金鑰

版權方自己開放的 API，是本註冊表中**唯一需要金鑰、也是唯一提供全文的高品質英文綜合源**。
目前英文全文只有 TheStreet、City AM、Mining.com 三家，Guardian 能明顯補強國際財經與政策題材。

| 項目               | 值                                                               |
| ------------------ | ---------------------------------------------------------------- |
| hostname           | `content.guardianapis.com`                                       |
| base               | `https://content.guardianapis.com/search`                        |
| kind               | `json_list`                                                      |
| provides_full_text | ✅                                                               |
| 免費層             | **500 calls/day、1 call/sec**、可存取 article text、190 萬篇存量 |
| 金鑰申請           | `https://open-platform.theguardian.com/access/` 的 developer key |

建議的請求參數：

```
GET https://content.guardianapis.com/search
    ?section=business|world|politics
    &from-date={YYYY-MM-DD}
    &order-by=newest
    &page-size=50
    &show-fields=headline,trailText,bodyText,byline,wordcount,firstPublicationDate
    &api-key={GUARDIAN_API_KEY}
```

`JsonListMapping` 對應：

```python
JsonListMapping(
    items_path=("response", "results"),
    id_field="id",
    url_field="webUrl",
    url_template=None,
    title_field="webTitle",
    time_field="webPublicationDate",
    time_format="iso",
    body_field="fields.bodyText",
)
```

回傳結構要點：頂層 `response` 下有 `status`、`total`、`pages`、`currentPage`、`results[]`；
每個 result 有 `id`、`webTitle`、`webPublicationDate`、`webUrl`、`sectionName`，
以及在 `show-fields` 指定後才出現的 `fields` 物件。

**實作注意：**

- **`page-size` 上限 200**，但配合 500 calls/day 的額度，建議 50 並依 section 分開請求。
- `show-fields=all` 會回傳大量用不到的欄位，明確列出需要的欄位可縮小 payload。
- **`fields.body` 是 HTML，`fields.bodyText` 是純文字**。摘要階段用 `bodyText`，
  可省掉一次 HTML 清理。
- 每秒 1 次的限制比其他來源嚴格得多，**必須在 adapter 層做節流**，不能跟其他 feed 一起併發。
- Guardian 有 24 小時內同步更新與撤稿的義務條款。因為本專案**不儲存正文**，
  影響僅限於已發佈 edition 中的標題與連結；若之後要做「已發佈內容回頭校正」，
  這是需要處理的來源之一。
- 設定新增 `DAILY_INSIGHTS_GUARDIAN_API_KEY`，同步更新 `core/config.py` 與 `.env.example`。
  依既有慣例用 `SecretStr`，且 production 驗證要走 `is_placeholder_value` 檢查。

> ⚠️ **參數細節需要第一次呼叫時確認**。撰寫本規格時 `open-platform.theguardian.com`
> 的官方文件頁無法取得（回 403），上述參數名與回傳結構取自次級來源。
> `bodyText` 與 `trailText` 的實際欄位名請在接上的第一個請求就用
> `show-fields=all` 印出完整 keys 確認一次，再改成明確欄位清單。

### 4.6 可選的補充來源（需申請免費金鑰，非必要）

以下兩個不列入主要註冊表，但在特定需求出現時值得接。實作時先不做，
把它們留在這裡當作後續選項。

**Webz.io News API Lite** — 亞洲與小語種的補充

| 項目   | 值                                    |
| ------ | ------------------------------------- |
| 免費層 | 1,000 calls/月、每次 10 篇、30 天歷史 |
| 全文   | ✅ 回傳含 article content             |
| 語言   | 170+                                  |
| 換算   | 約每日 330 篇全文                     |

適用時機：Phase 3 上線後，若亞洲與小語種的候選數長期偏低（`news.candidates.below_floor`
頻繁觸發），用它補足廣度。
⚠️ **確切端點與參數本規格未能查證**（文件頁為 JS 渲染），
請以 `https://docs.webz.io/reference/news-api-lite` 為準。

**Marketaux** — 標的對應與情緒訊號

| 項目   | 值                                      |
| ------ | --------------------------------------- |
| base   | `https://api.marketaux.com/v1/news/all` |
| 免費層 | 100 req/day、**每次僅 3 篇**            |
| 全文   | ❌ 只有 snippet                         |

它的價值**不在新聞本身**（3 篇/次太少），而在回傳的 `entities[]` 陣列：
每則新聞已標註股票代號、產業、國別、`match_score` 與 `sentiment_score`，
還有 `highlights[]` 指出情緒來自標題還是內文的哪一段。

```json
"entities": [{
  "symbol": "TSLA", "name": "Tesla, Inc.", "type": "equity",
  "industry": "Consumer Cyclical", "country": "us",
  "match_score": 12.13, "sentiment_score": 0.7783,
  "highlights": [{"highlight": "...", "sentiment": 0.7783,
                  "highlighted_in": "title"}]
}]
```

適用時機：晨報要做「新聞 → 個股」對應時，可先評估它的標註品質，
再決定是自建還是採用。主要參數：`api_token`、`symbols`、`entity_types`、
`industries`、`countries`、`language`、`published_after`、`sentiment_gte/lte`、
`filter_entities`、`limit`、`page`。回應 header 帶 `X-UsageLimit-Limit`
與 `X-RateLimit-Limit` 可監控用量。

## 5. 要從 `FEED_SOURCES` 移除的來源

以下三個目前在註冊表中，實測已無法取得資料。移除理由純粹是**功能性**的：

| 來源           | 實測結果                                                                                               |
| -------------- | ------------------------------------------------------------------------------------------------------ |
| `www.cnbc.com` | RSS 端點與網頁一致回 **403**。CNBC 全面阻擋自動化存取，備援的 `search.cnbc.com` 亦被 robots.txt 禁止。 |
| `www.bbc.com`  | 2026 年實測 13 家媒體的 robots.txt，BBC 對 7 種 crawler UA **全部封鎖**，封鎖率最高。                  |
| `apnews.com`   | 7 種 UA 中封鎖 5 種。`listing` kind 依賴 HTML 錨點抓取，在此封鎖程度下不可靠。                         |

移除後 `news_allowed_hostnames` 的預設值要同步調整（`core/config.py` 與 `.env.example`）。

## 6. 已知會誤判的陷阱清單

實作與 code review 時逐項確認：

1. **殭屍 feed**：HTTP 200 + XML 合法 + 有 20 則項目，但內容停在數月至數年前。
   實測遇到的例子：Zerohedge 停在 2014-12、數位時代 bnext 停在 2020-04、
   Investing.com 經濟指標分類停在 2024-11、MarketWatch 兩個分類停在 2025 年中、
   SEC EDGAR XBRL 全量 feed 停在 2026-02（該 feed 自稱每 10 分鐘更新）。
   **只驗證 HTTP 狀態碼一定會誤判**，Phase 1 的新鮮度檢查就是為了這個。
2. **東方財富 CDN 快取**：見 §4.1。
3. **`content:encoded` 假全文**：Forbes 有標籤但只有摘要；CoinDesk 的該標籤是空的。
   `provides_full_text` 應由實際長度門檻驗證，不是看標籤存在。
   建議在 adapter 中加最小長度檢查（例如 200 字元），不足則退回 fetch 路徑。
4. **RDF vs RSS**：`assets.wor.jp` 是 RSS 1.0，item 不在 `channel` 下、時間欄是 `dc:date`。
5. **PR Newswire 多語混雜**：實測看到斯洛伐克文稿件，選稿前需做語言過濾。
6. **hankyung 無摘要**：feed 只有標題連結，選稿階段若依賴摘要會拿到空字串。
7. **移除 GDELT 後沒有後備發現路徑**：任何讓 `FEED_SOURCES` 整批失效的改動
   （設定錯誤、allowlist 推導出錯、共用的 feedburner 網域故障）都會直接造成
   空的 edition。Phase 1 的 stale 與 below_floor 兩個事件是唯一的早期警訊，
   務必接上告警而不只是寫進 log。

## 7. 測試要求

沿用 `AGENTS.md` 的驗證規則，另外要求：

- 每個新 `kind` 至少三組 fixture：正常、欄位缺漏、時間格式異常。
- 新鮮度檢查要有一組「舊 payload 觸發 stale 事件」的測試。
- `provides_full_text` 的來源要有一組「feed 帶全文時不呼叫 `fetch_article`」的測試
  （用 mock 驗證未被呼叫）。
- 東方財富 adapter 要有一組測試驗證每次請求的 `r` 參數不同。
- 候選數量下限檢查要有測試：candidate 數不足時應觸發 `news.candidates.below_floor`。
- 不要把真實網路請求寫進測試。所有 fixture 落地成檔案。

`make check` 必須通過（format、lint、type、test、build）。

## 8. 交付方式

- 分支命名 `feat/news-feed-expansion`（或依 phase 拆成多個 `feat/...`）。
- commit 訊息格式 `<type>: <summary>`，見 `AGENTS.md`。
- 每個 phase 一個 PR，PR 描述說明該 phase 的驗收項目。
- 不使用 `--no-verify`。

## 9. 本規格未能驗證的項目

以下項目撰寫規格時無法實測（研究環境的網路出口限制，**不代表失效**），
實作第一步請自行確認：

- **Guardian Content API 的參數名與回傳欄位**（見 §4.5 的警告）。官方文件頁回 403，
  參數取自次級來源，第一次呼叫請用 `show-fields=all` 確認實際 keys。
- **Webz.io News API Lite 的端點與參數**（文件頁為 JS 渲染，見 §4.6）。
- **SEC EDGAR** `browse-edgar?action=getcurrent&type=8-K&output=atom`。
  這是美股公司事件最即時的來源，值得補測後加入註冊表。
  SEC 硬性要求 `User-Agent: 公司名 聯絡email`，且上限 10 req/sec。
- Yahoo Finance 個股 feed、Seeking Alpha、Business Wire、매일경제 mk.co.kr、
  연합뉴스 yna.co.kr、ロイター日本、Business Insider、Financial Post、Barron's。
- 風傳媒的 RSS（`/api/getRss/channel_id/4`，財經）—— 目前規格用它的 news sitemap 替代。
- 商業周刊的 RSS（`/rss` 是 React SPA，訂閱連結由 JS 產生）。
- 台灣民間媒體中，**天下雜誌 robots.txt 為全站 Disallow**，不列入註冊表。

## 10. 相關文件

- 授權與合規盤點（本規格以取得資料為優先，未含授權評估）：見專案的資料源盤點頁面。
- 既有的每日重大新聞架構：`docs/architecture/daily-news.md`（若該檔在你的 checkout 中不存在，
  請以 `modules/news/` 的程式碼與註解為準）。
