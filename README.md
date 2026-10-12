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

## 安裝（Windows）

需要 Python 3.10+。依賴刻意壓到最少：核心只有 `httpx`，Windows 加 `uiautomation`。

```powershell
git clone <this repo> decision-cua
cd decision-cua
uv sync --extra windows --extra mcp          # 或: pip install -e ".[windows,mcp]"
copy .env.example .env                        # 填入 OPENROUTER_API_KEY
uv run dcua doctor                            # 檢查設定並實際呼叫一次 Decisions API
```

選用：`--extra vision`（Pillow，帶 id 標記的截圖給有視覺的 planner）。

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

客戶端自己的模型當 planner，decision-cua 提供 10 個工具：
`observe, list_windows, focus_window, launch, click, type_text, hotkey, scroll, wait, screenshot`。

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

## 除錯與調門檻

```powershell
uv run dcua windows                 # 列出視窗
uv run dcua observe                 # 前景視窗大綱（給 planner 看的就是這個）
uv run dcua observe --full --all    # 全部元件、全部視窗
uv run dcua ground "檔名輸入框" --action type   # 只判斷不動作：選中誰、排名、機率、risk
```

`DCUA_BACKEND=fake` 會換成記憶體裡的假桌面（記事本 + 另存新檔），不碰真實滑鼠鍵盤，適合先試流程。

## 成本

每個動作通常 2–3 次 Decision 呼叫（grounding 與風險並行、之後驗證），每次幾百到一千多 input token。
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
  cli.py              dcua run | observe | windows | ground | mcp | doctor
tests/                20 個測試：模擬 Decisions API + 腳本化 planner + 假桌面的端到端流程
skills/               給 agent 用的 SKILL.md
integrations/         OpenCode / Claude Code 設定範例
```

```powershell
uv run --extra dev pytest -q
```

## 已知限制

- 以系統管理員執行的視窗需要以系統管理員執行 dcua（Windows UIPI）。
- 沒有 accessibility 樹的畫面（遊戲、Canvas、遠端桌面）無法操作。
- Decisions API 在 OpenRouter 上仍是 alpha；回應格式若變動，只需改 `decisions.py` 的 `_parse`。
- 門檻是起始值，請用 `dcua ground` 在你常用的 App 上實測後調整。
