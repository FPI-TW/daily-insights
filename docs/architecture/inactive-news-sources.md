# 非執行中的新聞來源

2026-09-14 整理。此目錄不供程式載入，不擴充文章白名單。重新啟用前須驗證端點、robots、正文擷取、市場政策與測試。

## 尚未上線市場

目前新聞只支援 global、tw_equity、us_equity；以下 19 個 feed 從執行清單移出，保留供未來市場開發參考。端點是原設定紀錄，不代表目前已通過可用性驗證。

| 市場 | 來源         | 原 feed 端點                                                                                 |
| ---- | ------------ | -------------------------------------------------------------------------------------------- |
| 中國 | 財聯社       | `https://m.cls.cn/nodeapi/telegraphs?app=CailianpressWap&os=web&sv=1&rn=30`                  |
| 中國 | 金十數據     | `https://www.jin10.com/flash_newest.js`                                                      |
| 中國 | 華爾街見聞   | `https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&client=pc&limit=30` |
| 中國 | 東方財富     | `https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html`                 |
| 中國 | 新浪財經     | `https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num=30&page=1`                |
| 中國 | 澎湃新聞     | `https://cache.thepaper.cn/contentapi/wwwIndex/rightSidebar`                                 |
| 中國 | 界面新聞     | `https://feedx.net/rss/jiemian.xml`                                                          |
| 香港 | 經濟通       | `https://www.etnet.com.hk/www/tc/news/rss.php?section=editor`                                |
| 香港 | 經濟通       | `https://www.etnet.com.hk/www/tc/news/rss.php?section=rumour`                                |
| 香港 | 經濟通       | `https://www.etnet.com.hk/www/tc/news/rss.php?section=commentary`                            |
| 香港 | 經濟通       | `https://www.etnet.com.hk/www/tc/news/rss.php?section=special`                               |
| 香港 | 香港電台     | `https://rthk9.rthk.hk/rthk/news/rss/c_expressnews_cfinance.xml`                             |
| 香港 | 星島頭條     | `https://www.stheadline.com/rss`                                                             |
| 日本 | 東洋経済     | `https://toyokeizai.net/list/feed/rss`                                                       |
| 日本 | ダイヤモンド | `https://diamond.jp/list/feed/rss/dol`                                                       |
| 日本 | 共同通信     | `https://www.kyodo.co.jp/feed/`                                                              |
| 日本 | 日本経済新聞 | `https://assets.wor.jp/rss/rdf/nikkei/news.rdf`                                              |
| 韓國 | 한국경제     | `https://www.hankyung.com/feed/finance`                                                      |
| 韓國 | 한국경제     | `https://www.hankyung.com/feed/economy`                                                      |

## 暫停抓取

| 來源     | 原 feed 端點                                                         | 原因與重新啟用條件                                                      |
| -------- | -------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| 旺得富   | `https://www.chinatimes.com/sitemaps/sitemap_wantrich_todaynews.xml` | 本機連續記錄 source_access_denied；取得可合法存取的來源並驗證後再啟用。 |
| 工商時報 | `https://www.ctee.com.tw/sitemaps/sitemap_newstoday.xml`             | 本機連續記錄 source_access_denied；取得可合法存取的來源並驗證後再啟用。 |

## 已移除的廣泛公司公告

GlobeNewswire Company Announcement（subjectcode 9）退出執行清單，以縮減例行公司公告候選。保留財報（subjectcode 13）與併購（subjectcode 27）。

原端點：`https://www.globenewswire.com/RssFeed/subjectcode/9-Company%20Announcement/feedTitle/GlobeNewswire%20-%20Company%20Announcement`

此調整保留既有新聞、候選與稽核紀錄；歷史失敗紀錄不是目前仍在抓取的證據。FinDB 不在本次修改範圍。
