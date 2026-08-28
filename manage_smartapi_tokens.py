from flask import Flask, redirect, render_template_string, request, url_for
import json
import os
import sqlite3
import tempfile
from pathlib import Path


app = Flask(__name__)
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


def load_tokens():
    if not TOKENS_FILE.exists():
        return []
    with TOKENS_FILE.open(encoding="utf-8") as file:
        tokens = json.load(file)
    if tokens == [[]]:
        return []
    if not isinstance(tokens, list):
        raise ValueError("token file must contain a JSON list")
    return tokens


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
<table><tr><th>Symbol</th><th>Exchange</th><th>Token</th><th>Level</th><th>Command</th><th></th></tr>
{% for item in tokens %}<tr><td>{{ item.symbol }}</td><td>{{ item.exchange }}</td><td>{{ item.token }}</td><td>{{ item.level }}</td><td>ADD {{ item.symbol }} {{ item.exchange }} {{ item.token }} {{ item.level }}</td><td><form class="inline" method="post" action="{{ url_for('remove') }}"><input type="hidden" name="symbol" value="{{ item.symbol }}"><input type="hidden" name="exchange" value="{{ item.exchange }}"><input type="hidden" name="token" value="{{ item.token }}"><input type="hidden" name="level" value="{{ item.level }}"><button class="remove">REMOVE</button></form></td></tr>{% endfor %}
</table>
<script>
const expiryValues = {{ expiry_values|tojson }};
function updateExpiry(instrument) {
    document.getElementById("expiry").value = expiryValues[instrument] || "";
}
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
        item = {"symbol": found_symbol, "exchange": exchange, "token": str(token), "level": level}
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
    target = {"symbol": request.form["symbol"], "exchange": request.form["exchange"], "token": request.form["token"], "level": float(request.form["level"])}
    tokens[:] = [item for item in tokens if item != target]
    save_tokens(tokens)
    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(debug=True, port=5002, use_reloader=False)
