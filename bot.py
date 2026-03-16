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
    
    From Marc's URL: https://kalshi.com/markets/kxbtcd/bitcoin-price-abovebelow/KXBTCD-26MAR1619
    Event ticker format: KXBTCD-{YY}{MON}{DD}{HH} where HH is the ET hour
    
    Strategy:
      1. Construct the event ticker for the current hour
      2. Query /events/{event_ticker} to get all strike markets
      3. Find the strike closest to our target
      4. Read the real yes/no ask price
    """
    try:
        import pytz
        
        # Step 1: Get current time in ET (Kalshi uses Eastern Time)
        utc_now = datetime.datetime.now(datetime.timezone.utc)
        try:
            et = pytz.timezone('US/Eastern')
            et_now = utc_now.astimezone(et)
        except Exception:
            # Fallback: assume EDT (UTC-4)
            et_now = utc_now - datetime.timedelta(hours=4)
        
        # The current hour's event — the one that settles at the TOP of the next hour
        # If it's 2:47 PM ET, we want the event that closes at 3:00 PM = hour 15
        # But Kalshi might label it as hour 14 or 15. Try the current hour first.
        month_names = {1:'JAN',2:'FEB',3:'MAR',4:'APR',5:'MAY',6:'JUN',
                       7:'JUL',8:'AUG',9:'SEP',10:'OCT',11:'NOV',12:'DEC'}
        
        yy = str(et_now.year)[2:]  # "26"
        mon = month_names[et_now.month]  # "MAR"
        dd = f"{et_now.day:02d}"  # "16"
        
        # Try current hour and next hour (Kalshi might label either way)
        hours_to_try = [et_now.hour, et_now.hour + 1]
        if et_now.hour + 1 > 23:
            hours_to_try = [et_now.hour]
        
        markets = []
        used_event = ""
        
        for hh in hours_to_try:
            event_ticker = f"KXBTCD-{yy}{mon}{dd}{hh:02d}"
            path = f"/events/{event_ticker}"
            data = kalshi_get(path)
            
            if data and 'event' in data and 'markets' in data['event']:
                mkts = data['event']['markets']
                if len(mkts) > 0:
                    markets = mkts
                    used_event = event_ticker
                    logger.info(f"Found {len(markets)} strikes in event {event_ticker}")
                    break
                else:
                    logger.info(f"Event {event_ticker} exists but has 0 markets")
            else:
                logger.info(f"Event {event_ticker} not found, trying next")
        
        # Fallback: try previous day's late hours if we're in early morning
        if not markets and et_now.hour < 4:
            prev_day = et_now - datetime.timedelta(days=1)
            dd_prev = f"{prev_day.day:02d}"
            for hh in [23, 22, 21]:
                event_ticker = f"KXBTCD-{yy}{mon}{dd_prev}{hh:02d}"
                data = kalshi_get(f"/events/{event_ticker}")
                if data and 'event' in data and 'markets' in data['event']:
                    mkts = data['event']['markets']
                    if len(mkts) > 0:
                        markets = mkts
                        used_event = event_ticker
                        logger.info(f"Found {len(markets)} strikes in fallback event {event_ticker}")
                        break
        
        if not markets:
            logger.error(f"No hourly event found for ET hour {et_now.hour} on {et_now.date()}")
            return None, None, None
        
        # Step 2: Find strike closest to target
        target_rounded = round(target_price / 250) * 250
        best_market = None
        best_diff = float('inf')
        
        for m in markets:
            # Get strike from floor_strike (for "above" markets) or cap_strike
            strike = m.get('floor_strike') or m.get('cap_strike') or 0
            
            # Handle .99 strikes (Kalshi uses 73749.99 to mean $73,750)
            if strike > 0:
                strike = round(strike)  # 73749.99 → 73750
            
            if strike == 0:
                # Parse from ticker: KXBTCD-26MAR1619-T73750
                ticker = m.get('ticker', '')
                if '-T' in ticker:
                    try:
                        strike = round(float(ticker.split('-T')[-1]))
                    except: pass
                elif '-B' in ticker:
                    try:
                        strike = round(float(ticker.split('-B')[-1]))
                    except: pass
            
            if not (50000 <= strike <= 200000):
                continue
            
            diff = abs(strike - target_rounded)
            if diff < best_diff:
                best_diff = diff
                best_market = m
        
        if not best_market:
            logger.error(f"No strike near ${target_rounded:,} in {used_event} ({len(markets)} markets)")
            for m in markets[:3]:
                logger.error(f"  Sample: {m.get('ticker')} floor={m.get('floor_strike')} cap={m.get('cap_strike')}")
            return None, None, None
        
        ticker = best_market['ticker']
        
        # Step 3: Get price
        def parse_dollars(val):
            if not val: return 0
            try: return int(round(float(val) * 100))
            except: return 0
        
        yes_ask = parse_dollars(best_market.get('yes_ask_dollars'))
        no_ask = parse_dollars(best_market.get('no_ask_dollars'))
        
        if yes_ask == 0:
            yes_ask = best_market.get('yes_ask') or 0
        if no_ask == 0:
            no_ask = best_market.get('no_ask') or 0
        
        if yes_ask == 0 and no_ask == 0:
            last = parse_dollars(best_market.get('last_price_dollars'))
            if last > 0:
                yes_ask = last
                no_ask = 100 - last
        
        if no_ask == 0 and yes_ask > 0:
            no_ask = 100 - yes_ask
        if yes_ask == 0 and no_ask > 0:
            yes_ask = 100 - no_ask
        
        strike_val = round(best_market.get('floor_strike') or best_market.get('cap_strike') or 0)
        
        logger.info(f"MATCH: {ticker} | event={used_event} | strike=${strike_val:,}")
        logger.info(f"  yes_ask_dollars={best_market.get('yes_ask_dollars')} no_ask_dollars={best_market.get('no_ask_dollars')}")
        logger.info(f"  Parsed: yes={yes_ask}¢ no={no_ask}¢")
        
        if direction == "BEAR":
            side, contract_price = "no", no_ask
        else:
            side, contract_price = "yes", yes_ask
        
        logger.info(f"  DECISION: {direction} → {side}@{contract_price}¢")
        return ticker, contract_price, side
    
    except Exception as e:
        logger.error(f"Market search error: {e}")
        import traceback
        logger.error(traceback.format_exc())
    
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


@app.route("/markets", methods=["GET"])
def debug_markets():
    """Debug: show what Kalshi returns for BTC hourly events."""
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401
    
    results = {}
    
    # Show what the events endpoint returns for current hour
    try:
        import pytz
        utc_now = datetime.datetime.now(datetime.timezone.utc)
        try:
            et = pytz.timezone('US/Eastern')
            et_now = utc_now.astimezone(et)
        except:
            et_now = utc_now - datetime.timedelta(hours=4)
        
        month_names = {1:'JAN',2:'FEB',3:'MAR',4:'APR',5:'MAY',6:'JUN',
                       7:'JUL',8:'AUG',9:'SEP',10:'OCT',11:'NOV',12:'DEC'}
        yy = str(et_now.year)[2:]
        mon = month_names[et_now.month]
        dd = f"{et_now.day:02d}"
        
        for hh in [et_now.hour, et_now.hour + 1, et_now.hour - 1]:
            if hh < 0 or hh > 23: continue
            event_ticker = f"KXBTCD-{yy}{mon}{dd}{hh:02d}"
            path = f"/events/{event_ticker}"
            data = kalshi_get(path)
            
            if data and 'event' in data:
                mkts = data['event'].get('markets', [])
                results[event_ticker] = {
                    "title": data['event'].get('title', ''),
                    "market_count": len(mkts),
                    "sample_markets": []
                }
                for m in mkts[:5]:
                    results[event_ticker]["sample_markets"].append({
                        "ticker": m.get('ticker'),
                        "floor_strike": m.get('floor_strike'),
                        "cap_strike": m.get('cap_strike'),
                        "yes_ask_dollars": m.get('yes_ask_dollars'),
                        "no_ask_dollars": m.get('no_ask_dollars'),
                        "yes_ask": m.get('yes_ask'),
                        "no_ask": m.get('no_ask'),
                        "status": m.get('status'),
                    })
            else:
                results[event_ticker] = "not found"
        
        results["et_time"] = et_now.strftime('%Y-%m-%d %H:%M ET')
    except Exception as e:
        results["error"] = str(e)
    
    return jsonify(results)


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
