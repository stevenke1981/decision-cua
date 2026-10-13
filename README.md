# decision-cua

**讓任何一般 LLM 都能操作 Windows 桌面。**
感知用 Windows UI Automation、規劃用你喜歡的任何聊天模型、把「意圖 → 元件」與安全判斷交給
[Microsoft-Decision-1](https://openrouter.ai/microsoft/microsoft-decision-1)（經 OpenRouter Decisions API）。

```
                 ┌──────────────────────────────────────────────┐
  你的任務 ──►   │ Planner：任何聊天模型（雲端或本地）            │
                 │ 只說「點『另存新檔』對話框裡的儲存」           │
                 └───────────────┬──────────────────────────────┘
                                 │ observe / click / type_text / hotkey ...
                 ┌───────────────▼──────────────────────────────┐
                 │ decision-cua                                 │
                 │  UIA 元件樹 ─► 文字大綱（穩定 id e1, e2…）     │
                 │  Decision-1：grounding ‖ 風險閘門 → 驗證       │
                 │  SendInput：滑鼠、鍵盤（含中日韓文字）          │
                 └──────────────────────────────────────────────┘
```

> **重要：** Microsoft-Decision-1 不會看圖、也不輸出座標。它只對「固定選項」回傳校準機率，
> 而且是純文字輸入。本專案的設計就是圍繞這點：程式碼負責看與動，Decision-1 負責判斷。
> 細節見 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 它解決什麼問題

| 一般模型做 computer use 的痛點 | 本專案的做法 |
|---|---|
| 不會看截圖 / 算不準座標 | 不需要：UIA 給出結構化元件，座標由程式碼算 |
| 沒有 function calling | `DCUA_PLANNER_PROTOCOL=json`，每回合吐一個 JSON 即可 |
| 元件太多塞爆 context | planner 只看前 60 個；其餘由 Decision-1 分組搜尋（~0.2 秒/次） |
| 點錯了不知道 | 每個動作後 diff + Decision-1 驗證，回報 `[unverified]` |
| 誤刪、誤送出 | 動作前風險判斷，高風險需人工確認 |
| 網頁/文件裡藏 prompt injection | observe 時自動掃描並警告 |
| 宣稱完成但其實沒做完 | done 時以畫面獨立檢查，最多退回兩次 |
| 畫布、自繪 ribbon 沒有元件可點 | 目標可直接給螢幕座標 `"x,y"`（取自截圖），仍經風險閘門；另有 `drag` 可畫線、拖選 |
| 判斷時的畫面和動手時不同 | `observe(format="json")` 給 `revision`；動作帶 `if_revision`，畫面變了就回 `stale`、不動作 |
| 新開的程式跑到背景，輸入送錯視窗 | `launch` 等新視窗出現並拉到前景；動作前若前景被搶走，先切回原視窗 |
| 不知道慢在哪 | 每個結果附 `timing:`（observe / decide / act / settle / verify 各段毫秒） |

## 安裝（Windows）

需要 Python 3.10+。依賴刻意壓到最少：核心只有 `httpx`，Windows 加 `uiautomation`。

```powershell
git clone <this repo> decision-cua
cd decision-cua
uv sync --extra windows --extra mcp --extra vision   # 或: pip install -e ".[windows,mcp,vision]"
copy .env.example .env                        # 填入 OPENROUTER_API_KEY
uv run dcua doctor                            # 檢查設定並實際呼叫一次 Decisions API
```

`--extra vision`（Pillow）提供截圖：給有視覺的 planner 看帶 id 標記的畫面，也是座標目標 `"x,y"` 的來源。

## 三種用法

### 1. 獨立 agent（任何模型當 planner）

```powershell
# .env 裡設 DCUA_PLANNER_MODEL=<任何 OpenRouter 模型 id>
uv run dcua run "打開記事本，寫一段自我介紹，存成 桌面\intro.txt"

# 不支援 tool calling 的模型
uv run dcua run "..." --protocol json --model <model-id>

# 本地模型（LM Studio / Ollama / llama.cpp）
#   DCUA_PLANNER_BASE_URL=http://localhost:11434/v1
#   DCUA_PLANNER_API_KEY=local
```

執行中遇到高風險動作會在終端機問你 `[y/N]`；planner 呼叫 `ask_user` 時也會在這裡問。
每次執行的完整紀錄寫在 `runs/run-*.jsonl`。

### 2. MCP server（OpenCode / Claude Code / Cline…）

客戶端自己的模型當 planner，decision-cua 提供 12 個工具：
`observe, ground, list_windows, focus_window, launch, click, drag, type_text, hotkey, scroll, wait, screenshot`。

- `ground(target)`：只判斷不動作。回 JSON：選中的元件、候選與機率、risk、是否需確認、`revision`。
- `observe(format="json")`：元件 id、rect、值、`revision`、`observed_at`（含時區），供程式或外部審查使用。
- `click` / `type_text` / `hotkey` / `drag` 皆可帶 `if_revision`，把動作綁定在做決定時的畫面上。
- 切回前景後才重查 `if_revision`；無法確認前景或目標已消失時停止輸入。
- `type_text(submit=true)` 或文字含換行會要求確認，因為 Enter 可能送出表單或執行命令。
- 非送出型的文字輸入會比對可讀欄位值；值不符、被遮罩／截斷或驗證不可用時回 `unverified`。

**OpenCode** — 把 `integrations/opencode.json` 的 `mcp` 區塊併入你的 `opencode.json`。
**Claude Code** — `claude mcp add decision-cua -- uv run --directory C:\path\to\decision-cua dcua mcp`
或參考 `integrations/claude-code.mcp.json`。

建議同時把 `skills/windows-computer-use/SKILL.md` 放進你的 skills 目錄，讓 agent 知道工作流程與狀態碼的意義。

MCP 模式下沒有終端機可以問人，所以高風險動作會回 `[needs_confirmation]`，由客戶端模型去問你，
你同意後它再帶 `confirm=true` 重送。

### 3. 當函式庫用

```python
import asyncio
from dcua.config import Settings
from dcua.executor import ComputerController

async def main():
    ctrl = ComputerController(Settings.from_env())
    print(await ctrl.observe())
    r = await ctrl.click("工具列上的儲存按鈕", expect="出現另存新檔對話框")
    print(r.status, r.grounding_p, r.verified_p)
    await ctrl.aclose()

asyncio.run(main())
```

## 示範：Decision-1 玩 Pac-Man

看 Microsoft-Decision-1 每一步在合法方向間做選擇，畫面上的箭頭長短與百分比就是模型給各方向的機率。

```powershell
uv run dcua pacman                    # 視窗版，Decision-1 決策（需要 OPENROUTER_API_KEY）
uv run dcua pacman --fake             # 離線啟發式策略，不呼叫 API、不花錢
uv run dcua pacman --headless --steps 100   # 不開視窗，每步印出方向、機率、分數，最後印出成本
```

- 每步一次 `Choice` 問題：選項只列合法方向，描述附上「吃到豆子 / 最近的豆子幾步 / 最近的危險鬼幾步 / 可吃的鬼幾步」，盤面以文字送出。
- 只剩一個出口時不呼叫模型；API 失敗時該步改用啟發式策略並在畫面標示 `fallback`。
- 實測（本機）：每次決策約 550–700 ms、約 $0.00002，60 步約 $0.0012。
- 按鍵：空白鍵暫停、`r` 重來、`q` / `Esc` 離開。`--delay` 調整箭頭停留時間（預設 400 ms）。

## 除錯與調門檻

```powershell
uv run dcua windows                 # 列出視窗
uv run dcua observe                 # 前景視窗大綱（給 planner 看的就是這個）
uv run dcua observe --full --all    # 全部元件、全部視窗
uv run dcua observe --json         # 機器可讀狀態（含 revision）
uv run dcua ground "檔名輸入框" --action type   # 只判斷不動作：選中誰、排名、機率、risk、各段耗時
uv run dcua ground "640,360" --json             # 座標目標：底下是哪個元件、風險多少
```

`DCUA_BACKEND=fake` 會換成記憶體裡的假桌面（記事本 + 另存新檔），不碰真實滑鼠鍵盤，適合先試流程。

## 成本

每個動作通常 2–3 次 Decision 呼叫（先 grounding，再以實際元件判斷風險，之後驗證），每次幾百到一千多 input token。
每個動作合計約 1–3 千 token；以 $0.042 / 百萬 input token 計，**一千個動作的決策成本約 0.05–0.15 美元**，成本主要會在 planner 那邊。
`dcua run` 結束時會印出實際的 `decision_cost`。

## 專案結構

```
src/dcua/
  decisions.py        OpenRouter Decisions API client（choice / noul / score、重試、成本統計）
  backends/
    windows.py        UIA 走訪 + 選單 popup、SendInput（Unicode）、EnumWindows、DPI aware
    linux.py          實驗性：AT-SPI + xdotool
    fake.py           測試/演示用假桌面
  observe.py          穩定 id、文字大綱、diff、候選過濾
  grounding.py        意圖 → 元件（分組並行 + 決賽）
  guard.py            風險閘門、動作驗證、prompt-injection、完成度檢查
  executor.py         ComputerController：上述元件的組合；所有 OS 呼叫在單一 COM 執行緒
  agent.py            planner 迴圈（tools / json 兩種協定、context 修剪）
  tools.py            工具定義
  mcp_server.py       MCP server（相容 MCP SDK 1.x 與 2.x）
  cli.py              dcua run | observe | windows | ground | pacman | mcp | doctor
  games/
    pacman.py         Pac-Man 規則與盤面文字（無 UI，可測試）
    pacman_agent.py   Decision-1 / 啟發式策略、無視窗遊戲迴圈
    pacman_tk.py      tkinter 視窗：迷宮、HUD、機率箭頭
tests/                離線測試：模擬 Decisions API、planner、假桌面及 Win32 DLL；涵蓋安全回歸與 Pac-Man
skills/               給 agent 用的 SKILL.md
integrations/         OpenCode / Claude Code 設定範例
```

```powershell
uv run --locked --extra dev ruff check src tests
uv run --locked --extra dev --extra mcp pytest -q
```

回歸案例、Windows 測試的隔離方式與未驗限制見 [docs/SAFETY_VALIDATION.md](docs/SAFETY_VALIDATION.md)。

## 已知限制

- 以系統管理員執行的視窗需要以系統管理員執行 dcua（Windows UIPI）。
- 沒有 accessibility 樹的畫面（遊戲、Canvas、遠端桌面）只能用座標目標操作，且驗證看不到像素，結果多為 `unverified`，需另以截圖確認。
- `revision` 涵蓋畫面上的文字與值；顯示即時資訊（時鐘、游標座標）的 App 會讓它頻繁變動，`if_revision` 宜只用在關鍵步驟。
- 會真的移動滑鼠、按鍵；執行期間請勿同時操作電腦。
- Decisions API 在 OpenRouter 上仍是 alpha；回應格式若變動，只需改 `decisions.py` 的 `_parse`。
- 門檻是起始值，請用 `dcua ground` 在你常用的 App 上實測後調整。
