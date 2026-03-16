"""
BTC Hourly Edge v4.1 — Auto-Trading Bot (Bug-Fixed)
=====================================================
6 bugs fixed from v4.0:
  1. Daily spend now tracked on ALL bets (not just failures)
  2. Private key newline handling for Railway env vars
  3. /stop and /start require authentication
  4. Series ticker configurable via env var
  5. Private key cached after first load
  6. Kalshi API path signing uses full path per docs

Requirements: pip install flask requests python-dotenv cryptography
"""

import os
import csv
import json
import uuid
import time
import math
import base64
import logging
import datetime
import re
from pathlib import Path
from urllib.parse import urlparse
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import requests as http_requests

load_dotenv()

# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "your-secret-key-change-me")
PORT = int(os.getenv("PORT", 5000))
LOG_DIR = os.getenv("LOG_DIR", "logs")

# Kalshi
KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "")
KALSHI_PRIVATE_KEY_RAW = os.getenv("KALSHI_PRIVATE_KEY", "")
KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
KALSHI_SERIES_TICKER = os.getenv("KALSHI_SERIES_TICKER", "KXBTCD")  # FIX #4: configurable
AUTO_TRADE = os.getenv("AUTO_TRADE", "false").lower() == "true"

# Safety Guardrails
MAX_BET_DOLLARS = int(os.getenv("MAX_BET_DOLLARS", "10"))
MAX_CONTRACT_CENTS = int(os.getenv("MAX_CONTRACT_CENTS", "5"))
DAILY_LOSS_LIMIT = int(os.getenv("DAILY_LOSS_LIMIT", "50"))

KALSHI_BASE = "https://kalshi.com"
POLYMARKET_BASE = "https://polymarket.com"

# ═══════════════════════════════════════════════════════════════════
# APP SETUP
# ═══════════════════════════════════════════════════════════════════

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

recent_signals = {}
daily_spend = {"date": "", "amount": 0}  # FIX #1: renamed to "spend" — tracks ALL bets
kill_switch = False

# FIX #5: Cache the private key after first load
_cached_private_key = None


# ═══════════════════════════════════════════════════════════════════
# KALSHI API CLIENT
# ═══════════════════════════════════════════════════════════════════

def load_kalshi_key():
    """Load and cache the RSA private key."""
    global _cached_private_key
    if _cached_private_key is not None:
        return _cached_private_key  # FIX #5: return cached key

    if not KALSHI_PRIVATE_KEY_RAW:
        return None
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend

        # FIX #2: Railway env vars store \n as literal characters
        key_text = KALSHI_PRIVATE_KEY_RAW.replace('\\n', '\n')
        key_data = key_text.encode('utf-8')

        private_key = serialization.load_pem_private_key(
            key_data, password=None, backend=default_backend()
        )
        _cached_private_key = private_key
        logger.info("Kalshi private key loaded successfully")
        return private_key
    except Exception as e:
        logger.error(f"Failed to load Kalshi private key: {e}")
        return None


def kalshi_sign(private_key, timestamp, method, path):
    """Create RSA-PSS signature for Kalshi API."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

    # FIX #6: Sign the full path including /trade-api/v2 prefix
    # Extract the path portion from BASE_URL and prepend it
    parsed = urlparse(KALSHI_BASE_URL)
    full_path = parsed.path.rstrip('/') + '/' + path.lstrip('/')
    full_path_clean = full_path.split('?')[0]

    message = f"{timestamp}{method}{full_path_clean}".encode('utf-8')
    signature = private_key.sign(
        message,
        asym_padding.PSS(
            mgf=asym_padding.MGF1(hashes.SHA256()),
            salt_length=asym_padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode('utf-8')


def kalshi_headers(private_key, method, path):
    """Generate authenticated headers."""
    timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
    sig = kalshi_sign(private_key, timestamp, method, path)
    return {
        'KALSHI-ACCESS-KEY': KALSHI_KEY_ID,
        'KALSHI-ACCESS-SIGNATURE': sig,
        'KALSHI-ACCESS-TIMESTAMP': timestamp,
        'Content-Type': 'application/json'
    }


def kalshi_get(path):
    """Authenticated GET to Kalshi."""
    pk = load_kalshi_key()
    if not pk:
        return None
    headers = kalshi_headers(pk, "GET", path)
    url = KALSHI_BASE_URL.rstrip('/') + '/' + path.lstrip('/')
    try:
        resp = http_requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        logger.error(f"Kalshi GET {path}: {resp.status_code} {resp.text[:300]}")
    except Exception as e:
        logger.error(f"Kalshi GET error: {e}")
    return None


def kalshi_post(path, data):
    """Authenticated POST to Kalshi."""
    pk = load_kalshi_key()
    if not pk:
        return None
    headers = kalshi_headers(pk, "POST", path)
    url = KALSHI_BASE_URL.rstrip('/') + '/' + path.lstrip('/')
    try:
        resp = http_requests.post(url, headers=headers, json=data, timeout=10)
        if resp.status_code in (200, 201):
            return resp.json()
        logger.error(f"Kalshi POST {path}: {resp.status_code} {resp.text[:300]}")
    except Exception as e:
        logger.error(f"Kalshi POST error: {e}")
    return None


# ═══════════════════════════════════════════════════════════════════
# MARKET FINDING & ORDER PLACEMENT
# ═══════════════════════════════════════════════════════════════════

def find_btc_hourly_market(direction, target_price):
    """
    Find the Kalshi BTC hourly market matching our signal.
    
    Kalshi BTC hourly market structure:
      Event ticker: KXBTCD-{YY}{MON}{DD}{HH} (e.g., KXBTCD-26MAR1604 for 4PM on Mar 16)
      Market ticker: {EVENT}-T{STRIKE} (e.g., KXBTCD-26MAR1604-T72750)
    
    Strategy:
      1. Get all open KXBTCD markets
      2. Filter to ones closing in the next hour
      3. Find the strike closest to our target
      4. Return ticker, contract price, and side
    """
    try:
        # Step 1: Get all open BTC hourly markets
        series = KALSHI_SERIES_TICKER
        path = f"/markets?status=open&series_ticker={series}&limit=200"
        data = kalshi_get(path)
        
        if not data or 'markets' not in data or len(data['markets']) == 0:
            # Try without series filter
            path = "/markets?status=open&limit=200"
            data = kalshi_get(path)
            if data and 'markets' in data:
                data['markets'] = [m for m in data['markets']
                                   if series.lower() in m.get('ticker', '').lower()]
        
        if not data or 'markets' not in data or len(data['markets']) == 0:
            logger.error(f"No markets found for series {series}")
            return None, None, None
        
        markets = data['markets']
        logger.info(f"Found {len(markets)} open {series} markets")
        
        # Step 2: Find the market with the strike closest to our target
        # Among markets closing soonest (the current hour)
        best_market = None
        best_diff = float('inf')
        
        # Round target to nearest $250 (Kalshi uses $250 strikes)
        target_rounded = round(target_price / 250) * 250
        
        for m in markets:
            ticker = m.get('ticker', '')
            
            # Try to extract strike from ticker (format: ...-T{STRIKE})
            strike = 0
            if '-T' in ticker:
                try:
                    strike_str = ticker.split('-T')[-1]
                    strike = float(strike_str)
                except ValueError:
                    pass
            
            # Also check the strike fields
            if strike == 0:
                strike = m.get('floor_strike', 0) or m.get('strike_price', 0) or 0
                # Kalshi sometimes stores as cents
                if strike > 1000000:
                    strike = strike / 100
            
            if strike == 0:
                # Try parsing from subtitle/title
                title = m.get('title', '') + ' ' + m.get('subtitle', '')
                nums = re.findall(r'\$?([\d,]+)', title)
                for n in nums:
                    val = int(n.replace(',', ''))
                    if 50000 < val < 200000:
                        strike = val
                        break
            
            if strike == 0:
                continue
            
            diff = abs(strike - target_rounded)
            if diff < best_diff:
                best_diff = diff
                best_market = m
        
        if not best_market:
            logger.error(f"No market found near strike ${target_rounded:,}")
            return None, None, None
        
        ticker = best_market['ticker']
        
        # Step 3: Determine side and price
        # For BEAR: buy NO (betting price drops below strike)
        # For BULL: buy YES (betting price stays above / goes above strike)
        
        # Get prices - try multiple field names Kalshi might use
        yes_ask = best_market.get('yes_ask', 0) or best_market.get('last_price', 0) or 0
        no_ask = best_market.get('no_ask', 0) or 0
        
        # If no_ask is 0, calculate from yes_ask (yes + no = 100)
        if no_ask == 0 and yes_ask > 0:
            no_ask = 100 - yes_ask
        if yes_ask == 0 and no_ask > 0:
            yes_ask = 100 - no_ask
        
        if direction == "BEAR":
            side = "no"
            contract_price = no_ask
        else:
            side = "yes"
            contract_price = yes_ask
        
        logger.info(f"Selected: {ticker} | Strike: ${best_market.get('floor_strike', '?')} | "
                    f"{side}={contract_price}¢ | Target was ${target_rounded:,}")
        
        return ticker, contract_price, side

    except Exception as e:
        logger.error(f"Market search error: {e}")
        import traceback
        logger.error(traceback.format_exc())

    return None, None, None

    return None, None, None


def check_guardrails(contract_price_cents):
    """Check all safety limits before placing a trade."""
    global daily_spend, kill_switch

    if kill_switch:
        return False, "Kill switch is ON."

    if not AUTO_TRADE:
        return False, "AUTO_TRADE is disabled."

    if not KALSHI_KEY_ID or not KALSHI_PRIVATE_KEY_RAW:
        return False, "Kalshi API keys not configured."

    if contract_price_cents > MAX_CONTRACT_CENTS:
        return False, f"Contract {contract_price_cents}¢ > max {MAX_CONTRACT_CENTS}¢."

    if contract_price_cents <= 0:
        return False, "Contract price is 0 — no liquidity."

    today = datetime.date.today().isoformat()
    if daily_spend["date"] != today:
        daily_spend = {"date": today, "amount": 0}

    if daily_spend["amount"] >= DAILY_LOSS_LIMIT:
        return False, f"Daily spend limit ${DAILY_LOSS_LIMIT} reached."

    return True, "OK"


def place_kalshi_bet(ticker, side, contract_price_cents):
    """Place a bet with all safety checks."""
    global daily_spend

    count = int((MAX_BET_DOLLARS * 100) / contract_price_cents)
    if count <= 0:
        return False, "Can't afford any contracts."

    order_data = {
        "ticker": ticker,
        "action": "buy",
        "side": side,
        "count": count,
        "type": "limit",
        "time_in_force": "fill_or_kill",
        "client_order_id": str(uuid.uuid4()),
        "buy_max_cost": MAX_BET_DOLLARS * 100,
    }

    if side == "yes":
        order_data["yes_price"] = contract_price_cents
    else:
        order_data["no_price"] = contract_price_cents

    logger.info(f"Placing: {side.upper()} {count}x {ticker} @ {contract_price_cents}¢ "
               f"(max ${MAX_BET_DOLLARS})")

    # FIX #1: Track spend BEFORE placing — money is committed either way
    daily_spend["amount"] += MAX_BET_DOLLARS

    result = kalshi_post("/portfolio/orders", order_data)

    if result and 'order' in result:
        order = result['order']
        fill_count = order.get('fill_count', 0)
        status = order.get('status', 'unknown')
        logger.info(f"Order {order.get('order_id')}: {status}, filled {fill_count}")

        if fill_count == 0 and status != 'resting':
            # Order was killed (fill_or_kill) with no fills — refund spend tracker
            daily_spend["amount"] -= MAX_BET_DOLLARS
            return False, "Order not filled (no liquidity at this price)."

        return True, {
            "order_id": order.get('order_id'),
            "ticker": ticker,
            "side": side,
            "count": fill_count or count,
            "price_cents": contract_price_cents,
            "total_cost": (fill_count or count) * contract_price_cents,
            "status": status,
        }
    else:
        # API call failed — refund spend tracker
        daily_spend["amount"] -= MAX_BET_DOLLARS
        return False, f"API error: {result}"


# ═══════════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════════

def send_telegram(message, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message,
               "parse_mode": parse_mode, "disable_web_page_preview": True}
    try:
        resp = http_requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200
    except:
        return False


# ═══════════════════════════════════════════════════════════════════
# MESSAGE FORMATTING
# ═══════════════════════════════════════════════════════════════════

def format_signal_message(data, trade_result=None):
    direction = data.get("dir", "UNKNOWN")
    score = data.get("score", 0)
    price = data.get("price", 0)
    minute = data.get("min", 0)
    mins_remaining = 60 - minute
    fires = "🔥" * min(score, 5)

    if direction == "BULL":
        emoji = "🟢"; arrow = "UP"
        target = int(math.ceil(price / 250) * 250)
        if target - price < 150: target += 250
        dist = target - price
        bet_line = f"YES on ${target:,}+ (${dist:,.0f} to go)"
    elif direction == "BEAR":
        emoji = "🔴"; arrow = "DOWN"
        target = int(math.floor(price / 250) * 250)
        if price - target < 150: target -= 250
        dist = price - target
        bet_line = f"NO on ${target:,}+ (${dist:,.0f} to go)"
    else:
        emoji = "⚪"; arrow = "?"; target = 0; dist = 0
        bet_line = "CHECK MANUALLY"

    msg = f"""{emoji}{emoji}{emoji} <b>BTC heading {arrow}</b> {emoji}{emoji}{emoji}

<b>Now:</b> ${price:,.0f}
<b>Target:</b> ${target:,} (${dist:,.0f} away)
<b>Score:</b> {score}/5 {fires}
<b>Time:</b> {mins_remaining} min left

🎯 <b>{bet_line}</b>"""

    if trade_result:
        if trade_result.get("success"):
            info = trade_result["info"]
            msg += f"""

🤖 <b>AUTO-BET PLACED:</b>
{info['count']}x {info['side'].upper()} @ {info['price_cents']}¢
Cost: ${info['total_cost']/100:.2f}
Order: {info['order_id'][:12]}..."""
        else:
            msg += f"\n\n⚠️ {trade_result.get('reason', 'unknown')}"

    msg += f"\n\n<a href=\"{KALSHI_BASE}\">Kalshi</a>  •  <a href=\"{POLYMARKET_BASE}\">Polymarket</a>"

    # Add daily spend summary
    today_spent = daily_spend.get('amount', 0)
    if today_spent > 0:
        msg += f"\n📊 Today: ${today_spent} / ${DAILY_LOSS_LIMIT} limit"

    return msg.strip(), target, dist


# ═══════════════════════════════════════════════════════════════════
# SIGNAL LOGGING
# ═══════════════════════════════════════════════════════════════════

def log_signal(data, target, dist, trade_info=None):
    try:
        Path(LOG_DIR).mkdir(exist_ok=True)
        log_path = os.path.join(LOG_DIR, "signals.csv")
        write_header = not os.path.exists(log_path)
        with open(log_path, 'a', newline='') as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(["timestamp", "direction", "score", "price", "target",
                                 "distance", "minute", "auto_traded", "ticker",
                                 "side", "contracts", "price_cents", "outcome", "pnl"])
            writer.writerow([
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
                data.get("dir"), data.get("score"), data.get("price"),
                target, round(dist), data.get("min"),
                "yes" if trade_info else "no",
                trade_info.get("ticker", "") if trade_info else "",
                trade_info.get("side", "") if trade_info else "",
                trade_info.get("count", "") if trade_info else "",
                trade_info.get("price_cents", "") if trade_info else "",
                "", ""
            ])
    except Exception as e:
        logger.error(f"Log error: {e}")


def is_duplicate(data):
    key = f"{data.get('dir')}_{data.get('min')}_{int(data.get('price', 0) / 100)}"
    now = time.time()
    for k in list(recent_signals.keys()):
        if now - recent_signals[k] > 3600: del recent_signals[k]
    if key in recent_signals: return True
    recent_signals[key] = now
    return False


def check_auth(req):
    """FIX #3: Verify webhook secret on sensitive endpoints."""
    s = req.args.get("secret", "") or req.headers.get("X-Webhook-Secret", "")
    if WEBHOOK_SECRET == "your-secret-key-change-me":
        return True  # No secret configured — allow all
    return s == WEBHOOK_SECRET


# ═══════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "running", "version": "4.1", "auto_trade": AUTO_TRADE})


@app.route("/webhook", methods=["POST"])
def webhook():
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401

    try:
        raw = request.get_data(as_text=True)
        logger.info(f"Webhook: {raw[:200]}")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"dir": "UNKNOWN", "score": 0, "price": 0, "min": 0}

        if is_duplicate(data):
            return jsonify({"status": "duplicate"}), 200

        # ─── AUTO-TRADE ──────────────────────────────────────
        trade_result = {"success": False, "reason": "Auto-trade disabled", "info": None}

        if AUTO_TRADE:
            direction = data.get("dir")
            price = data.get("price", 0)

            if direction == "BULL":
                target = int(math.ceil(price / 250) * 250)
                if target - price < 150: target += 250
            elif direction == "BEAR":
                target = int(math.floor(price / 250) * 250)
                if price - target < 150: target -= 250
            else:
                target = 0

            ticker, contract_price, side = find_btc_hourly_market(direction, target)

            if ticker and contract_price:
                ok, reason = check_guardrails(contract_price)
                if ok:
                    success, info = place_kalshi_bet(ticker, side, contract_price)
                    trade_result = {
                        "success": success,
                        "info": info if success else None,
                        "reason": str(info) if not success else ""
                    }
                else:
                    trade_result = {"success": False, "reason": reason}
            else:
                trade_result = {"success": False,
                    "reason": f"No market found (ticker={ticker}, price={contract_price})"}

        # ─── SEND ────────────────────────────────────────────
        message, target, dist = format_signal_message(
            data, trade_result if AUTO_TRADE else None
        )
        log_signal(data, target, dist,
                  trade_result.get("info") if trade_result.get("success") else None)
        send_telegram(message)

        return jsonify({"status": "sent", "auto_traded": trade_result.get("success", False)}), 200

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/test", methods=["GET"])
def test_alert():
    test_data = {"dir": "BEAR", "score": 4, "price": 68432.50, "min": 54}
    message, _, _ = format_signal_message(test_data)
    message = "🧪 <b>TEST ALERT</b> 🧪\n\n" + message
    return jsonify({"status": "test_sent" if send_telegram(message) else "failed"})


@app.route("/balance", methods=["GET"])
def balance():
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401
    data = kalshi_get("/portfolio/balance")
    return jsonify(data) if data else jsonify({"error": "Check API keys"}), 500


@app.route("/signals", methods=["GET"])
def view_signals():
    log_path = os.path.join(LOG_DIR, "signals.csv")
    if not os.path.exists(log_path):
        return jsonify({"signals": [], "count": 0})
    signals = []
    with open(log_path) as f:
        for row in csv.DictReader(f): signals.append(row)
    return jsonify({"signals": signals[-50:], "count": len(signals)})


@app.route("/stop", methods=["GET", "POST"])
def stop_trading():
    # FIX #3: Require auth
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401
    global kill_switch
    kill_switch = True
    send_telegram("🛑 <b>KILL SWITCH ON</b> — Auto-trading halted.")
    return jsonify({"status": "stopped"})


@app.route("/start", methods=["GET", "POST"])
def start_trading():
    # FIX #3: Require auth
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401
    global kill_switch
    kill_switch = False
    send_telegram("🟢 <b>Auto-trading re-enabled.</b>")
    return jsonify({"status": "started"})


@app.route("/health", methods=["GET"])
def health():
    telegram_ok = False
    try:
        r = http_requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe", timeout=5)
        telegram_ok = r.status_code == 200
    except: pass

    kalshi_ok = False
    if KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_RAW:
        bal = kalshi_get("/portfolio/balance")
        kalshi_ok = bal is not None

    return jsonify({
        "status": "healthy", "version": "4.1",
        "telegram": telegram_ok, "kalshi_api": kalshi_ok,
        "auto_trade": AUTO_TRADE, "kill_switch": kill_switch,
        "daily_spend": daily_spend,
        "guardrails": {
            "max_bet": f"${MAX_BET_DOLLARS}",
            "max_contract": f"{MAX_CONTRACT_CENTS}¢",
            "daily_limit": f"${DAILY_LOSS_LIMIT}",
        }
    })


if __name__ == "__main__":
    Path(LOG_DIR).mkdir(exist_ok=True)
    logger.info(f"BTC Hourly Edge v4.1 — Port {PORT}")
    logger.info(f"Auto-trade: {AUTO_TRADE} | Max: ${MAX_BET_DOLLARS}/bet, "
               f"{MAX_CONTRACT_CENTS}¢ cap, ${DAILY_LOSS_LIMIT}/day limit")
    if AUTO_TRADE and not KALSHI_KEY_ID:
        logger.warning("⚠️ AUTO_TRADE on but KALSHI_KEY_ID not set!")
    app.run(host="0.0.0.0", port=PORT, debug=False)
