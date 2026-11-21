# 🏆 BetPredict Pro

An all-around advanced statistical match predictor and value-betting engine for European football (Premier League, LaLiga, Serie A, Bundesliga, Ligue 1) and ATP/WTA Tennis.

---

## ⚡ Quick Start

### 1. Run Locally (PC & Mobile via Wi-Fi)
```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```
- On your **PC**: Open `http://localhost:8000`
- On your **Phone** (connected to the same Wi-Fi): Open `http://<YOUR-PC-IP>:8000` (e.g. `http://192.168.1.50:8000`)
- **Install on Mobile (PWA)**: In Chrome or Safari on your phone, tap **"Add to Home Screen"** to run it as a native full-screen app!

---

## ☁️ Deploy to the Cloud (Render / Railway)

### Deploy on Render:
1. Push this repository to GitHub.
2. In Render dashboard, click **New + -> Web Service**.
3. Select your repository.
4. Set:
   - **Environment**: `Python 3` (or `Docker`)
   - **Build Command**: `pip install -r requirements.txt && python -m app.backtest football`
   - **Start Command**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Click **Deploy**. Your app will be accessible 24/7 on your custom `onrender.com` URL!

---

## 📊 Statistical Logic & Coefficients

The engine uses a **two-tier linear opinion pool**:
1. **Tier 1 (Independent Statistical Components)**:
   - **xG Poisson (Dixon-Coles)**: `43.5%` weight — Opponent & venue adjusted expected goals (~12-match half-life EWMA).
   - **Elo Rating**: `41.9%` weight — Goal-margin multiplier with +45 Elo points home advantage.
   - **Goals Poisson (Dixon-Coles)**: `14.6%` weight — Real goals scored/conceded capturing finishing efficiency.
   - **Form & Table**: Exponentially regressed recent performance.
2. **Tier 2 (Market Integration)**:
   - **Market Share**: `65%` bookmaker consensus de-vigged odds + `35%` pure statistical model.
   - **Value Bets**: Flags betting edges where `Model_Prob * Odds - 1 >= 4.0%` with quarter-Kelly bankroll staking.

---

## 🎾 Tennis Historical Data Setup
To enable surface-specific Elo and historical backtesting for Tennis:
1. Download yearly tennis Excel files from `tennis-data.co.uk` (e.g., `2024.xlsx`, `2025.xlsx`, `2026.xlsx`).
2. Place them into `data/tennis/`.
3. Run `python -m app.backtest tennis` to train and calibrate tennis weights.
