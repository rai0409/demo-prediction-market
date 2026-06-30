# Demo Prediction Market Viewer

日本語表示名: 予想マーケット・デモビューア

このアプリはデモ用の予想マーケットビューアです。
表示される市場データは参考情報であり、投資・賭博・取引の推奨ではありません。
アプリ内のデモポイントは無償のシミュレーション専用ポイントです。
デモポイントは購入・換金・出金・譲渡・外部ポイント交換・暗号資産交換・景品交換ができません。
このアプリはPolymarketへの注文、ウォレット接続、入金、出金、売買を行いません。

## Overview

Demo Prediction Market Viewer is a local FastAPI MVP. It shows public Polymarket-style market data when live mode is enabled, otherwise it uses bundled sample fallback data. Users can press `予想する` and `デモ参加する` to record local simulated positions with free `デモポイント`.

## Safety Boundary

Intentionally not implemented:

- real orders or real trading
- wallet connection
- deposit, withdrawal, or cashout functionality
- conversion to money, crypto, gifts, or external points
- paid point purchase
- private keys, seed phrases, API keys, API secrets, or passphrases
- authenticated user WebSocket channels
- automated trading

## Setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Run In Sample Mode

```bash
DEMO_PREDICTION_LIVE=0 python -m uvicorn app.main:app --host 127.0.0.1 --port 8092
```

Open `http://127.0.0.1:8092`.

## Run With Optional Public Fetch

```bash
DEMO_PREDICTION_LIVE=1 python -m uvicorn app.main:app --host 127.0.0.1 --port 8092
```

Live mode uses only public market data and falls back to `data/sample_events.json` if the fetch fails. No secrets are required.

## Tests

```bash
python -m pytest tests -q
python -c "from app.main import app; print(app.title)"
```

## Demo Points

The app creates one local demo user, `local-demo-user`, with `10000` free demo points. Demo participation subtracts points locally and records simulated positions, simulated order history, and a demo point ledger.

Demo points are simulation-only and have no monetary value.
