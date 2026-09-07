from flask import Flask, Response, jsonify, redirect, render_template_string, request, stream_with_context, url_for
import json
import os
import pyotp
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2


app = Flask(__name__)


@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


BASE_DIR = Path(__file__).parent
TOKENS_FILE = BASE_DIR / "smartapi_tokens.json"
EXPIRY_DB = BASE_DIR / "expiry_date.db"
SCRIP_DB = BASE_DIR / "scrip_master_small.db"
SUPPORTED_EXCHANGES = {"NSE", "NFO", "BSE", "BFO", "MCX", "NCX", "CDE"}
INSTRUMENT_SYMBOLS = {
    "Nifty": "Nifty 50",
    "Sensex": "SENSEX",
    "Crudeoil": "MCXCRUDEX",
}
DEFAULT_EXPIRY = {
    "Nifty": "Nifty_exp",
    "Sensex": "Sensex_exp",
    "Crudeoil": "Crude_exp",
}
DEFAULT_EXCHANGE = {"Nifty": "NSE", "Sensex": "BSE", "Crudeoil": "MCX"}
token_file_lock = threading.RLock()

API_KEY = "btT0lqAK"
CLIENT_CODE = "AACC089277"
PASSWORD = "7536"
TOTP_SECRET = "WADAPVNHVCMWFW3GWRGDV2L5R4"
REARM_THRESHOLDS = {
    "NSE": 5, "BSE": 10, "NFO": 50, "BFO": 50,
    "MCX": 50, "NCX": 50, "CDE": 50,
}
EXCHANGE_MAP = {
    "NSE": SmartWebSocketV2.NSE_CM,
    "NFO": SmartWebSocketV2.NSE_FO,
    "BSE": SmartWebSocketV2.BSE_CM,
    "BFO": SmartWebSocketV2.BSE_FO,
    "MCX": SmartWebSocketV2.MCX_FO,
    "NCX": SmartWebSocketV2.NCX_FO,
    "CDE": SmartWebSocketV2.CDE_FO,
}
EXCHANGE_NAME_MAP = {value: key for key, value in EXCHANGE_MAP.items()}
smart_api = None
sws = None
websocket_connected = False
token_lock = threading.Lock()
subscribed_tokens = {}
latest_prices = {}
crossing_states = {}
token_levels = {}
last_token_file_signature = None
websocket_thread = None


def normalize_token_entry(item):
    if not isinstance(item, dict):
        return item
    normalized = dict(item)
    normalized["symbol"] = str(normalized.get("symbol", "")).strip()
    normalized["exchange"] = str(normalized.get("exchange", "")).upper()
    normalized["token"] = str(normalized.get("token", ""))
    normalized["level"] = float(normalized.get("level", 0.0))
    normalized["enabled"] = bool(normalized.get("enabled", True))
    normalized["activated"] = bool(normalized.get("activated", True))
    normalized["ltp"] = normalized.get("ltp")
    return normalized


def load_tokens():
    if not TOKENS_FILE.exists():
        return []
    with TOKENS_FILE.open(encoding="utf-8") as file:
        tokens = json.load(file)
    if tokens == [[]]:
        return []
    if not isinstance(tokens, list):
        raise ValueError("token file must contain a JSON list")
    normalized_tokens = []
    for item in tokens:
        normalized = normalize_token_entry(item)
        normalized.setdefault("enabled", True)
        normalized.setdefault("activated", True)
        normalized.setdefault("ltp", None)
        normalized_tokens.append(normalized)
    return normalized_tokens


def save_tokens(tokens):
    with token_file_lock:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="smartapi_tokens_", suffix=".json", dir=BASE_DIR, text=True
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(tokens, file, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            for attempt in range(3):
                try:
                    os.replace(temporary_name, TOKENS_FILE)
                    break
                except PermissionError:
                    if attempt == 2:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except Exception:
            if os.path.exists(temporary_name):
                os.remove(temporary_name)
            raise


def create_expiry_db():
    with sqlite3.connect(EXPIRY_DB) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS expiry_date ("
            "instrument TEXT PRIMARY KEY, expirystring TEXT)"
        )
        for instrument, expiry in DEFAULT_EXPIRY.items():
            connection.execute(
                "INSERT OR IGNORE INTO expiry_date (instrument, expirystring) VALUES (?, ?)",
                (instrument, expiry),
            )


def load_expiry_list():
    with sqlite3.connect(EXPIRY_DB) as connection:
        rows = connection.execute(
            "SELECT instrument, expirystring FROM expiry_date ORDER BY instrument"
        ).fetchall()
    return [{"instrument": instrument, "expirystring": expiry} for instrument, expiry in rows]


if not EXPIRY_DB.exists():
    create_expiry_db()

expiry_list = load_expiry_list()


def get_expiry(instrument):
    entry = next(
        (item for item in expiry_list if item["instrument"] == instrument),
        None,
    )
    return entry["expirystring"] if entry else DEFAULT_EXPIRY[instrument]


def save_expiry(instrument, expiry):
    global expiry_list
    expiry = expiry.strip()
    if not expiry:
        raise ValueError("Expiry string is required.")
    with sqlite3.connect(EXPIRY_DB) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO expiry_date (instrument, expirystring) VALUES (?, ?)",
            (instrument, expiry),
        )
    entry = next(
        (item for item in expiry_list if item["instrument"] == instrument),
        None,
    )
    if entry is None:
        expiry_list.append({"instrument": instrument, "expirystring": expiry})
    else:
        entry["expirystring"] = expiry


def search_symbol(symbol):
    if not SCRIP_DB.exists():
        raise ValueError(f"{SCRIP_DB.name} not found.")
    with sqlite3.connect(SCRIP_DB) as connection:
        rows = connection.execute(
            "SELECT token, symbol, exch_seg FROM scrip_master "
            "WHERE UPPER(symbol) = UPPER(?) ORDER BY symbol", (symbol,)
        ).fetchall()
        if not rows:
            rows = connection.execute(
                "SELECT token, symbol, exch_seg FROM scrip_master "
                "WHERE UPPER(symbol) LIKE UPPER(?) ORDER BY symbol", (symbol + "%",)
            ).fetchall()
    return rows


def build_symbol(instrument, expiry, strike, option_type):
    if not strike.strip():
        return INSTRUMENT_SYMBOLS[instrument]
    try:
        strike_value = float(strike)
    except ValueError as error:
        raise ValueError("Strike price must be numeric.") from error
    if not strike_value.is_integer():
        raise ValueError("Strike price must be a whole number.")
    return f"{expiry.strip()}{int(strike_value)}{option_type}"


PAGE = """
<!doctype html>
<title>SmartAPI Token Manager</title>
<style>
body { font-family: Segoe UI, sans-serif; max-width: 1000px; margin: 32px auto; padding: 0 20px; color: #17202a; }
h1 { margin-bottom: 6px; } .muted { color: #667085; }
form.grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; align-items: end; }
label { display: grid; gap: 5px; font-weight: 600; } input, select, button { padding: 9px; font: inherit; }
button { cursor: pointer; } .actions { display: flex; gap: 8px; } .message { margin: 18px 0; padding: 10px; background: #eef6ff; }
table { width: 100%; border-collapse: collapse; margin-top: 24px; } th, td { text-align: left; padding: 10px; border-bottom: 1px solid #ddd; }
.inline { display: inline; } .remove { color: #a21caf; } @media (max-width: 700px) { form.grid { grid-template-columns: 1fr 1fr; } }
</style>
<h1>SmartAPI Token Manager</h1>
<p class="muted">Build a symbol, search the script master, and manage the shared tracking list.</p>
{% if message %}<div class="message">{{ message }}</div>{% endif %}
<form class="grid" method="post" action="{{ url_for('add') }}">
<label>Instrument<select name="instrument" id="instrument" onchange="updateExpiry(this.value)">{% for value in instruments %}<option value="{{ value }}" {% if value == selected_instrument %}selected{% endif %}>{{ value }}</option>{% endfor %}</select></label>
<label>Expiry string<input name="expiry" id="expiry" value="{{ expiry }}" required></label>
<label>Strike price<input name="strike" value="{{ strike }}" placeholder="Blank for index"></label>
<label>Option type<select name="option_type"><option {% if option_type == 'CE' %}selected{% endif %}>CE</option><option {% if option_type == 'PE' %}selected{% endif %}>PE</option></select></label>
<label>Level<input name="level" value="{{ level }}" required></label>
<div class="actions"><button type="submit">ADD TOKEN</button><button formaction="{{ url_for('save_expiry_route') }}">SAVE EXPIRY</button></div>
</form>
<p>Generated symbol: <strong>{{ symbol }}</strong>{% if result %} | {{ result }}{% endif %}</p>
<table>
  <thead><tr><th>Symbol</th><th>Exchange</th><th>Token</th><th>Level</th><th>Ltp</th><th>Enabled</th><th>Activated</th><th></th></tr></thead>
  <tbody id="token-table-body">
    {% for item in tokens %}<tr><td>{{ item.symbol }}</td><td>{{ item.exchange }}</td><td>{{ item.token }}</td><td>{{ item.level }}</td><td class="ltp-cell">{{ item.get('ltp', '') }}</td><td><form class="inline" method="post" action="{{ url_for('toggle_status') }}"><input type="hidden" name="symbol" value="{{ item.symbol }}"><input type="hidden" name="exchange" value="{{ item.exchange }}"><input type="hidden" name="token" value="{{ item.token }}"><input type="hidden" name="level" value="{{ item.level }}"><input type="hidden" name="field" value="enabled"><button>{% if item.enabled %}Disable{% else %}Enable{% endif %}</button></form></td><td><form class="inline" method="post" action="{{ url_for('toggle_status') }}"><input type="hidden" name="symbol" value="{{ item.symbol }}"><input type="hidden" name="exchange" value="{{ item.exchange }}"><input type="hidden" name="token" value="{{ item.token }}"><input type="hidden" name="level" value="{{ item.level }}"><input type="hidden" name="field" value="activated"><button>{% if item.activated %}Deactivate{% else %}Activate{% endif %}</button></form></td><td><form class="inline" method="post" action="{{ url_for('remove') }}"><input type="hidden" name="symbol" value="{{ item.symbol }}"><input type="hidden" name="exchange" value="{{ item.exchange }}"><input type="hidden" name="token" value="{{ item.token }}"><input type="hidden" name="level" value="{{ item.level }}"><button class="remove">REMOVE</button></form></td></tr>{% endfor %}
  </tbody>
</table>
<script>
const expiryValues = {{ expiry_values|tojson }};
function updateExpiry(instrument) {
    document.getElementById("expiry").value = expiryValues[instrument] || "";
}

function formatLtp(value) {
    if (value === null || value === undefined || value === '') {
        return '';
    }
    const numericValue = Number(value);
    return Number.isFinite(numericValue) ? numericValue.toFixed(2) : value;
}

function renderTokenRows(tokens) {
    const tbody = document.getElementById('token-table-body');
    if (!tbody) return;
    tbody.innerHTML = tokens.map((item) => {
        const symbol = item.symbol || '';
        const exchange = item.exchange || '';
        const token = item.token || '';
        const level = item.level ?? '';
        const ltp = formatLtp(item.ltp);
        const enabled = item.enabled !== false;
        const activated = item.activated !== false;
        return `
            <tr>
                <td>${symbol}</td>
                <td>${exchange}</td>
                <td>${token}</td>
                <td>${level}</td>
                <td class="ltp-cell">${ltp}</td>
                <td>
                    <form class="inline" method="post" action="/toggle-status">
                        <input type="hidden" name="symbol" value="${symbol}">
                        <input type="hidden" name="exchange" value="${exchange}">
                        <input type="hidden" name="token" value="${token}">
                        <input type="hidden" name="level" value="${level}">
                        <input type="hidden" name="field" value="enabled">
                        <button>${enabled ? 'Disable' : 'Enable'}</button>
                    </form>
                </td>
                <td>
                    <form class="inline" method="post" action="/toggle-status">
                        <input type="hidden" name="symbol" value="${symbol}">
                        <input type="hidden" name="exchange" value="${exchange}">
                        <input type="hidden" name="token" value="${token}">
                        <input type="hidden" name="level" value="${level}">
                        <input type="hidden" name="field" value="activated">
                        <button>${activated ? 'Deactivate' : 'Activate'}</button>
                    </form>
                </td>
                <td>
                    <form class="inline" method="post" action="/remove">
                        <input type="hidden" name="symbol" value="${symbol}">
                        <input type="hidden" name="exchange" value="${exchange}">
                        <input type="hidden" name="token" value="${token}">
                        <input type="hidden" name="level" value="${level}">
                        <button class="remove">REMOVE</button>
                    </form>
                </td>
            </tr>
        `;
    }).join('');
}

const tokenStream = new EventSource('/stream');
tokenStream.addEventListener('tokens', (event) => {
    try {
        const tokens = JSON.parse(event.data);
        renderTokenRows(tokens);
    } catch (error) {
        console.error('Token stream parse error:', error);
    }
});
tokenStream.onerror = () => {
    console.warn('Token stream reconnecting...');
};
renderTokenRows({{ tokens|tojson }});
</script>
"""


def page_values(form=None, message="", result=""):
    form = form or {}
    instrument = form.get("instrument", "Nifty")
    expiry = form.get("expiry", get_expiry(instrument))
    strike = form.get("strike", "")
    option_type = form.get("option_type", "CE")
    level = form.get("level", "")
    exchange = form.get("exchange", "")
    try:
        symbol = build_symbol(instrument, expiry, strike, option_type)
    except ValueError:
        symbol = ""
    return render_template_string(
        PAGE, instruments=INSTRUMENT_SYMBOLS,
        selected_instrument=instrument, expiry=expiry, strike=strike,
        option_type=option_type, level=level, exchange=exchange, symbol=symbol,
        expiry_values={item["instrument"]: item["expirystring"] for item in expiry_list},
        tokens=load_tokens(), message=message, result=result,
    )


@app.get("/")
def home():
    return page_values()


@app.get("/api/tokens")
def api_tokens():
    return jsonify(load_tokens())


@app.get("/stream")
def stream_tokens():
    def generate():
        while True:
            payload = json.dumps(load_tokens())
            yield f"event: tokens\ndata: {payload}\n\n"
            time.sleep(3)

    return Response(stream_with_context(generate()), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


@app.post("/add")
def add():
    try:
        instrument = request.form["instrument"]
        expiry = request.form["expiry"]
        strike = request.form.get("strike", "")
        option_type = request.form.get("option_type", "CE").upper()
        level = float(request.form["level"])
        if option_type not in {"CE", "PE"}:
            raise ValueError("Invalid option type.")
        symbol = build_symbol(instrument, expiry, strike, option_type)
        rows = search_symbol(symbol)
        if len(rows) != 1:
            return page_values(request.form, result=f"{len(rows)} matches found for {symbol}.")
        token, found_symbol, found_exchange = rows[0]
        exchange = found_exchange.upper()
        with token_file_lock:
            tokens = load_tokens()
            item = {
                "symbol": found_symbol,
                "exchange": exchange,
                "token": str(token),
                "level": level,
                "enabled": True,
                "activated": True,
                "ltp": None,
            }
            item = normalize_token_entry(item)
            if item not in tokens:
                tokens.append(item)
                save_tokens(tokens)
        return page_values(request.form, message=f"Command sent: ADD {found_symbol} {exchange} {token} {level}", result="Token added.")
    except (KeyError, ValueError, OSError, sqlite3.Error) as error:
        return page_values(request.form, message=str(error))


@app.post("/save-expiry")
def save_expiry_route():
    try:
        save_expiry(request.form["instrument"], request.form["expiry"])
        return page_values(request.form, message="Expiry string saved.")
    except (KeyError, ValueError, sqlite3.Error) as error:
        return page_values(request.form, message=str(error))


@app.post("/remove")
def remove():
    with token_file_lock:
        tokens = load_tokens()
        symbol = request.form["symbol"]
        exchange = request.form["exchange"]
        token = request.form["token"]
        level = float(request.form["level"])
        tokens[:] = [
            item for item in tokens
            if not (
                item.get("symbol") == symbol
                and item.get("exchange", "").upper() == exchange.upper()
                and str(item.get("token")) == token
                and float(item.get("level")) == level
            )
        ]
        save_tokens(tokens)
    return redirect(url_for("home"))


@app.post("/toggle-status")
def toggle_status():
    try:
        symbol = request.form["symbol"]
        exchange = request.form["exchange"]
        token = request.form["token"]
        level = float(request.form["level"])
        field = request.form["field"]
        if field not in {"enabled", "activated"}:
            raise ValueError("Invalid field.")
        with token_file_lock:
            tokens = load_tokens()
            target = next(
                (item for item in tokens if item["symbol"] == symbol and item["exchange"] == exchange and item["token"] == token and float(item["level"]) == level),
                None,
            )
            if target:
                target[field] = not target[field]
                save_tokens(tokens)
        return redirect(url_for("home"))
    except (KeyError, ValueError) as error:
        return page_values(message=str(error))


def update_token_record(symbol, exchange, token, level, field, value):
    if field == "activated":
        return {"status": "error", "message": "Activate/Deactivate is managed only from the UI."}, 403
    if field not in {"enabled", "ltp"}:
        raise ValueError("Invalid field.")
    with token_file_lock:
        tokens = load_tokens()
        target = next(
            (item for item in tokens
             if item.get("symbol") == symbol
             and item.get("exchange", "").upper() == exchange.upper()
             and str(item.get("token")) == str(token)
             and float(item.get("level")) == float(level)),
            None,
        )
        if target is None:
            target = next(
                (item for item in tokens
                 if item.get("exchange", "").upper() == exchange.upper()
                 and str(item.get("token")) == str(token)),
                None,
            )
        if target is None:
            return {"status": "error", "message": "Token not found"}, 404
        target[field] = bool(value) if field == "enabled" else float(value)
        save_tokens(tokens)
    return {"status": "ok", "message": f"{field} set to {value}"}, 200


@app.post("/api/update-status")
def api_update_status():
    try:
        data = request.get_json()
        if data is None:
            raise ValueError("JSON body is required.")
        return update_token_record(
            data["symbol"], data["exchange"], data["token"],
            data["level"], data["field"], data["value"],
        )
    except (KeyError, ValueError, TypeError) as error:
        return {"status": "error", "message": str(error)}, 400


def login():
    global smart_api
    smart_api = SmartConnect(API_KEY)
    session = smart_api.generateSession(
        CLIENT_CODE, PASSWORD, pyotp.TOTP(TOTP_SECRET).now()
    )
    if not session or session.get("status") is False:
        raise RuntimeError(f"SmartAPI login failed: {session}")
    return session["data"]["jwtToken"], smart_api.getfeedToken()


def load_token_list():
    return load_tokens()


def call_api_update(symbol, exchange, token, level, field, value):
    try:
        update_token_record(symbol, exchange, token, level, field, value)
    except Exception as error:
        print(f"Token update error: {error}")


def on_open(_wsapp):
    global websocket_connected
    websocket_connected = True
    print("SmartAPI websocket connected")


def on_error(_wsapp, error):
    global websocket_connected
    websocket_connected = False
    print(f"SmartAPI websocket error: {error}")


def on_close(_wsapp):
    global websocket_connected
    websocket_connected = False
    print("SmartAPI websocket closed")


def on_data(_wsapp, message):
    try:
        exchange_type = message.get("exchange_type")
        token = str(message.get("token"))
        ltp = float(message["last_traded_price"]) / 100.0
        exchange_name = EXCHANGE_NAME_MAP.get(exchange_type, str(exchange_type))
        with token_lock:
            latest_prices[(exchange_type, token)] = ltp
        token_record = next(
            (item for item in load_token_list()
             if item["exchange"] == exchange_name and item["token"] == token),
            None,
        )
        if token_record:
            call_api_update(token_record["symbol"], exchange_name, token,
                            token_record["level"], "ltp", ltp)
        evaluate_crossing(exchange_type, token, ltp)
    except Exception as error:
        print(f"Error processing tick: {error}; raw message: {message}")


def evaluate_crossing(exchange_type, token, price):
    exchange_name = EXCHANGE_NAME_MAP.get(exchange_type, str(exchange_type))
    threshold = REARM_THRESHOLDS.get(exchange_name, 50)
    with token_lock:
        for level_item in token_levels.get((exchange_type, token), []):
            level = level_item["level"]
            state = crossing_states.setdefault(
                (exchange_type, token, level),
                {"previous": None, "armed": True, "disabled_since": None},
            )
            previous = state["previous"]
            enabled = level_item.get("enabled", True)
            activated = level_item.get("activated", True)
            if previous is None:
                level_item["enabled"] = abs(price - level) > threshold
                state["disabled_since"] = None if level_item["enabled"] else price
                call_api_update(level_item["symbol"], exchange_name, token, level,
                                "enabled", level_item["enabled"])
            elif not enabled and state.get("disabled_since") is not None:
                if abs(price - level) > threshold:
                    level_item["enabled"] = True
                    state["disabled_since"] = None
                    call_api_update(level_item["symbol"], exchange_name, token,
                                    level, "enabled", True)
            elif activated and enabled:
                direction = None
                if previous <= level < price:
                    direction = "UP"
                elif previous >= level > price:
                    direction = "DOWN"
                if direction:
                    take_entry_in_trade(token, direction, price, level_item["symbol"])
                    level_item["enabled"] = False
                    level_item["activated"] = False
                    state["disabled_since"] = price
                    call_api_update(level_item["symbol"], exchange_name, token,
                                    level, "enabled", False)
                    call_api_update(level_item["symbol"], exchange_name, token,
                                    level, "activated", False)
            state["previous"] = price


def get_atm_strike(token, price, direction, symbol):
    if token == "99926000":
        increment = 50
    elif token == "99919000":
        increment = 100
    else:
        print(f"ATM strike calculation is not implemented for {symbol}")
        return None
    strike = round(price / increment) * increment
    strike += increment if direction == "UP" else -increment
    print(f"ATM strike calculated for {symbol}: {strike}")
    return strike


def take_entry_in_trade(token, direction, price, symbol):
    print(f"TRADE ENTRY: token={token} direction={direction} price={price} symbol={symbol}")
    get_atm_strike(token, price, direction, symbol)


def add_token(exchange_name, token):
    if not websocket_connected or exchange_name.upper() not in EXCHANGE_MAP:
        return
    exchange_type = EXCHANGE_MAP[exchange_name.upper()]
    token = str(token)
    with token_lock:
        if token in subscribed_tokens.get(exchange_type, set()):
            return
        sws.subscribe(
            correlation_id=f"ADD{int(time.time() * 1000)}",
            mode=SmartWebSocketV2.LTP_MODE,
            token_list=[{"exchangeType": exchange_type, "tokens": [token]}],
        )
        subscribed_tokens.setdefault(exchange_type, set()).add(token)


def remove_token(exchange_name, token):
    if not websocket_connected or exchange_name.upper() not in EXCHANGE_MAP:
        return
    exchange_type = EXCHANGE_MAP[exchange_name.upper()]
    token = str(token)
    with token_lock:
        if token not in subscribed_tokens.get(exchange_type, set()):
            return
        sws.unsubscribe(
            correlation_id=f"REM{int(time.time() * 1000)}",
            mode=SmartWebSocketV2.LTP_MODE,
            token_list=[{"exchangeType": exchange_type, "tokens": [token]}],
        )
        subscribed_tokens[exchange_type].remove(token)
        if not subscribed_tokens[exchange_type]:
            del subscribed_tokens[exchange_type]


def sync_token_list():
    global last_token_file_signature
    if not websocket_connected:
        return
    tokens = load_token_list()
    signature = json.dumps(tokens, sort_keys=True)
    if signature == last_token_file_signature:
        return
    last_token_file_signature = signature
    desired = {(EXCHANGE_MAP[item["exchange"]], item["token"]) for item in tokens}
    with token_lock:
        token_levels.clear()
        for item in tokens:
            key = (EXCHANGE_MAP[item["exchange"]], item["token"])
            token_levels.setdefault(key, []).append(item)
        current = {
            (exchange_type, token)
            for exchange_type, values in subscribed_tokens.items()
            for token in values
        }
    for exchange_type, token in desired - current:
        add_token(EXCHANGE_NAME_MAP[exchange_type], token)
    for exchange_type, token in current - desired:
        remove_token(EXCHANGE_NAME_MAP[exchange_type], token)


def token_file_loop():
    while True:
        try:
            sync_token_list()
        except Exception as error:
            print(f"Token synchronization error: {error}")
        time.sleep(1)


def print_prices_loop():
    while True:
        time.sleep(10)
        try:
            with token_lock:
                prices = dict(latest_prices)
            for item in load_token_list():
                exchange_type = EXCHANGE_MAP[item["exchange"]]
                price = prices.get((exchange_type, item["token"]), "waiting")
                print(f"{item['exchange']} | {item['symbol']} | {item['token']} | LTP = {price}")
        except Exception as error:
            print(f"Price report error: {error}")


def start_websocket():
    global sws, websocket_thread
    if websocket_thread is not None and websocket_thread.is_alive():
        return
    auth_token, feed_token = login()
    sws = SmartWebSocketV2(
        auth_token=auth_token, api_key=API_KEY, client_code=CLIENT_CODE,
        feed_token=feed_token, max_retry_attempt=5, retry_strategy=0,
        retry_delay=5, retry_duration=30,
    )
    sws.on_open = on_open
    sws.on_data = on_data
    sws.on_error = on_error
    sws.on_close = on_close
    websocket_thread = threading.Thread(
        target=sws.connect, name="smartapi-websocket", daemon=True
    )
    websocket_thread.start()
    threading.Thread(target=token_file_loop, name="token-file-sync", daemon=True).start()
    threading.Thread(target=print_prices_loop, name="price-report", daemon=True).start()


if __name__ == "__main__":
    start_websocket()
    app.run(debug=True, port=5002, threaded=True, use_reloader=False)
