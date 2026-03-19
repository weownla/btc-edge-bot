"""
BTC Trend Rider v5.0 — Auto-Trading Bot
========================================
Built for volatile trending days:
  - Detects regime (ATR) + multi-hour trend → bet WITH trend
  - Finds contracts in 3-25c range (8-30x payout)
  - Late window (:50-:57) for cheapest contracts

Key fixes from v4.1:
  - Market finder searches ALL strikes for best-priced contract
  - MAX_CONTRACT_CENTS default 25 (was 8 — blocked everything)
  - Handles null yes_ask/no_ask properly  
  - Logs full POST response on failure
"""

import os, csv, json, uuid, time, math, base64, logging, datetime, re
from pathlib import Path
from urllib.parse import urlparse
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import requests as http_requests

load_dotenv()

# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "your-secret-key-change-me")
PORT = int(os.getenv("PORT", 5000))
LOG_DIR = os.getenv("LOG_DIR", "logs")

KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "")
KALSHI_PRIVATE_KEY_RAW = os.getenv("KALSHI_PRIVATE_KEY", "")
KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
AUTO_TRADE = os.getenv("AUTO_TRADE", "false").lower() == "true"

MAX_BET_DOLLARS = int(os.getenv("MAX_BET_DOLLARS", "20"))
MAX_CONTRACT_CENTS = int(os.getenv("MAX_CONTRACT_CENTS", "25"))
MIN_CONTRACT_CENTS = int(os.getenv("MIN_CONTRACT_CENTS", "3"))
DAILY_LOSS_LIMIT = int(os.getenv("DAILY_LOSS_LIMIT", "100"))

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

recent_signals = {}
daily_spend = {"date": "", "amount": 0}
kill_switch = False
_cached_private_key = None

# ═══════════════════════════════════════════════════════════════════
# KALSHI API CLIENT
# ═══════════════════════════════════════════════════════════════════

def load_kalshi_key():
    global _cached_private_key
    if _cached_private_key is not None:
        return _cached_private_key
    if not KALSHI_PRIVATE_KEY_RAW:
        return None
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend
        key_text = KALSHI_PRIVATE_KEY_RAW.replace('\\n', '\n')
        private_key = serialization.load_pem_private_key(
            key_text.encode('utf-8'), password=None, backend=default_backend()
        )
        _cached_private_key = private_key
        logger.info("Kalshi private key loaded")
        return private_key
    except Exception as e:
        logger.error(f"Key load failed: {e}")
        return None


def kalshi_sign(private_key, timestamp, method, path):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as ap
    parsed = urlparse(KALSHI_BASE_URL)
    full_path = parsed.path.rstrip('/') + '/' + path.lstrip('/')
    full_path_clean = full_path.split('?')[0]
    message = f"{timestamp}{method}{full_path_clean}".encode('utf-8')
    signature = private_key.sign(
        message, ap.PSS(mgf=ap.MGF1(hashes.SHA256()), salt_length=ap.PSS.DIGEST_LENGTH), hashes.SHA256()
    )
    return base64.b64encode(signature).decode('utf-8')


def kalshi_headers(private_key, method, path):
    timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
    sig = kalshi_sign(private_key, timestamp, method, path)
    return {
        'KALSHI-ACCESS-KEY': KALSHI_KEY_ID,
        'KALSHI-ACCESS-SIGNATURE': sig,
        'KALSHI-ACCESS-TIMESTAMP': timestamp,
        'Content-Type': 'application/json'
    }


def kalshi_get(path):
    pk = load_kalshi_key()
    if not pk: return None
    headers = kalshi_headers(pk, "GET", path)
    url = KALSHI_BASE_URL.rstrip('/') + '/' + path.lstrip('/')
    try:
        resp = http_requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        logger.error(f"GET {path}: {resp.status_code} {resp.text[:300]}")
    except Exception as e:
        logger.error(f"GET error: {e}")
    return None


def kalshi_post(path, data):
    pk = load_kalshi_key()
    if not pk: return None
    headers = kalshi_headers(pk, "POST", path)
    url = KALSHI_BASE_URL.rstrip('/') + '/' + path.lstrip('/')
    try:
        resp = http_requests.post(url, headers=headers, json=data, timeout=10)
        logger.info(f"POST {path}: status={resp.status_code} body={resp.text[:500]}")
        if resp.status_code in (200, 201):
            return resp.json()
        logger.error(f"POST FAILED: {resp.status_code} {resp.text[:500]}")
    except Exception as e:
        logger.error(f"POST error: {e}")
    return None


# ═══════════════════════════════════════════════════════════════════
# MARKET FINDING — SEARCHES ALL STRIKES FOR BEST PRICE
# ═══════════════════════════════════════════════════════════════════

def parse_cents(val):
    if not val: return 0
    try: return int(round(float(val) * 100))
    except: return 0


def get_event_tickers():
    """Build KXBTCD-{YY}{MON}{DD}{HH} candidates for current hour in ET."""
    try:
        import pytz
        et = pytz.timezone('US/Eastern')
        et_now = datetime.datetime.now(datetime.timezone.utc).astimezone(et)
    except Exception:
        et_now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=4)
    
    months = {1:'JAN',2:'FEB',3:'MAR',4:'APR',5:'MAY',6:'JUN',
              7:'JUL',8:'AUG',9:'SEP',10:'OCT',11:'NOV',12:'DEC'}
    yy = str(et_now.year)[2:]
    mon = months[et_now.month]
    dd = f"{et_now.day:02d}"
    
    tickers = []
    for hh in [et_now.hour, et_now.hour + 1]:
        if 0 <= hh <= 23:
            tickers.append(f"KXBTCD-{yy}{mon}{dd}{hh:02d}")
    if et_now.hour < 2:
        prev = et_now - datetime.timedelta(days=1)
        dd_prev = f"{prev.day:02d}"
        for hh in [23, 22]:
            tickers.append(f"KXBTCD-{yy}{mon}{dd_prev}{hh:02d}")
    return tickers, et_now


def find_best_contract(direction, btc_price):
    """
    Search ALL strikes for the best-priced contract in our direction.
    
    BULL → buy YES on a strike above current price (YES is cheap = underdog)
    BEAR → buy NO on a strike near current price (NO is cheap = underdog)
    
    Returns: (ticker, price_cents, side, strike) or (None, None, None, None)
    """
    try:
        event_tickers, et_now = get_event_tickers()
        
        all_markets = []
        used_event = ""
        for evt in event_tickers:
            data = kalshi_get(f"/markets?event_ticker={evt}&status=open&limit=100")
            if data and 'markets' in data and len(data['markets']) > 0:
                all_markets = data['markets']
                used_event = evt
                logger.info(f"Found {len(all_markets)} strikes in {evt}")
                break
        
        if not all_markets:
            logger.error(f"No markets for: {event_tickers}")
            return None, None, None, None
        
        # Parse all strikes with prices
        candidates = []
        for m in all_markets:
            strike = m.get('floor_strike') or m.get('cap_strike') or 0
            if strike > 0:
                strike = round(strike)
            if strike == 0:
                tk = m.get('ticker', '')
                for sep in ['-T', '-B']:
                    if sep in tk:
                        try: strike = round(float(tk.split(sep)[-1]))
                        except: pass
                        break
            if not (50000 <= strike <= 200000):
                continue
            
            yes_ask = parse_cents(m.get('yes_ask_dollars'))
            no_ask = parse_cents(m.get('no_ask_dollars'))
            if yes_ask == 0: yes_ask = m.get('yes_ask') or 0
            if no_ask == 0: no_ask = m.get('no_ask') or 0
            if yes_ask == 0 and no_ask == 0:
                last = parse_cents(m.get('last_price_dollars'))
                if last > 0: yes_ask = last; no_ask = 100 - last
            if no_ask == 0 and yes_ask > 0: no_ask = 100 - yes_ask
            if yes_ask == 0 and no_ask > 0: yes_ask = 100 - no_ask
            
            candidates.append({
                'ticker': m.get('ticker'), 'strike': strike,
                'yes_ask': yes_ask, 'no_ask': no_ask,
                'dist': abs(strike - btc_price),
            })
        
        if not candidates:
            logger.error(f"No valid strikes from {len(all_markets)} markets")
            return None, None, None, None
        
        # Find best contract for direction
        side = "yes" if direction == "BULL" else "no"
        price_key = 'yes_ask' if side == "yes" else 'no_ask'
        
        # Filter to contracts in our price range
        valid = [c for c in candidates if MIN_CONTRACT_CENTS <= c[price_key] <= MAX_CONTRACT_CENTS]
        
        if not valid:
            # Log what's available
            logger.error(f"No {side.upper()} in {MIN_CONTRACT_CENTS}-{MAX_CONTRACT_CENTS}c range. Available:")
            for c in sorted(candidates, key=lambda x: x['dist'])[:8]:
                logger.error(f"  ${c['strike']:,}: YES={c['yes_ask']}c NO={c['no_ask']}c")
            return None, None, None, None
        
        # Pick closest to current price (highest win probability within our price range)
        best = min(valid, key=lambda c: c['dist'])
        price = best[price_key]
        
        payout = f"{100/price:.0f}x" if price > 0 else "?"
        logger.info(f"BEST: {best['ticker']} strike=${best['strike']:,} "
                    f"{side.upper()}@{price}c ({payout}) event={used_event}")
        
        return best['ticker'], price, side, best['strike']
    
    except Exception as e:
        logger.error(f"Market finder error: {e}")
        import traceback
        logger.error(traceback.format_exc())
    return None, None, None, None


# ═══════════════════════════════════════════════════════════════════
# GUARDRAILS & ORDER PLACEMENT
# ═══════════════════════════════════════════════════════════════════

def check_guardrails(contract_price_cents):
    global daily_spend, kill_switch
    if kill_switch: return False, "Kill switch ON"
    if not AUTO_TRADE: return False, "Auto-trade disabled"
    if not KALSHI_KEY_ID: return False, "Kalshi keys not set"
    if contract_price_cents > MAX_CONTRACT_CENTS:
        return False, f"Contract {contract_price_cents}c > max {MAX_CONTRACT_CENTS}c"
    if contract_price_cents < MIN_CONTRACT_CENTS:
        return False, f"Contract {contract_price_cents}c < min {MIN_CONTRACT_CENTS}c"
    today = datetime.date.today().isoformat()
    if daily_spend["date"] != today:
        daily_spend = {"date": today, "amount": 0}
    if daily_spend["amount"] >= DAILY_LOSS_LIMIT:
        return False, f"Daily limit ${DAILY_LOSS_LIMIT} reached"
    return True, "OK"


def place_kalshi_bet(ticker, side, contract_price_cents):
    global daily_spend
    count = int((MAX_BET_DOLLARS * 100) / contract_price_cents)
    if count <= 0:
        return False, "Can't afford any contracts"
    
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
    
    logger.info(f"PLACING: {side.upper()} {count}x {ticker} @ {contract_price_cents}c "
               f"(${MAX_BET_DOLLARS} max)")
    
    daily_spend["amount"] += MAX_BET_DOLLARS
    result = kalshi_post("/portfolio/orders", order_data)
    
    if result and 'order' in result:
        order = result['order']
        fills = order.get('fill_count', 0)
        status = order.get('status', 'unknown')
        logger.info(f"Order: {order.get('order_id')} status={status} filled={fills}")
        
        if fills == 0 and status != 'resting':
            daily_spend["amount"] -= MAX_BET_DOLLARS
            return False, f"No fill (status={status})"
        
        return True, {
            "order_id": order.get('order_id', '?'),
            "ticker": ticker, "side": side,
            "count": fills or count,
            "price_cents": contract_price_cents,
            "total_cost": (fills or count) * contract_price_cents,
            "status": status,
            "payout_mult": f"{100/contract_price_cents:.0f}x"
        }
    else:
        daily_spend["amount"] -= MAX_BET_DOLLARS
        return False, f"API error: {json.dumps(result) if result else 'None'}"


# ═══════════════════════════════════════════════════════════════════
# TELEGRAM & UTILS
# ═══════════════════════════════════════════════════════════════════

def send_telegram(message, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = http_requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID, "text": message,
            "parse_mode": parse_mode, "disable_web_page_preview": True
        }, timeout=10)
        return resp.status_code == 200
    except: return False


def format_signal_message(data, trade_result=None):
    direction = data.get("dir", "?")
    score = data.get("score", 0)
    price = data.get("price", 0)
    minute = data.get("min", 0)
    atr = data.get("atr", 0)
    t4h = data.get("t4h", 0)
    mins_left = 60 - minute
    fires = "\U0001f525" * min(int(score), 5)

    if direction == "BULL":
        emoji = "\U0001f7e2"; arrow = "UP"
        target = int(math.ceil(price / 250) * 250)
        if target - price < 150: target += 250
        dist = target - price
        bet_line = f"YES on ${target:,}+"
    elif direction == "BEAR":
        emoji = "\U0001f534"; arrow = "DOWN"
        target = int(math.floor(price / 250) * 250)
        if price - target < 150: target -= 250
        dist = price - target
        bet_line = f"NO on strike near ${price:,.0f}"
    else:
        emoji = ""; arrow = "?"; target = 0; dist = 0; bet_line = "?"

    msg = f"""{emoji}{emoji}{emoji} <b>BTC TREND {arrow}</b> {emoji}{emoji}{emoji}

<b>Now:</b> ${price:,.0f}
<b>4h Trend:</b> ${t4h:+,.0f} | <b>ATR:</b> {atr:.1f}x
<b>Score:</b> {score}/5 {fires}
<b>Time:</b> {mins_left} min left

\U0001f3af <b>{bet_line}</b>"""

    if trade_result:
        if trade_result.get("success"):
            info = trade_result["info"]
            msg += f"""

\U0001f4b0 <b>AUTO-BET PLACED!</b>
{info['count']}x {info['side'].upper()} @ {info['price_cents']}c ({info['payout_mult']} payout)
Cost: ${info['total_cost']/100:.2f}
ID: {info['order_id'][:16]}"""
        else:
            msg += f"\n\n\u26a0\ufe0f {trade_result.get('reason', '?')}"

    msg += f'\n\n<a href="https://kalshi.com">Kalshi</a>'
    if daily_spend.get('amount', 0) > 0:
        msg += f"\n\U0001f4ca Today: ${daily_spend['amount']} / ${DAILY_LOSS_LIMIT}"
    return msg.strip(), target, dist


def log_signal(data, target, dist, trade_info=None):
    try:
        Path(LOG_DIR).mkdir(exist_ok=True)
        p = os.path.join(LOG_DIR, "signals.csv")
        new = not os.path.exists(p)
        with open(p, 'a', newline='') as f:
            w = csv.writer(f)
            if new:
                w.writerow(["timestamp","dir","score","price","target","dist","min",
                            "atr","t4h","traded","ticker","side","count","price_c"])
            w.writerow([
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
                data.get("dir"), data.get("score"), data.get("price"),
                target, round(dist), data.get("min"),
                data.get("atr",""), data.get("t4h",""),
                "yes" if trade_info else "no",
                trade_info.get("ticker","") if trade_info else "",
                trade_info.get("side","") if trade_info else "",
                trade_info.get("count","") if trade_info else "",
                trade_info.get("price_cents","") if trade_info else "",
            ])
    except Exception as e:
        logger.error(f"Log error: {e}")


def is_duplicate(data):
    key = f"{data.get('dir')}_{data.get('min')}_{int(data.get('price',0)/100)}"
    now = time.time()
    for k in list(recent_signals.keys()):
        if now - recent_signals[k] > 3600: del recent_signals[k]
    if key in recent_signals: return True
    recent_signals[key] = now
    return False


def check_auth(req):
    s = req.args.get("secret","") or req.headers.get("X-Webhook-Secret","")
    if WEBHOOK_SECRET == "your-secret-key-change-me": return True
    return s == WEBHOOK_SECRET


# ═══════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status":"running","version":"5.0","strategy":"trend_rider","auto_trade":AUTO_TRADE})

@app.route("/webhook", methods=["POST"])
def webhook():
    if not check_auth(request):
        return jsonify({"error":"unauthorized"}), 401
    try:
        raw = request.get_data(as_text=True)
        logger.info(f"Webhook: {raw[:300]}")
        try: data = json.loads(raw)
        except: data = {"dir":"?","score":0,"price":0,"min":0}
        
        if is_duplicate(data):
            return jsonify({"status":"duplicate"}), 200
        
        trade_result = {"success":False,"reason":"Auto-trade off","info":None}
        
        if AUTO_TRADE:
            direction = data.get("dir")
            btc_price = data.get("price", 0)
            ticker, cprice, side, strike = find_best_contract(direction, btc_price)
            
            if ticker and cprice and side:
                ok, reason = check_guardrails(cprice)
                if ok:
                    success, info = place_kalshi_bet(ticker, side, cprice)
                    trade_result = {"success":success, "info":info if success else None,
                                    "reason":str(info) if not success else ""}
                else:
                    trade_result = {"success":False, "reason":reason}
            else:
                trade_result = {"success":False,
                    "reason":f"No contract in {MIN_CONTRACT_CENTS}-{MAX_CONTRACT_CENTS}c range"}
        
        message, target, dist = format_signal_message(data, trade_result if AUTO_TRADE else None)
        log_signal(data, target, dist, trade_result.get("info") if trade_result.get("success") else None)
        send_telegram(message)
        return jsonify({"status":"sent","traded":trade_result.get("success",False)}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        import traceback; logger.error(traceback.format_exc())
        return jsonify({"error":str(e)}), 500

@app.route("/test", methods=["GET"])
def test_alert():
    d = {"dir":"BEAR","score":4,"price":71400,"min":52,"atr":2.1,"t2h":-500,"t4h":-1200}
    msg, _, _ = format_signal_message(d)
    msg = "\U0001f9ea <b>TEST</b> \U0001f9ea\n\n" + msg
    return jsonify({"status":"sent" if send_telegram(msg) else "failed"})

@app.route("/balance", methods=["GET"])
def balance():
    if not check_auth(request): return jsonify({"error":"unauthorized"}), 401
    data = kalshi_get("/portfolio/balance")
    return jsonify(data) if data else jsonify({"error":"check keys"})

@app.route("/markets", methods=["GET"])
def debug_markets():
    if not check_auth(request): return jsonify({"error":"unauthorized"}), 401
    results = {}
    try:
        evts, et_now = get_event_tickers()
        results["et_time"] = et_now.strftime('%Y-%m-%d %H:%M ET')
        for evt in evts:
            data = kalshi_get(f"/markets?event_ticker={evt}&status=open&limit=100")
            if data and 'markets' in data:
                mkts = data['markets']
                parsed = []
                for m in mkts:
                    s = round(m.get('floor_strike') or m.get('cap_strike') or 0)
                    parsed.append({"ticker":m.get('ticker'),"strike":s,
                        "yes_c":parse_cents(m.get('yes_ask_dollars')),
                        "no_c":parse_cents(m.get('no_ask_dollars')),
                        "yes_raw":m.get('yes_ask_dollars'),"no_raw":m.get('no_ask_dollars')})
                parsed.sort(key=lambda x:x['strike'], reverse=True)
                results[evt] = {"count":len(mkts),"markets":parsed[:15]}
            else:
                results[evt] = "none"
    except Exception as e:
        results["error"] = str(e)
    return jsonify(results)

@app.route("/signals", methods=["GET"])
def view_signals():
    p = os.path.join(LOG_DIR, "signals.csv")
    if not os.path.exists(p): return jsonify({"signals":[],"count":0})
    sigs = []
    with open(p) as f:
        for row in csv.DictReader(f): sigs.append(row)
    return jsonify({"signals":sigs[-50:],"count":len(sigs)})

@app.route("/stop", methods=["GET","POST"])
def stop_trading():
    if not check_auth(request): return jsonify({"error":"unauthorized"}), 401
    global kill_switch; kill_switch = True
    send_telegram("\U0001f6d1 <b>KILL SWITCH ON</b>")
    return jsonify({"status":"stopped"})

@app.route("/start", methods=["GET","POST"])
def start_trading():
    if not check_auth(request): return jsonify({"error":"unauthorized"}), 401
    global kill_switch; kill_switch = False
    send_telegram("\U0001f7e2 <b>Trading ON</b>")
    return jsonify({"status":"started"})

@app.route("/health", methods=["GET"])
def health():
    tok = False
    try: tok = http_requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe",timeout=5).status_code==200
    except: pass
    kok = False
    if KALSHI_KEY_ID: kok = kalshi_get("/portfolio/balance") is not None
    return jsonify({"status":"healthy","version":"5.0","telegram":tok,"kalshi_api":kok,
        "auto_trade":AUTO_TRADE,"kill_switch":kill_switch,"daily_spend":daily_spend,
        "guardrails":{"max_bet":f"${MAX_BET_DOLLARS}",
            "range":f"{MIN_CONTRACT_CENTS}-{MAX_CONTRACT_CENTS}c","daily":f"${DAILY_LOSS_LIMIT}"}})

if __name__ == "__main__":
    Path(LOG_DIR).mkdir(exist_ok=True)
    logger.info(f"Trend Rider v5.0 | Auto:{AUTO_TRADE} | ${MAX_BET_DOLLARS}/bet | "
               f"{MIN_CONTRACT_CENTS}-{MAX_CONTRACT_CENTS}c | ${DAILY_LOSS_LIMIT}/day")
    app.run(host="0.0.0.0", port=PORT, debug=False)
