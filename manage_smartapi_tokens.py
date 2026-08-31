from flask import Flask, Response, jsonify, redirect, render_template_string, request, stream_with_context, url_for
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path


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
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="smartapi_tokens_", suffix=".json", dir=BASE_DIR, text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(tokens, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_name, TOKENS_FILE)
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


@app.post("/api/update-status")
def api_update_status():
    try:
        data = request.get_json()
        if data is None:
            raise ValueError("JSON body is required.")
        symbol = data["symbol"]
        exchange = data["exchange"].upper()
        token = str(data["token"])
        level = float(data["level"])
        field = data["field"]
        value = data["value"]
        if field == "activated":
            return {
                "status": "error",
                "message": "Activate/Deactivate is managed only from the UI.",
            }, 403
        if field not in {"enabled", "ltp"}:
            raise ValueError("Invalid field.")
        tokens = load_tokens()
        target = next(
            (item for item in tokens if item.get("symbol") == symbol and item.get("exchange", "").upper() == exchange and str(item.get("token")) == token and float(item.get("level")) == level),
            None,
        )
        if target is None:
            target = next(
                (item for item in tokens if item.get("exchange", "").upper() == exchange and str(item.get("token")) == token),
                None,
            )
        if target:
            if field == "enabled":
                target[field] = bool(value)
            else:
                target["ltp"] = float(value)
            save_tokens(tokens)
            return {"status": "ok", "message": f"{field} set to {value}"}, 200
        return {"status": "error", "message": "Token not found"}, 404
    except (KeyError, ValueError, TypeError) as error:
        return {"status": "error", "message": str(error)}, 400


if __name__ == "__main__":
    app.run(debug=True, port=5002, use_reloader=False)
