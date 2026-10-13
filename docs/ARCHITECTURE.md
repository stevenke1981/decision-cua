# 架構說明

## 為什麼不是「把截圖丟給模型」

Microsoft-Decision-1 是**決策模型**：輸入 `state`（文字 / JSON）＋ 一組有固定選項的問題，
輸出每個選項的校準機率。它不生成文字、不輸出座標，而且在 OpenRouter 上是**純文字輸入**
（不在支援圖片的 Decisions 模型清單中）。

所以本專案把「computer use」拆成三種能力，各交給最適合的元件：

| 能力 | 誰負責 | 為什麼 |
|---|---|---|
| 看螢幕 | **Windows UI Automation**（程式碼） | 直接拿到每個控制項的角色、名稱、值、座標、焦點，比 OCR / 視覺定位更準、零 token |
| 決定下一步 | **任何聊天 LLM**（planner） | 只需輸出「點儲存按鈕」這種意圖，不必會看圖、不必會算座標 |
| 意圖 → 元件、危險判斷、成功驗證 | **Decision-1** | 有界的判斷題，校準機率可直接拿來設門檻；~0.2 秒、每百萬 input token $0.042 |
| 座標、按鍵、計算、diff | **程式碼** | 決策模型不做計算；這些本來就該是確定性的 |

## 一個動作的完整流程

```
planner: click(target="儲存按鈕", expect="出現另存新檔對話框")
   │
   ├─ snapshot()            UIA 走訪前景視窗（+ 選單/下拉 popup），穩定 id（IdRegistry）
   │
   ├─ grounding             choice：候選元件 e1..eN + none   （>20 個時分組並行 → 決賽）
   ├─ risk gate             noul：以實際元件標籤／值／視窗判斷是否難以復原 / 有外部後果
   │
   ├─ grounding p < 0.55    → 回傳 [ambiguous] + 前幾名候選與機率，不動作
   ├─ risk p ≥ 0.5          → CLI 問人 / MCP 回 [needs_confirmation]
   │
   ├─ 確認前景 → 重查 revision；元件消失則停止，不沿用舊座標
   ├─ 動作                  SetCursorPos + SendInput（或 UIA Invoke / ValuePattern）
   ├─ settle → snapshot()   → diff（程式碼算：開關了哪些視窗、值變化、元件增減）
   └─ verify                noul：expected outcome 是否出現 → [done] / [unverified]
```

另外兩個決策點：

- **observe() 時的 prompt-injection 掃描**：把畫面上 ≥12 字元的文字交給 noul 判斷「是否在對 AI 下指令」，
  結果以內容雜湊快取，同一畫面只問一次。
- **done() 時的完成度檢查**：planner 宣稱完成時，用目前畫面獨立判斷；低於門檻最多退回兩次。

## 每個動作的 Decision 呼叫數

| 動作 | 呼叫 | 延遲（P50 ≈ 0.23s/次，OpenRouter 頁面數據） |
|---|---|---|
| 以 id 點擊 | risk + verify = 2 | ~0.5s |
| 以描述點擊（≤20 候選） | grounding → risk + verify = 3 | ~0.75s（風險依實際元件判斷） |
| 以描述點擊（60 候選） | 3 組並行 + 決賽 + risk + verify = 6 | ~1s |
| hotkey / 無目標 type | risk + verify = 2 | ~0.5s |

## 門檻怎麼調

決策模型的機率是**校準過**的，但各 App 的 UI 命名風格差很多，所以門檻要實測：

```powershell
dcua ground "工具列上的儲存按鈕"
dcua ground "檔名輸入框" --action type
```

它只做判斷、不送出任何輸入，會印出選中的元件、完整排名、機率與 risk p。
找幾個明確案例、一個模糊案例、一個畫面上不存在的目標，看機率分佈後再調 `.env`。

## 與其他方案的取捨

- **純視覺 computer-use 模型**（看截圖輸出座標）：能處理沒有 accessibility 的畫面（遊戲、
  Canvas、遠端桌面），但貴、慢、且需要特定模型。本專案在這類畫面上會失效——UIA 樹是空的。
  需要時可開 `DCUA_PLANNER_VISION=true` 讓有視覺能力的 planner 看帶 id 標記的截圖輔助判斷，
  但點擊仍以 UIA 元件為準。
- **讓 planner 自己從完整元件清單挑 id**：可行（直接傳 `e12` 就會跳過 grounding），但每一步
  都要把幾百個元件塞進大模型 context。本專案預設只給 planner 前 60 個，其餘交給便宜的
  Decision-1 搜尋，大模型 token 用量大幅下降。

## 已知限制

- 前述 API 延遲為舊基準的流程估算，本輪安全回歸未呼叫付費 API。
- 文字含換行或 `submit=true` 時採固定確認閘門。可讀欄位的非送出型輸入還會比對值；
  遮罩、截斷、未讀到值或模型驗證不可用時回 `unverified`。詳細測試範圍見 [SAFETY_VALIDATION.md](SAFETY_VALIDATION.md)。

- 以系統管理員身分執行的視窗：Windows UIPI 會擋下一般權限程式的輸入，需以系統管理員執行 dcua。
- UIA 支援差的 App（部分遊戲、自繪 UI、某些 Electron 設定）元件樹會很貧乏。
- Chromium/Electron 會在偵測到 UIA client 時才建立 accessibility 樹，第一次 observe 可能較空，再 observe 一次即可。
- Linux backend（AT-SPI + xdotool）為實驗性質，Wayland 下大多數 App 不接受合成輸入。
