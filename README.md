# Claude Trader — 90-Day Paper Trading Experiment

A standalone system where Claude analyzes pre-market news catalysts each morning and places bracket orders on an **Alpaca paper account**. No real money, ever. The deliverable is the decision dataset, not the P&L.

**Strategy (frozen):** Pre-market catalyst interpretation. Claude reads the news behind the morning's biggest gappers, picks at most 3 with substantive catalysts, enters long at the open with a 4% stop / 8% target bracket, and everything flattens by the close. Rules live in `config.py` and do not change for 90 days.

---


## Setup — do this tonight (~30 minutes)

### Step 1: Get an Alpaca paper account (free)

1. Go to https://alpaca.markets and sign up. You do NOT need to fund anything — paper trading is free and comes with $100k fake money.
2. In the dashboard, make sure you're toggled to **Paper** (top-left switcher).
3. Go to the API keys section on the paper dashboard and generate a key pair. Copy both the Key ID and Secret.

### Step 2: Get an Anthropic API key

1. Go to https://console.anthropic.com, create an account, add a small amount of credit ($5 will last months at one call per day).
2. Create an API key under Settings → API Keys.

### Step 3: Set up the project

```bash
# clone or copy this folder somewhere, then:
cd claude-trader
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Step 4: Set your keys as environment variables

Never hardcode keys (you learned this one on Bridgeway). Create a file called `.env.sh` (and add it to `.gitignore` if you push this to GitHub):

```bash
export ALPACA_API_KEY="your_paper_key_id"
export ALPACA_SECRET_KEY="your_paper_secret"
export ANTHROPIC_API_KEY="your_anthropic_key"
```

Then before each run: `source .env.sh`

On Windows PowerShell instead:
```powershell
$env:ALPACA_API_KEY="..."
$env:ALPACA_SECRET_KEY="..."
$env:ANTHROPIC_API_KEY="..."
```

### Step 5: Test it tonight (markets closed is fine)

```bash
python -c "import executor; print(executor.get_account()['equity'])"
```

If that prints `100000` (or similar), your Alpaca connection works.

```bash
python scanner.py
```

This prints candidate gappers (results will be stale/empty after hours — that's expected; the point is that it runs without errors).

---

## Daily routine

### Morning (8:30am ET, before the 9:30 open)

```bash
source .env.sh
python run_morning.py
```

That's the whole thing. It scans, asks Claude, places paper brackets, logs everything, and exits. The brackets manage themselves — do not touch positions intraday, that's the experiment design.

### After the close (optional, 2 minutes)

```bash
python review.py
```

Shows equity, win rate, and realized P&L so far.

### Fully automated via GitHub Actions (recommended — no laptop required)

The repo includes `.github/workflows/morning-run.yml`, which runs the pipeline automatically every trading weekday morning on GitHub's servers. Setup:

1. Create a **private** GitHub repo (e.g. `claude-trader`) and push this folder to it:
   ```bash
   cd claude-trader
   git init && git add . && git commit -m "initial"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/claude-trader.git
   git push -u origin main
   ```
2. In the repo on github.com: **Settings → Secrets and variables → Actions → New repository secret**. Add three secrets with these exact names:
   - `ALPACA_API_KEY`
   - `ALPACA_SECRET_KEY`
   - `ANTHROPIC_API_KEY`
3. Go to the **Actions** tab, enable workflows, open "Morning Trading Run", and click **Run workflow** once to test it manually. Green check = you're done.

From then on it runs itself every weekday around 8:10–8:25am ET, skips market holidays automatically (it checks Alpaca's trading calendar), and **commits the day's decision logs back to the repo** — so your entire experiment history is versioned on GitHub and you can read each morning's decisions from your phone.

Notes:
- GitHub cron is in UTC and can lag 5–15 minutes at peak times; the schedule is set early (12:10 UTC) so there's buffer before the 9:30 open.
- **Daylight saving:** when clocks fall back in November, edit the cron line in both workflow files (instructions are commented inline). This is the one manual maintenance task.
- An `evening-review.yml` workflow also runs `review.py` after each close; read its output in the Actions tab.

### Manual fallback

You can always run it yourself: `source .env.sh && python run_morning.py` — or trigger the workflow from the Actions tab (works from the GitHub mobile app too).

---

## The rules of the experiment

1. **No config changes for 90 days.** A bad week is data, not a bug.
2. **No manual trades in the paper account.** It contaminates the dataset.
3. **No intraday intervention.** The brackets are the exit strategy.
4. **Log everything.** `logs/decisions.jsonl` captures every Claude decision including rejections — this is the actual research output.
5. **Paper only.** The executor is hardcoded to the paper endpoint. Leave it that way.

## What the guardrails do (outside the model)

- Position sizing is computed by `executor.py`, never by Claude — 1% risk per trade, 15% max position.
- Max 3 trades/day, max 3 open positions.
- Daily loss halt at -3%: no new trades that day.
- Hallucination guard: if Claude names a ticker that wasn't in the candidate list, the trade is skipped and logged.
- JSON parse failure = no trades that day (fail safe, not fail open).

## Known limitations (write these in your final writeup)

- Paper fills are optimistic — no real slippage or spread cost. Haircut results mentally by a few percent per trade.
- 90 days ≈ one market regime. Results don't generalize automatically.
- LLM decisions are non-deterministic — same setup may get different answers on different days.
- Alpaca's free IEX data feed is thinner than the SIP feed; pre-market prices can be imprecise.

## File map

```
config.py        — frozen strategy rules (the constitution)
scanner.py       — pulls pre-market gappers + news (Alpaca)
analyst.py       — Claude API call, structured JSON decision
executor.py      — sizing + bracket orders (paper endpoint only)
trade_logger.py  — decision + trade + equity logging
run_morning.py   — daily entry point
review.py        — performance stats
logs/            — decisions.jsonl, trade_log.csv, equity_curve.csv
```
