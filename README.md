# RFx Monitor

Daily digest of traffic/civil engineering procurement opportunities
(SAM.gov, NYSDOT, NYSCR, NYC CROL, NJDOT, MTA C&D, Nassau, PANYNJ Bonfire,
Suffolk Bonfire, NYC PASSPort, 9 Suffolk towns), with optional LLM
relevance tagging via OpenRouter.

## Run it now, from anywhere
**Actions tab -> "RFx Daily Digest" -> Run workflow.**
Works from the GitHub mobile app too. Also runs automatically at 7:00 AM ET.

## Edit from anywhere
Press `.` in this repo for a browser VS Code (github.dev), or any file ->
pencil icon. Commit; the next run picks it up.

## Files
| File | Purpose |
|---|---|
| `rfx_alert.py` | Main script: sources, dedupe, NEW-tracking, LLM hook, email |
| `rfx_common.py` | Shared keyword filter — tune keywords HERE, once |
| `rfx_playwright_sources.py` | Bonfire / Suffolk Bonfire / PASSPort (headless Chromium) |
| `rfx_suffolk_towns.py` | 9 Suffolk town scrapers |
| `rfx_llm_filter.py` | Optional AI relevance pass (tag/drop modes, fails open) |
| `rfx_seen.json` | Already-seen memory (auto-committed back after each run) |
| `run_rfx.bat` | Local Windows launcher (unused by Actions) |

## Secrets (Settings -> Secrets and variables -> Actions)
Required: `SAM_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `RFX_RECIPIENT`
Optional: `OPENROUTER_API_KEY` (LLM pass skips itself if absent)

## Known caveat: datacenter IP blocking
Bonfire and PASSPort aggressively block datacenter IPs, and GitHub Actions
runs on Azure datacenter IPs. Those sources may return 0 in cloud runs even
though they work from a home connection. Compare the first Actions log to a
local run. If they're blocked, the practical options are:
1. **Hybrid** — let the cloud run cover everything else daily, and keep the
   local Task Scheduler run for the portal sources.
2. **Self-hosted runner** — register your PC as the Actions runner (jobs then
   use your home IP, but your PC must be on).
3. **Residential proxy** (paid, ~$/GB) wired into the Playwright launches.

## Logs
Every run's console output is in the Actions tab — the same
`[SAM.gov] 4 matches` lines you see locally.
