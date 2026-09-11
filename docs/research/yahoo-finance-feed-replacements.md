# Yahoo Finance RSS 替代來源研究

研究日期：2026-09-11（Asia/Taipei）

## 結論

Yahoo Finance `https://finance.yahoo.com/news/rssindex` 可以移除，但沒有任何單一「官方 feed」能完整取代它原本涵蓋的美股個股、產業與總體新聞。較可靠的方案是改用一組第一方來源：

1. 優先實作美國財政部 sitemap、BLS 分類 Atom、BEA News Release RSS 與 SEC Press Releases RSS。
2. EIA、CFTC 與 BIS 保留為條件式來源，先處理 robots／AI 使用政策與市場純度。
3. 不接入未驗證或定位不符的 Treasury `/feed`、Nasdaq Investor Relations RSS 與 IMF 舊 RSS 索引。

這組來源能補強「官方事件」的召回率，但不能替代 Yahoo 的一般財經編採。因此美股公司與產業催化劑仍應由現有 CNBC、TheStreet、GlobeNewswire、PR Newswire 等來源提供；官方來源主要負責總體數據、財政、監管、能源與市場結構事件。

## 驗證方法

本次只使用來源機構自己的網站、文件與端點。對候選端點實際執行 HTTP GET，檢查：

- HTTP 狀態、最終 URL、Content-Type 與可否解析。
- feed 項目數、最新項目時間及連結格式。
- 至少一個最新文章 URL 能否回傳 HTML。
- `robots.txt`、認證需求、已公布的請求速率或內容使用限制。

「新鮮度」以 2026-09-11 的即時回應為準。月度或事件驅動的官方來源在沒有新發布時，本來就可能超過 24 小時；這不代表 feed 故障，程式應以文章本身的發布時間決定是否納入當日候選，而不應因此永久停用來源。

## 通過來源驗證，適合優先實作

這四組來源均由官方網站提供且即時驗證可用，但仍須為各自的格式、來源配額與合規要求完成程式接線及 fixture 測試；「通過來源驗證」不表示把 URL 加進現有註冊表即可直接上線。

### 1. U.S. Department of the Treasury sitemap

- 端點：<https://home.treasury.gov/sitemap.xml>
- 建議市場：`global`、`us_equity`
- 建議格式：一般 XML sitemap；只保留 URL path 為 `/news/press-releases/` 的項目。
- 即時驗證：HTTP 200、`text/xml`、約 2.49 MB；共有 15,385 個新聞稿 URL。最新項目包含 `sb0627`，`lastmod` 為 2026-09-10 23:30:52 UTC，文章頁可由同一官方網域取得。
- robots／認證：`robots.txt` 對 `User-agent: *` 為 `Allow: /`，無需 API key。
- 注意事項：sitemap 很大且包含歷史資料，應以 `lastmod` 做增量探索，再讀文章頁的實際發布日期；`lastmod` 是更新時間，不應直接當作 `published_at`。
- 現有程式差異：此檔約 2.49 MB，超過目前單一 feed 的 2 MB 下載上限；它也是不含 `news:title` 的一般 sitemap，現有 Google News sitemap adapter 會產生 0 筆候選。接入前需新增受限的一般 sitemap 解析路徑，並以串流篩選、較小的官方 sitemap 分片或僅針對此來源的經審核大小上限處理，不能直接放寬所有來源的全域限制。

財政部官方新聞稿列表持續提供新內容，例如債務管理、制裁、國際資本流動與金融監管事件：[Treasury Press Releases](https://home.treasury.gov/news/press-releases)。官方 `robots.txt` 也直接列出上述 sitemap：[Treasury robots.txt](https://home.treasury.gov/robots.txt)。

### 2. Bureau of Labor Statistics 分類 Atom feeds

- 端點：
  - PPI：<https://www.bls.gov/feed/ppi.rss>
  - CPI：<https://www.bls.gov/feed/cpi.rss>
  - Employment Situation：<https://www.bls.gov/feed/empsit.rss>
- 建議市場：`global`、`us_equity`
- 建議格式：Atom（副檔名雖為 `.rss`）。
- 即時驗證：三個端點皆 HTTP 200、`application/rss+xml`，各有 12 個項目，文章連結皆回傳 HTTP 200 HTML。PPI 最新項目發布於 2026-09-10 07:50:35 EDT；CPI 與 Employment Situation 則依各自月度發布週期更新。
- robots／認證：feed 與新聞稿路徑未被禁止，無需 API key。
- 內容使用：BLS 表示其發布內容除少數既有版權圖片外均屬公有領域，可使用及連結，但希望註明來源：[BLS Copyright Information](https://www.bls.gov/opub/copyright-information.htm)。
- 注意事項：不建議使用 `https://www.bls.gov/feed/bls_latest.rss` 作為主要探索來源；驗證時它只有一個聚合項目，且連結指向 BLS 首頁，無法直接取得單一事件文章。

BLS 官方 RSS 索引明列 CPI、PPI、Employment Situation 與其他分類 feeds：[BLS RSS Feeds](https://www.bls.gov/feed/)。第一階段先接上述三項，避免低市場價值的統計公告稀釋候選池。

### 3. Bureau of Economic Analysis News Release Feed

- 端點：<https://apps.bea.gov/rss/rss.xml>
- 建議市場：`global`、`us_equity`
- 建議格式：RSS 2.0。
- 即時驗證：HTTP 200、`text/xml`、48 個項目；最新項目是 2026-09-03 的美國國際貿易數據，文章 URL 回傳 HTTP 200 HTML。
- robots／認證：公開 feed，不需 API key。
- 注意事項：BEA 是事件驅動／排程發布來源，沒有每日文章很正常。當日 24 小時內沒有發布時應回傳零候選，不應把來源標成永久失效。

BEA 的 For Developers 頁面正式提供「News Release Feed (RSS)」，用途是取得新聞稿摘要與數據：[BEA For Developers](https://www.bea.gov/resources/for-developers)。BEA 也說明經濟帳戶新聞稿會在預定發布時間後數分鐘內刊登：[Priorities and Schedules for Posting Content](https://www.bea.gov/about/policies-and-information/priorities-and-schedules-posting-content)。

可另外使用官方 release schedule JSON 作為「今天是否預期有發布」的輔助訊號，但它只有日程，不是新聞來源：<https://apps.bea.gov/API/signup/release_dates.json>。[BEA Online Calendar Subscription](https://www.bea.gov/news/schedule/icalendar)

### 4. SEC Press Releases RSS

- 端點：<https://www.sec.gov/news/pressreleases.rss>
- 建議市場：`us_equity` 為主；重大市場結構或跨境規則才進 `global`。
- 建議格式：RSS 2.0。
- 即時驗證：HTTP 200、`application/rss+xml`、25 個項目；最新項目發布於 2026-09-10 13:45:22 EDT，文章 URL 回傳 HTTP 200 HTML。
- robots：feed 與 `/newsroom/press-releases/` 未被禁止。
- 必要設定：所有自動請求必須送出能辨識應用程式及聯絡方式的 `User-Agent`；SEC 目前限制每個使用者合計不超過每秒 10 個請求，超量可能封鎖 IP：[SEC Developer Resources](https://www.sec.gov/about/developer-resources)。
- 注意事項：SEC press releases 會包含執法、任命與活動公告，應在本地先用市場結構、上市公司、加密資產、會計與重大執法等關鍵字過濾，再送入模型。

SEC 官方 RSS 索引明列 Press Releases 與 EDGAR 搜尋 feed：[SEC RSS Feeds](https://www.sec.gov/about/rss-feeds)。若未來啟用 EDGAR 8-K，仍需沿用具聯絡資訊的 User-Agent 與限速，不應把 EDGAR 當成無限制的一般新聞 API。

## 條件式接入

### 5. EIA Today in Energy／Press Releases

- 端點：
  - Today in Energy：<https://www.eia.gov/rss/todayinenergy.xml>
  - Press Releases：<https://www.eia.gov/rss/press_rss.xml>
- 建議市場：`global` 為主；只有會顯著影響美股能源、運輸或通膨定價的事件才複用到 `us_equity`。
- 即時驗證：兩者皆 HTTP 200、`text/xml`。Today in Energy 有 15 個項目，最新項目發布於 2026-09-10 09:00 EST，文章頁回傳 HTTP 200；Press Releases 有 11 個項目，最新項目發布於 2026-09-09 12:00 EST。
- 解析注意：Press Releases feed 內的 `link` 是相對 URL，例如 `/pressroom/releases/press592.php`，必須相對 feed origin 做 `urljoin`。
- 內容使用：EIA 表示 Today in Energy 除另有版權標示外屬公有領域，允許在註明來源後複製與散布：[Today in Energy About](https://www.eia.gov/todayinenergy/about.php)。EIA 的一般重用政策也要求標明來源及發布日期：[EIA Copyrights and Reuse](https://www.eia.gov/about/copyrights_reuse.php)。
- 阻擋點：EIA 官方 RSS 頁面鼓勵訂閱並說明 RSS 會持續提供最新發布，但其 `robots.txt` 同時對 `User-agent: *` 設定 `Disallow: /rss`：[EIA RSS Feeds](https://www.eia.gov/tools/rssfeeds/)、[EIA robots.txt](https://www.eia.gov/robots.txt)。現行系統承諾遵守 robots，因此在未取得明確授權或調整合規判定前，不應直接上線。

建議先向 EIA 確認自動聚合使用方式，或尋找不位於 `/rss` 的正式 API／通知端點；確認後再接入。

### 6. CFTC General Press Releases RSS

- 端點：<https://www.cftc.gov/RSS/RSSGP/rssgp.xml>
- 建議市場：`us_equity` 與 `global`，但只保留衍生品、商品、數位資產、清算與市場結構事件。
- 即時驗證：HTTP 200、`application/rss+xml`、10 個項目；最新項目發布於 2026-09-10 20:36:53 UTC，文章頁回傳 HTTP 200 HTML。
- 認證：不需 API key。
- 使用限制：CFTC `robots.txt` 對一般 agent 允許抓取，但宣告 `search=yes, ai-train=no, use=reference`，並封鎖多個具名 AI crawlers：[CFTC robots.txt](https://www.cftc.gov/robots.txt)。CFTC 官方頁面確認 general press releases 是正式 RSS 類別：[CFTC RSS](https://www.cftc.gov/RSS/index.htm)。

現行流程會把正文送往第三方模型生成摘要，是否符合 `use=reference` 需要先由產品／法務確認。確認前可只用標題、日期與連結做規則式探索，不應把完整正文送入 DeepSeek。

### 7. Bank for International Settlements Media Releases RSS

- 端點：<https://www.bis.org/doclist/all_pressrels.rss>
- 建議市場：`global`
- 建議格式：RSS 1.0／RDF。
- 即時驗證：HTTP 200、`application/rss+xml`、10 個項目；最新項目日期為 2026-09-08，文章頁回傳 HTTP 200 HTML。
- robots／認證：feed 與 media release 路徑未被禁止，不需 API key。
- 注意事項：發布頻率低、內容集中在國際金融標準與 BIS 機構消息，應設定低來源配額並以政策實質性過濾。

BIS 官方 RSS 索引列出 Media releases feed：[BIS RSS feeds](https://www.bis.org/rss/index.htm)。接入前仍應檢查 BIS 當期著作權／重用條款，摘要中只保存必要事實並連回原文。

## 不建議接入

### Treasury `/news/press-releases/feed`

`https://home.treasury.gov/news/press-releases/feed` 不是財政部頁面提供的正式 feed；即時驗證多次逾時，官方新聞稿頁也未列出此連結。應使用已驗證的 sitemap，不應依賴猜測的 Drupal `/feed` 路徑。

### Nasdaq Investor Relations RSS

`https://ir.nasdaq.com/rss/news-releases.xml?items=15` 是 Nasdaq, Inc. 自身的投資人關係新聞，不是 Nasdaq 上市公司的綜合市場新聞。官方頁面確實列出 All News Releases／Financial Releases 等 RSS，但本次即時請求逾時，而且內容定位只涵蓋單一公司；不適合作為 Yahoo Finance 替代來源。[Nasdaq IR RSS Feeds](https://ir.nasdaq.com/tools/rss-feeds)

Nasdaq.com 的市場新聞頁面也說明其公司新聞稿來自 BusinessWire、PR Newswire、GlobeNewswire 等第三方 feeds，而不是 Nasdaq 自己生成的第一方公司新聞；專案已經直接使用其中部分新聞稿來源，無需再透過 Nasdaq 重複收錄。[Nasdaq Market Activity FAQ](https://www.nasdaq.com/market-activity)

### IMF 舊 RSS 索引

IMF Social Hub 仍連到 `https://www.imf.org/external/rss/rsslist/rsslist.aspx`，但本次直接請求回傳 HTTP 403，無法穩定列舉或驗證實際 feed。IMF 新聞頁本身有持續更新，但在沒有可穩定存取、由 IMF 明確列出的新聞 feed 前，不建議加入 production。[IMF Social Hub](https://www.imf.org/en/social-hub)、[IMF News](https://www.imf.org/en/news)

### BEA release schedule JSON 當作新聞來源

`https://apps.bea.gov/API/signup/release_dates.json` 僅提供資料發布名稱與時間，沒有文章標題、摘要或發布結果。它適合預熱抓取、事件日曆與監控缺漏，不可取代 BEA RSS，也不可直接送進新聞篩選模型。

## 建議的接入順序

1. **Treasury sitemap**：補上目前缺少、且會直接影響美債、美元、制裁、金融條件的官方財政事件。
2. **BLS PPI／CPI／Employment feeds**：用分類 Atom 直接捕捉通膨與就業催化劑。
3. **BEA RSS**：補 GDP、PCE、企業獲利、貿易與國際收支。
4. **SEC Press Releases RSS**：設定合規 User-Agent 與低於 10 requests/second 的全域限速後接入，並做本地關鍵字預濾。
5. **EIA**：先釐清 `/rss` robots 衝突。
6. **CFTC／BIS**：完成內容使用政策確認與低配額規則後再接。

## 實作驗收條件

每個新來源在 production 啟用前，至少應滿足：

- 連續 7 天或跨過至少 2 次預定發布事件的可用性觀察。
- 發布事件發生時，feed／sitemap 能在合理時間內出現新項目。
- 文章 URL 可經現有 SSRF、hostname、HTTPS 與 robots 檢查。
- Atom、RSS 1.0/RDF、RSS 2.0、相對 URL 與一般 sitemap 都有固定測試樣本。
- 對月度／事件驅動來源，不因「24 小時內沒有文章」誤判為 feed 故障。
- 每個來源設定獨立市場、來源配額與本地關鍵字預濾，避免官方例行公告擠掉真正的市場催化劑。
- SEC 使用明確識別產品及聯絡方式的 User-Agent，並在所有 worker 共用限速器。
- 摘要與前端展示均保留來源名稱、原始發布時間及原文連結。
