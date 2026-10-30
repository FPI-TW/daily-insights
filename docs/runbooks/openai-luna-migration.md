# OpenAI GPT-6 Luna 遷移與回復

新聞及聊天改用 OpenAI Chat Completions (`https://api.openai.com/v1`)，模型為 `gpt-6-luna`。新聞所有階段（含 embed 前繁中翻譯、why、精選去重及重複新聞篩選）使用 `high`，聊天使用 `medium`。embedding、功能啟用狀態、prompt 及選稿策略不隨本次遷移改變。

## 設定與金鑰

以下設定名稱均加 `DAILY_INSIGHTS_` 前綴，正式環境放 GitHub production Variables。

| 設定                                                            | Luna 設定                     |
| --------------------------------------------------------------- | ----------------------------- |
| `NEWSROOM_LLM_BASE_URL`                                         | `https://api.openai.com/v1`   |
| 四個 `NEWSROOM_*_MODEL`                                         | `gpt-6-luna`                  |
| `NEWSROOM_LLM_EXTRA_BODY`                                       | `{"reasoning_effort":"high"}` |
| `NEWSROOM_LLM_TIMEOUT_SECONDS`                                  | `240`                         |
| `NEWSROOM_LLM_MAX_COMPLETION_TOKENS`                            | `32768`                       |
| `NEWSROOM_LLM_SESSION_HEADER`                                   | 空白                          |
| `NEWSROOM_EDITOR_EXTRA_BODY`、`NEWSROOM_EDITOR_TIMEOUT_SECONDS` | 空白，繼承共同設定            |
| `CHAT_MODEL_PROVIDER`                                           | `openai`                      |
| `CHAT_MODEL_NAME`                                               | `gpt-6-luna`                  |
| `CHAT_MODEL_API_BASE_URL`                                       | `https://api.openai.com/v1`   |
| `CHAT_MODEL_REASONING_EFFORT`                                   | `medium`                      |
| `CHAT_MAX_OUTPUT_TOKENS`                                        | `16384`                       |
| `CHAT_TIMEOUT_SECONDS`                                          | `180`                         |

Luna 新聞請求未指定推理強度時預設 high；可經現有 extra body 明確覆寫。啟用推理時不送 `temperature`。completion 上限包括推理 tokens，不代表可見文字長度。清除舊 `thinking`、`max_tokens` 與 session header；不要把 Go 的參數帶到 OpenAI。

production Secrets：

- `DAILY_INSIGHTS_NEWSROOM_OPENAI_API_KEY`：新聞專用。
- `DAILY_INSIGHTS_CHAT_OPENAI_API_KEY`：聊天專用。

兩把金鑰在 OpenAI「晨報智能體」專案建立，皆不過期，權限限於 Chat Completions Request。release 依新聞端點／聊天 provider 選用對應的新 Secret，部署仍傳入原有 runtime 金鑰名稱。缺少新金鑰不得退回舊金鑰。金鑰只交付持有該功能的服務，勿寫入程式碼、文件、指令參數或日誌。

舊 `DAILY_INSIGHTS_NEWS_MODEL_API_KEY`、`DAILY_INSIGHTS_CHAT_MODEL_API_KEY` 保留。embedding Secret 保持原樣。新 Secret 不能替代 embedding key，因為它沒有 Embeddings 權限。

## 上線與驗收

1. 保存當前非機密 Variables、映像 digest 與 GitHub release run；舊金鑰維持既有 Secret。
2. 發布相容新舊供應商的程式之前，先將舊端點及四個新聞 model 明確設為正式變數，避免新預設提前切換。聊天既有明確設定繼續沿用。新 tuning 先保留原逾時及 token 預算。
3. 以隔離虛構資料驗證四個新聞階段各一筆、聊天首輪及延續對話、取消；不連線正式資料庫、不發布新聞。驗證 schema、繁中分析、英文翻譯、request ID、model、usage 與完成狀態。
4. CI、完整測試、建置與部署契約通過後，經遠端 PR 合併並部署相容版本。不在本地 merge main。
5. 避開台北時間 08:00 精選窗口，設定上表 Variables，執行 main 的 release workflow。必須重新建立 API、newsroom-worker 及 orchestration-worker；單純 restart 不會重讀設定。
6. 觀察 15 分鐘服務健康、佇列、LLM request ID、模型與錯誤碼；驗證使用者聊天可正常完成／取消。若功能原本停用，以隔離驗證取代啟用正式功能。

新聞單次 HTTP 呼叫有整體逾時；工作租約涵蓋修復回合與 embedding 呼叫及 60 秒緩衝。聊天每 15 秒送 SSE 註解保活，不影響既有 delta/done/error 事件；空白、拒絕、截斷或中斷回應不會記為成功。

```sql
SELECT stage, model, error_code, count(*), max(request_id) AS sample_request_id
FROM newsroom_llm_calls
WHERE created_at > now() - interval '15 minutes'
GROUP BY stage, model, error_code
ORDER BY stage, error_code NULLS FIRST;
```

煙霧測試只證明功能與部署可用，不代表已驗證完整歷史選稿品質。

## 回復

若煙霧測試失敗、持續出現認證／設定錯誤或佇列不再推進，恢復先前的端點、四個模型、聊天 provider、extra body、session header、逾時及 token 預算，重新執行 release。舊 provider 仍使用原請求格式與原 Secret；必要時退回先前映像 digest。

不要直接回復至已失效的 DeepSeek 金鑰：切換前須確認舊供應商仍可用。若舊供應商也不可用，停用受影響功能並修復供應商設定，不將新金鑰送到舊端點。

只對這次切換後受影響的資料列依 stage、錯誤碼及時間界線重新排程，先 dry run，不重算既有新聞或 embedding。

## 驗證記錄

2026-10-30：兩把專用金鑰已安全存入 GitHub production Secrets，`gpt-6-luna` 的新聞 high 與聊天 medium 最小 API 探測皆回傳 200。四個新聞階段（初篩、精選、分析、翻譯）及聊天兩輪、取消均已通過真實 API 煙霧測試，測試資料為虛構，未連線正式資料庫。新增遷移測試 31 項通過；格式、lint、型別、API client、建置與部署契約通過。完整測試通過：Web 332、API client 31、API 1,755。正式切換結果須於完成後更新本節。

切換前成功 release：`40873979866`（main `37a0707`）；API digest `sha256:8e668eecfe9b8d6958032e8ce671e421bb664edd7ff53f284019db4f8ca8bab2`，Web digest `sha256:1d206df4ae5247fd29d8d3a351247877c0c8a1cad1fb996117c3bd1eb20d9f31`。已明確固定舊新聞端點與四個模型、新聞逾時 90 秒、聊天 token 上限 4096，保留相容版本先上線的切換順序。

參考：[模型規格](https://developers.openai.com/api/docs/models/gpt-6-luna)、[推理與請求遷移](https://developers.openai.com/api/docs/guides/latest-model)、[Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。
