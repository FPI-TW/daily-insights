# Web 動畫設計（Motion）

狀態：已實作於 `apps/web`，涵蓋客戶端與後台全部頁面。視覺語言以
[motion.dev](https://motion.dev/) 的節制風格為參考；動畫只作為「內容抵達」與
「操作回饋」的訊號，不作裝飾。

本文記錄 2026-09-02 談定的範圍與參數。改動參數時同步更新本文與
`apps/web/src/lib/motion.ts`。

## 範圍

| 類型     | 做                                                                                                                                              | 不做                                             |
| -------- | ----------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| 進場     | 客戶端載入後才渲染的內容：報告卡、報告區塊、新聞卡、Podcast 集數、帳號面板、後台清單、載入骨架、非同步狀態訊息                                  | 伺服器直出的靜態外框（AppShell、標題、登入表單） |
| 互動     | 所有按鈕 tap 縮到 0.97；卡片 hover 上抬 2px 並加深陰影；主題切換 spring tap；語系切換、頂部導覽、後台分頁與市場分類列的作用中標記以 spring 滑動 | 圖示旋轉、hover 放大、輸入框聚焦動畫             |
| 版面過渡 | 路由切換交叉淡出；設定面板與錯誤提示開合                                                                                                        | 捲動驅動（`whileInView`）與捲動進度              |

## 套件與集中管理

- 使用 `motion` 套件的 `motion/react`（React 19、SSR 相容）。不採用
  `LazyMotion`；client chunk 約 42 KB gzip，之後若成為問題再切換。
- 所有時長、easing、variants 與互動預設集中在 `apps/web/src/lib/motion.ts`。
  元件只引用預設名稱，不在元件內寫數值。

| 參數                                | 值                                                                                     |
| ----------------------------------- | -------------------------------------------------------------------------------------- |
| `durations.fast` / `base` / `enter` | 150 / 250 / 350 ms                                                                     |
| 路由淡出 / 淡入                     | 250 ms / 350 ms（淡入在淡出結束後開始）                                                |
| easing                              | 進場 `cubic-bezier(0.22, 1, 0.36, 1)`，離場 `cubic-bezier(0.4, 0, 1, 1)`               |
| spring                              | `snappy`（stiffness 520 / damping 34）用於 hover 與滑動標籤；`tap`（700 / 30）用於 tap |
| stagger                             | 每筆 40 ms，只對前 8 筆錯開，其餘與第 8 筆同時出現                                     |

## SSR 與首屏

伺服器直出的內容在 hydration 前已在畫面上；若對它套進場動畫，不是要等 JS
到達才顯示，就是會先顯示再消失再淡入。因此 `useEnterAnimation()` 只在
hydration 完成後掛載的元件回傳 `true`：

- 直接開啟或重新整理頁面：所有內容靜態顯示，不播進場。
- 客戶端導覽、loader 完成、載入骨架、非同步錯誤或播放器狀態：播進場。

判斷方式是 `useSyncExternalStore` 的伺服器快照：React 在任何 hydration 渲染
（包含串流 SSR 下較晚 hydrate 的 Suspense 邊界）都會回傳伺服器快照，只有沒有
伺服器 HTML 的掛載才拿到客戶端快照。不能改用根路由 effect 設定的模組旗標：根
路由的 effect 會比較晚 hydrate 的邊界更早觸發，那些內容會先消失再淡入。

## 路由過渡

原本規劃以 `AnimatePresence mode="wait"` 包住 `Outlet`，但 TanStack Router 的
`Outlet` 讀取即時的 router state，離場中的舊頁會在 loader 完成時直接換成新內
容，無法凍結；而 `defaultPreload: "intent"` 讓多數導覽在幾毫秒內完成，以
router 狀態驅動的淡出幾乎不會播放。

因此路由交叉淡出改用瀏覽器 View Transitions API：`router.tsx` 開啟
`defaultViewTransition: true`，TanStack Router 在 loader 完成、提交新頁面時呼
叫 `document.startViewTransition`，瀏覽器對舊畫面截圖後再淡入新頁面，等待
loader 的語意與 `mode="wait"` 相同。時序在 `styles.css` 的
`::view-transition-old(root)` / `::view-transition-new(root)` 規則中定義。

兩個例外：

- 切換語系會停留在同一頁、只替換文字，`router.tsx` 的 `defaultViewTransition.types`
  對「去掉語系後路徑相同」的導覽回傳 `false`，內容原地更新而不做交叉淡出。
- 載入骨架的時序由 `defaultPendingMs: 200` 與 `defaultPendingMinMs: 300` 控制：
  200 ms 內完成的載入直接換內容，較慢的才顯示骨架並至少停留 300 ms。原本報告
  路由用 `pendingMs: 0` 加上預設 500 ms 的最短停留，每次第一次進入某個語系或
  市場都要等半秒，看起來像卡住。

另外 `html` 設了 `scrollbar-gutter: stable`，讓長短頁面切換時捲軸出現與否不會
讓置中的 header 左右位移。

### 切換元件的滑動標記

語系切換、客戶端頂部導覽、後台分頁與市場分類列共用
`components/ActiveIndicator.tsx`：作用中的連結內渲染一個帶 `layoutId` 的
`motion.span`，切換時標記從舊連結彈到新連結，而不是各自淡入淡出。三種樣式：
`pill`（淡色底）、`chip`（實色底）、`underline`（底線）。每個導覽以
`useIndicatorGroup()` 取得自己的群組 id，標記只在同一列的連結之間移動。

為了讓標記在路由切換時真的滑動而不是被交叉淡出的截圖蓋住，AppShell 的 header
與市場分類列各自帶 `view-transition-name`，在 `styles.css` 中把兩側都存在時的
old 隱藏、new 不做動畫；只有單側存在時（進出報告區）改跟頁面一起淡入淡出。
市場分類列與頁面標題並抽到 `_customer/reports.tsx` 這個 layout 路由，列表、
市場詳細、骨架與錯誤畫面都在其 `Outlet` 內替換，分類列本身不會重新掛載。

已知限制：

- 載入超過 `pendingMs` 才出現的骨架畫面由 `startTransition` 提交，不經過 View
  Transition，所以舊頁到骨架的切換沒有淡出；骨架本身有淡入，骨架到內容仍會交
  叉淡出。
- 不支援 View Transitions 的瀏覽器直接切換，沒有動畫，功能不受影響。

## 減少動態

根路由以 `MotionConfig reducedMotion="user"` 包住整個應用；使用者作業系統開啟
減少動態時，Motion 略過 transform 與 layout 動畫、只保留 opacity。`styles.css`
既有的 `prefers-reduced-motion` 規則另外關閉 CSS transition、View Transition 與
`tw-animate-css` 的動畫。

## 測試

- Vitest：`vitest.setup.ts` 設定 `MotionGlobalConfig.skipAnimations = true`，所
  有動畫立即完成，離場元素不會殘留在 DOM。
- Playwright：`playwright.config.ts` 以 `contextOptions.reducedMotion: "reduce"`
  觸發上述降級，斷言不會與進行中的動畫競速。
- 動畫本身以手動與截圖驗收，不寫自動化斷言。
