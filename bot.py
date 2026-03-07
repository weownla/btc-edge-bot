"""
BTC Hourly Edge Detector — Telegram Alert Bot
================================================
Receives TradingView webhook alerts and sends formatted
notifications to your Telegram chat.

Deploy on: Railway, Render, Fly.io, VPS, or any server with a public URL.

Requirements: pip install flask requests python-dotenv
"""

import os
import json
import time
import logging
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import requests
import math

load_dotenv()

# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "your-secret-key-change-me")
PORT = int(os.getenv("PORT", 5000))

# Prediction market quick links (update with your actual market URLs)
POLYMARKET_BASE = "https://polymarket.com"
KALSHI_BASE = "https://kalshi.com"

# ═══════════════════════════════════════════════════════════════════
# APP SETUP
# ═══════════════════════════════════════════════════════════════════

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Track recent signals to avoid duplicates
recent_signals = {}


def send_telegram(message: str, parse_mode: str = "HTML") -> bool:
    """Send a message to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info("Telegram message sent successfully")
            return True
        else:
            logger.error(f"Telegram API error: {resp.status_code} — {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return False


def format_signal_message(data: dict) -> str:
    """
    Finds the underdog $250 strike that's at least $150 away in the
    signal direction. That's where the 10x+ contracts live.
    """
    direction = data.get("dir", "UNKNOWN")
    score = data.get("score", 0)
    price = data.get("price", 0)
    minute = data.get("min", 0)
    
    mins_remaining = 60 - minute
    fires = "🔥" * min(score, 5)
    
    if direction == "BULL":
        emoji = "🟢"
        arrow = "UP"
        # Next $250 line ABOVE price, at least $150 away
        target = int(math.ceil(price / 250) * 250)
        if target - price < 150:
            target += 250
        dist = target - price
        bet_line = f"YES on ${target:,}+ (${dist:,.0f} to go)"
        
    elif direction == "BEAR":
        emoji = "🔴"
        arrow = "DOWN"
        # Next $250 line BELOW price, at least $150 away
        target = int(math.floor(price / 250) * 250)
        if price - target < 150:
            target -= 250
        dist = price - target
        bet_line = f"NO on ${target:,}+ (${dist:,.0f} to go)"
        
    else:
        emoji = "⚪"
        arrow = "?"
        target = 0
        dist = 0
        bet_line = "CHECK MANUALLY"
    
    msg = f"""
{emoji}{emoji}{emoji} <b>BTC heading {arrow}</b> {emoji}{emoji}{emoji}

<b>Now:</b> ${price:,.0f}
<b>Target:</b> ${target:,} (${dist:,.0f} away)
<b>Score:</b> {score}/5 {fires}
<b>Time:</b> {mins_remaining} min left

🎯 <b>{bet_line}</b>

<a href="{KALSHI_BASE}">Open Kalshi</a>  •  <a href="{POLYMARKET_BASE}">Polymarket</a>
"""
    return msg.strip()


def is_duplicate(data: dict) -> bool:
    """Prevent duplicate signals within the same hour."""
    key = f"{data.get('dir', '')}_{data.get('min', 0)}_{int(data.get('price', 0) / 100)}"
    now = time.time()
    
    # Clean old entries (older than 1 hour)
    for k in list(recent_signals.keys()):
        if now - recent_signals[k] > 3600:
            del recent_signals[k]
    
    if key in recent_signals:
        return True
    
    recent_signals[key] = now
    return False


# ═══════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def home():
    """Health check endpoint."""
    return jsonify({
        "status": "running",
        "service": "BTC Hourly Edge Alert Bot",
        "version": "2.1",
        "timestamp": datetime.now(timezone.utc).isoformat()
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Receive TradingView webhook alerts.
    
    TradingView sends the alert message as the raw POST body.
    Our Pine Script sends JSON, so we parse it.
    """
    # Optional: verify webhook secret via query param or header
    secret = request.args.get("secret", "") or request.headers.get("X-Webhook-Secret", "")
    if WEBHOOK_SECRET != "your-secret-key-change-me" and secret != WEBHOOK_SECRET:
        logger.warning(f"Unauthorized webhook attempt from {request.remote_addr}")
        return jsonify({"error": "unauthorized"}), 401
    
    # Parse the incoming data
    try:
        # TradingView sends alert text as raw body
        raw_body = request.get_data(as_text=True)
        logger.info(f"Received webhook: {raw_body[:200]}")
        
        # Try to parse as JSON (our Pine Script sends JSON alerts)
        try:
            data = json.loads(raw_body)
        except json.JSONDecodeError:
            # If not JSON, wrap it as a simple text alert
            data = {"raw": raw_body, "dir": "UNKNOWN", "score": 0, "price": 0, "min": 0}
        
        # Check for duplicates
        if is_duplicate(data):
            logger.info("Duplicate signal — skipping")
            return jsonify({"status": "duplicate_skipped"}), 200
        
        # Format and send
        message = format_signal_message(data)
        success = send_telegram(message)
        
        if success:
            return jsonify({"status": "sent", "direction": data.get("dir")}), 200
        else:
            return jsonify({"status": "telegram_error"}), 500
            
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/test", methods=["GET"])
def test_alert():
    """Send a test alert to verify Telegram is working."""
    test_data = {
        "dir": "BULL",
        "score": 5,
        "price": 67432.50,
        "min": 53,
        "atr_ratio": 1.87,
        "roc": 0.312,
        "rsi": 64.8,
        "strike": 67500,
        "dist": 67.50
    }
    
    message = format_signal_message(test_data)
    message = "🧪 <b>TEST ALERT</b> 🧪\n\n" + message
    success = send_telegram(message)
    
    if success:
        return jsonify({"status": "test_sent", "message": "Check your Telegram!"}), 200
    else:
        return jsonify({"status": "failed", "message": "Check your bot token and chat ID"}), 500


@app.route("/health", methods=["GET"])
def health():
    """Detailed health check."""
    telegram_ok = False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe"
        resp = requests.get(url, timeout=5)
        telegram_ok = resp.status_code == 200
    except Exception:
        pass
    
    return jsonify({
        "status": "healthy",
        "telegram_connected": telegram_ok,
        "bot_token_set": TELEGRAM_BOT_TOKEN != "YOUR_BOT_TOKEN_HERE",
        "chat_id_set": TELEGRAM_CHAT_ID != "YOUR_CHAT_ID_HERE",
        "uptime_signals_tracked": len(recent_signals)
    })


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logger.info(f"Starting BTC Hourly Edge Alert Bot on port {PORT}")
    logger.info(f"Webhook URL will be: http://YOUR_SERVER:{PORT}/webhook")
    logger.info(f"Test URL: http://YOUR_SERVER:{PORT}/test")
    
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.warning("⚠️  TELEGRAM_BOT_TOKEN not set! Create a .env file or set environment variable.")
    if TELEGRAM_CHAT_ID == "YOUR_CHAT_ID_HERE":
        logger.warning("⚠️  TELEGRAM_CHAT_ID not set! Create a .env file or set environment variable.")
    
    app.run(host="0.0.0.0", port=PORT, debug=False)
