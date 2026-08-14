# TradingView → Flatrade Webhook Bridge

Replaces your Railway (US) server with a **Singapore-region** FastAPI server.
Expected order execution time: **300–800 ms** instead of 5–6 seconds.

---

## Files
| File | Purpose |
|---|---|
| `main.py` | FastAPI server — the entire bridge logic |
| `requirements.txt` | Python dependencies |
| `render.yaml` | One-click Render deployment config |

---

## Option A — Deploy on Render (FREE, recommended)

### Step 1 — Push to GitHub
```bash
git init
git add main.py requirements.txt render.yaml
git commit -m "flatrade bridge"
git remote add origin https://github.com/YOUR_USERNAME/flatrade-bridge.git
git push -u origin main
```

### Step 2 — Create Render account
Go to https://render.com → sign up free.

### Step 3 — New Web Service
1. Click **New → Web Service**
2. Connect your GitHub repo
3. Render auto-detects `render.yaml` — confirm the settings:
   - **Region: Singapore** ← critical
   - **Plan: Free**
4. Add these environment variables in the Render dashboard:
   | Key | Value |
   |---|---|
   | `FLATRADE_API_KEY` | your Flatrade API key |
   | `FLATRADE_API_SECRET` | your Flatrade API secret |
   | `WEBHOOK_SECRET` | any random string e.g. `mySecretXYZ123` |
5. Click **Deploy**

### Step 4 — Get your URL
After deploy, Render gives you:
```
https://tv-flatrade-bridge.onrender.com
```

### Step 5 — Update TradingView alert
Set your webhook URL to:
```
https://tv-flatrade-bridge.onrender.com/webhook?secret=mySecretXYZ123
```
The JSON body sent by your Pine Script alert remains exactly the same:
```json
{"action":"BUY","symbol":"NIFTY18AUG26C24450","qty":65,"type":"MKT","product":"I","exchange":"NFO"}
```

---

## Option B — Deploy on Fly.io (FREE, even faster — Mumbai region available)

### Install flyctl
```bash
curl -L https://fly.io/install.sh | sh
fly auth signup
```

### Deploy
```bash
# In your project folder:
fly launch --name tv-flatrade-bridge --region bom   # bom = Mumbai!
fly secrets set FLATRADE_API_KEY=your_key
fly secrets set FLATRADE_API_SECRET=your_secret
fly secrets set WEBHOOK_SECRET=mySecretXYZ123
fly deploy
```

Fly.io has a **Mumbai (bom) region** — this gets you even closer to Flatrade servers.
Your URL will be: `https://tv-flatrade-bridge.fly.dev`

---

## Keeping the free server awake (important!)

Render free tier sleeps after 15 min of inactivity. Use a free cron pinger:

1. Go to https://cron-job.org (free)
2. Create a job: `GET https://tv-flatrade-bridge.onrender.com/health`
3. Schedule: every 10 minutes, 09:00–15:30 IST weekdays only

This keeps the server warm so the first webhook of the day doesn't hit a cold-start delay.

---

## Refresh token before market open

Call this URL once at 9:00 AM IST (add to cron-job.org as a second job):
```
GET https://tv-flatrade-bridge.onrender.com/refresh-token?secret=mySecretXYZ123
```
Schedule: 9:00 AM IST, weekdays only.

---

## Test your server

```bash
curl -X POST "https://tv-flatrade-bridge.onrender.com/webhook?secret=mySecretXYZ123" \
  -H "Content-Type: application/json" \
  -d '{"action":"BUY","symbol":"NIFTY18AUG26C24450","qty":65,"type":"MKT","product":"I","exchange":"NFO"}'
```

Expected response:
```json
{"status": "ok", "action": "BUY", "flatrade": {"stat": "Ok", "norenordno": "..."}}
```

---

## Latency comparison

| Route | Region | Approx latency |
|---|---|---|
| Old: Railway → Flatrade | US → India | 5–6 seconds |
| New: Render → Flatrade | Singapore → India | 300–800 ms |
| Best: Fly.io → Flatrade | Mumbai → India | 50–200 ms |

---

## Troubleshooting

**"403 Invalid secret"** — your `WEBHOOK_SECRET` env var doesn't match the `?secret=` in your URL.

**"Token not found in response"** — double check `FLATRADE_API_KEY` and `FLATRADE_API_SECRET`. The login hash is `SHA256(api_key + api_secret)`.

**Server sleeping (first alert slow)** — set up the cron-job.org pinger described above.

**Flatrade API changed** — update `FLATRADE_BASE` in `main.py` and redeploy.
