"""
Angel One SmartAPI - Dynamic WebSocket V2

Features:
    - Login using API key + Client Code + PIN + TOTP
    - Connect SmartWebSocketV2
    - Dynamically ADD tokens
    - Dynamically REMOVE tokens
    - LIST currently subscribed tokens
    - Print LTP on every tick
    - Multiple exchanges supported
    - Thread-safe token management
    - WebSocket stays connected while tokens are added/removed

Commands:

    ADD NSE 99926000 24322
    ADD NSE 99926000 24231
    ADD BSE 99919000 74322
    ADD BSE 99919000 74231
    ADD MCX 576363 400
    ADD MCX 576363 423

    REMOVE NSE 99926000 24322

    LIST

    HELP

    EXIT


Exchange names supported:

    NSE  -> NSE cash
    NFO  -> NSE futures/options
    BSE  -> BSE cash
    BFO  -> BSE futures/options
    MCX  -> MCX futures
    NCX  -> NCX
    CDE  -> Currency derivatives
"""

import threading
import time
import pyotp
import json
import requests
from pathlib import Path

from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2


# ============================================================
# USER CONFIGURATION
# ============================================================

# API_KEY = "YOUR_API_KEY"
# CLIENT_CODE = "YOUR_CLIENT_CODE"
# PIN = "YOUR_PIN"
# TOTP_SECRET = "YOUR_TOTP_SECRET"

API_KEY = "btT0lqAK"    #"IAsRqpKo"
CLIENT_CODE = "AACC089277"
PASSWORD = "7536"
TOTP_SECRET = "WADAPVNHVCMWFW3GWRGDV2L5R4"
TOKENS_FILE = Path(__file__).with_name("smartapi_tokens.json")
FLASK_API_URL = "http://127.0.0.1:5002/api/update-status"

# Re-arm thresholds by exchange
REARM_THRESHOLDS = {
    "NSE": 5,
    "BSE": 10,
    "NFO": 50,
    "BFO": 50,
    "MCX": 50,
    "NCX": 50,
    "CDE": 50,
}


# ============================================================
# EXCHANGE MAPPING
# ============================================================

EXCHANGE_MAP = {
    "NSE": SmartWebSocketV2.NSE_CM,
    "NFO": SmartWebSocketV2.NSE_FO,
    "BSE": SmartWebSocketV2.BSE_CM,
    "BFO": SmartWebSocketV2.BSE_FO,
    "MCX": SmartWebSocketV2.MCX_FO,
    "NCX": SmartWebSocketV2.NCX_FO,
    "CDE": SmartWebSocketV2.CDE_FO,
}


# Reverse mapping
EXCHANGE_NAME_MAP = {
    value: key
    for key, value in EXCHANGE_MAP.items()
}


# ============================================================
# GLOBAL VARIABLES
# ============================================================

smart_api = None
sws = None

websocket_connected = False

# Protect token dictionary from multiple threads
token_lock = threading.Lock()

# Currently subscribed tokens
#
# Example:
#
# {
#     1: {"3045", "26000"},
#     2: {"64325"},
# }
#
subscribed_tokens = {}
latest_prices = {}
crossing_states = {}
token_levels = {}
last_token_file_signature = None


# ============================================================
# LOGIN
# ============================================================

def login():
    global smart_api

    print()
    print("=" * 70)
    print("ANGEL ONE LOGIN")
    print("=" * 70)

    try:
        smart_api = SmartConnect(API_KEY)

        # Generate current TOTP
        totp = pyotp.TOTP(TOTP_SECRET).now()

        print("Generating session...")

        session = smart_api.generateSession(
            CLIENT_CODE,
            PASSWORD,
            totp
        )

        if not session:
            raise Exception("No response received from Angel One.")

        if session.get("status") is False:
            raise Exception(
                f"Login failed: {session.get('message')}"
            )

        auth_token = session["data"]["jwtToken"]
        feed_token = smart_api.getfeedToken()

        print()
        print("Login successful.")
        print("Client Code :", CLIENT_CODE)
        print("Feed Token  :", feed_token[:10] + "...")
        print()

        return auth_token, feed_token

    except Exception as e:
        print()
        print("LOGIN ERROR:")
        print(e)
        raise


# ============================================================
# WEBSOCKET CALLBACK - ON OPEN
# ============================================================

def on_open(wsapp):
    global websocket_connected

    websocket_connected = True

    print()
    print("=" * 70)
    print("WEBSOCKET CONNECTED")
    print("=" * 70)
    print("You can now ADD / REMOVE tokens.")
    print()


# ============================================================
# WEBSOCKET CALLBACK - ON DATA
# ============================================================

def on_data(wsapp, message):

    try:

        subscription_mode = message.get("subscription_mode")

        exchange_type = message.get("exchange_type")

        token = message.get("token")

        raw_ltp = message.get("last_traded_price")

        # Angel One sends price multiplied by 100
        ltp = raw_ltp / 100.0

        exchange_name = EXCHANGE_NAME_MAP.get(
            exchange_type,
            str(exchange_type)
        )

        with token_lock:
            latest_prices[(exchange_type, str(token))] = ltp

        token_record = next(
            (
                item for item in load_token_list()
                if item["exchange"].upper() == exchange_name.upper()
                and str(item["token"]) == str(token)
            ),
            None,
        )
        if token_record is not None:
            call_api_update(
                token_record.get("symbol", ""),
                exchange_name,
                token,
                token_record.get("level", 0.0),
                "ltp",
                ltp,
            )

        evaluate_crossing(exchange_type, str(token), ltp)

    except Exception as e:

        print("Error processing tick:", e)

        # Useful while debugging
        print("Raw message:", message)


def load_token_list():
    with TOKENS_FILE.open(encoding="utf-8") as file:
        tokens = json.load(file)
    if not isinstance(tokens, list):
        raise ValueError("token file must contain a JSON list")
    if tokens == [[]]:
        return []
    for item in tokens:
        if not isinstance(item, dict) or not {"exchange", "token", "level"}.issubset(item):
            raise ValueError("each token must contain exchange, token, and level")
    return [
        {
            "exchange": item["exchange"].upper(),
            "symbol": item.get("symbol", ""),
            "token": str(item["token"]),
            "level": float(item["level"]),
            "enabled": bool(item.get("enabled", True)),
            "activated": bool(item.get("activated", True)),
            "ltp": item.get("ltp"),
        }
        for item in tokens
    ]


def call_api_update(symbol, exchange, token, level, field, value):
    try:
        payload = {
            "symbol": symbol,
            "exchange": exchange,
            "token": str(token),
            "level": float(level),
            "field": field,
            "value": value,
        }
        if field == "enabled":
            payload["value"] = bool(value)
        elif field == "ltp":
            payload["value"] = float(value)
        response = requests.post(FLASK_API_URL, json=payload, timeout=2)
        """
        if response.status_code == 200:
            print(f"API: {field} for {symbol} {exchange} {token} {level} set to {value}")
        else:
            print(f"API Error: {response.status_code} {response.text}")
        """
    except Exception as error:
        print(f"API Call Error: {error}")


def set_level_enabled(exchange_name, token, level, enabled):
    token_key = (EXCHANGE_MAP[exchange_name], str(token))
    with token_lock:
        for level_item in token_levels.get(token_key, []):
            if float(level_item["level"]) == float(level):
                previous_enabled = bool(level_item.get("enabled", True))
                if previous_enabled != bool(enabled):
                    level_item["enabled"] = bool(enabled)
                    call_api_update(
                        level_item.get("symbol", ""),
                        exchange_name,
                        str(token),
                        level,
                        "enabled",
                        bool(enabled),
                    )
                    print(
                        f"LEVEL {exchange_name} {token} | level={float(level):.2f} | "
                        f"STATUS = {'ENABLED' if bool(enabled) else 'DISABLED'}"
                    )
                return


def evaluate_crossing(exchange_type, token, price):
    token_key = (exchange_type, token)
    exchange_name = EXCHANGE_NAME_MAP.get(exchange_type, str(exchange_type))
    rearm_threshold = REARM_THRESHOLDS.get(exchange_name, 50)

    with token_lock:
        for level_item in token_levels.get(token_key, []):
            level = level_item["level"]
            symbol = level_item.get("symbol", "")
            enabled = level_item.get("enabled", True)
            activated = level_item.get("activated", True)
            key = (exchange_type, token, level)
            state = crossing_states.setdefault(key, {"previous": None, "armed": True, "disabled_since": None})
            previous = state["previous"]

            if previous is None:
                if abs(price - level) <= rearm_threshold:
                    level_item["enabled"] = False
                    state["disabled_since"] = price
                    call_api_update(symbol, exchange_name, token, level, "enabled", False)
                    print(
                        f"LEVEL {exchange_name} {token} | level={level:.2f} | "
                        f"STATUS = DISABLED (initial inside threshold band)"
                    )
                else:
                    level_item["enabled"] = True
                    state["disabled_since"] = None
                    call_api_update(symbol, exchange_name, token, level, "enabled", True)
                    print(
                        f"LEVEL {exchange_name} {token} | level={level:.2f} | "
                        f"STATUS = ENABLED (initial outside threshold band)"
                    )
            else:
                if enabled is False and state.get("disabled_since") is not None:
                    if abs(price - level) > rearm_threshold:
                        level_item["enabled"] = True
                        state["disabled_since"] = None
                        call_api_update(symbol, exchange_name, token, level, "enabled", True)
                        print(
                            f"LEVEL {exchange_name} {token} | level={level:.2f} | "
                            f"STATUS = ENABLED (rearmed outside threshold band)"
                        )
                elif previous is not None and enabled and activated:
                    if previous <= level < price:
                        Take_entry_in_trade(token,"UP",price,symbol)
                        print(f"UP {exchange_name} {token} | price={price:.2f} level={level:.2f}")
                        state["armed"] = False
                        level_item["enabled"] = False
                        state["disabled_since"] = price
                        call_api_update(symbol, exchange_name, token, level, "enabled", False)
                        call_api_update(symbol, exchange_name, token, level, "activated", False)
                    elif previous >= level > price:
                        Take_entry_in_trade(token,"DOWN",price,symbol)
                        print(f"DOWN {exchange_name} {token} | price={price:.2f} level={level:.2f}")
                        state["armed"] = False
                        level_item["enabled"] = False
                        state["disabled_since"] = price
                        call_api_update(symbol, exchange_name, token, level, "enabled", False)
                        call_api_update(symbol, exchange_name, token, level, "activated", False)

                if not state["armed"] and abs(price - level) >= rearm_threshold:
                    state["armed"] = True
                    print(f"REARMED {exchange_name} {token} at {price:.2f} for level={level:.2f}")

            state["previous"] = price


def sync_token_list():
    global last_token_file_signature
    if not websocket_connected:
        return
    try:
        tokens = load_token_list()
        signature = json.dumps(tokens, sort_keys=True)
        if signature == last_token_file_signature:
            return
        last_token_file_signature = signature
    except Exception as error:
        print("TOKEN FILE ERROR:", error)
        return

    desired = {(EXCHANGE_MAP[item["exchange"]], item["token"]) for item in tokens}
    with token_lock:
        token_levels.clear()
        for item in tokens:
            key = (EXCHANGE_MAP[item["exchange"]], item["token"])
            token_levels.setdefault(key, []).append({"level": item["level"], "symbol": item.get("symbol", ""), "enabled": item.get("enabled", True), "activated": item.get("activated", True)})
            crossing_states.setdefault(
                (key[0], key[1], item["level"]),
                {"previous": None, "armed": True},
            )
    with token_lock:
        current = {
            (exchange_type, token)
            for exchange_type, token_set in subscribed_tokens.items()
            for token in token_set
        }

    for exchange_type, token in desired - current:
        add_token(EXCHANGE_NAME_MAP[exchange_type], token)
    for exchange_type, token in current - desired:
        remove_token(EXCHANGE_NAME_MAP[exchange_type], token)


def print_prices_loop():
    while True:
        time.sleep(10)
        try:
            tokens = load_token_list()
            timestamp = time.strftime("%H:%M:%S")
            print(f"\n{timestamp} | CURRENT TOKEN PRICES")
            with token_lock:
                prices = dict(latest_prices)
            for item in tokens:
                exchange_name, token = item["exchange"], item["token"]
                exchange_type = EXCHANGE_MAP[exchange_name]
                price = prices.get((exchange_type, token), "waiting")
                formatted_price = f"{price:.2f}" if isinstance(price, float) else price
                enabled_state = "ENABLED" if item.get("enabled", True) else "DISABLED"
                activated_state = "ACTIVATED" if item.get("activated", True) else "DEACTIVATED"
                
                print(
                    f"{exchange_name:<4} | {item['symbol']:<24} | "
                    f"{token:<10} | LTP = {formatted_price} | "
                    f"LEVEL = {item['level']:.2f} | "
                    f"STATUS = {enabled_state} | {activated_state}"
                )
                
        except Exception as error:
            print("PRICE REPORT ERROR:", error)


def token_file_loop():
    while True:
        sync_token_list()
        time.sleep(1)

def get_the_ATM_strike(token, price, direction, symbol):
    # Calculate the ATM strike based on the price
    if(token=="99926000"):
        expected_strike = round(price / 50) * 50
        if(direction=="UP"):
            expected_strike = expected_strike + 50
            call_put = "PE"
        elif(direction=="DOWN"):
            expected_strike = expected_strike - 50
            call_put = "CE"
    elif(token=="99919000"):
        expected_strike = round(price / 100) * 100
        if(direction=="UP"):
            expected_strike = expected_strike + 100 
            call_put = "PE"
        elif(direction=="DOWN"):
            expected_strike = expected_strike - 100
            call_put = "CE"
    else:
        print("This index does not have ATM strike calculation logic implemented.")
        expected_strike = None   
    print(f"ATM Strike calculated for {symbol}: {expected_strike}")
    return expected_strike

def Take_entry_in_trade(token,direction,price,symbol):
    print(f"TRADE ENTRY: Token {token} Direction {direction} Price {price} Symbol {symbol}")
    expected_strike = get_the_ATM_strike(token,price, direction,symbol)
    print(f"Expected Strike for {symbol}: {expected_strike}")


# WEBSOCKET CALLBACK - ERROR
# ============================================================

def on_error(wsapp, error):

    global websocket_connected

    websocket_connected = False

    print()
    print("=" * 70)
    print("WEBSOCKET ERROR")
    print("=" * 70)
    print(error)
    print()


# ============================================================
# WEBSOCKET CALLBACK - CLOSE
# ============================================================

def on_close(wsapp):

    global websocket_connected

    websocket_connected = False

    print()
    print("=" * 70)
    print("WEBSOCKET CLOSED")
    print("=" * 70)
    print()


# ============================================================
# ADD TOKEN
# ============================================================

def add_token(exchange_name, token):

    exchange_name = exchange_name.upper()
    token = str(token)

    if exchange_name not in EXCHANGE_MAP:

        print()
        print("Invalid exchange.")
        print("Supported exchanges:")
        print(", ".join(EXCHANGE_MAP.keys()))
        return

    if not websocket_connected:

        print("WebSocket is not connected.")
        return

    exchange_type = EXCHANGE_MAP[exchange_name]

    with token_lock:

        # Check duplicate
        if (
            exchange_type in subscribed_tokens
            and token in subscribed_tokens[exchange_type]
        ):

            print(
                f"{exchange_name} {token} "
                f"is already subscribed."
            )

            return

        token_list = [
            {
                "exchangeType": exchange_type,
                "tokens": [token]
            }
        ]

        try:

            # LTP mode = 1
            sws.subscribe(
                correlation_id=f"ADD{int(time.time())}",
                mode=SmartWebSocketV2.LTP_MODE,
                token_list=token_list
            )

            if exchange_type not in subscribed_tokens:
                subscribed_tokens[exchange_type] = set()

            subscribed_tokens[exchange_type].add(token)

            print()
            print(
                f"ADDED : {exchange_name} {token}"
            )
            print()

        except Exception as e:

            print()
            print("SUBSCRIBE ERROR:")
            print(e)
            print()


# ============================================================
# REMOVE TOKEN
# ============================================================

def remove_token(exchange_name, token):

    exchange_name = exchange_name.upper()
    token = str(token)

    if exchange_name not in EXCHANGE_MAP:

        print("Invalid exchange.")
        return

    if not websocket_connected:

        print("WebSocket is not connected.")
        return

    exchange_type = EXCHANGE_MAP[exchange_name]

    with token_lock:

        if (
            exchange_type not in subscribed_tokens
            or token not in subscribed_tokens[exchange_type]
        ):

            print(
                f"{exchange_name} {token} "
                f"is not currently subscribed."
            )

            return

        token_list = [
            {
                "exchangeType": exchange_type,
                "tokens": [token]
            }
        ]

        try:

            sws.unsubscribe(
                correlation_id=f"REM{int(time.time())}",
                mode=SmartWebSocketV2.LTP_MODE,
                token_list=token_list
            )

            subscribed_tokens[
                exchange_type
            ].remove(token)

            # Remove empty exchange
            if not subscribed_tokens[exchange_type]:

                del subscribed_tokens[exchange_type]

            print()
            print(
                f"REMOVED : {exchange_name} {token}"
            )
            print()

        except Exception as e:

            print()
            print("UNSUBSCRIBE ERROR:")
            print(e)
            print()


# ============================================================
# LIST TOKENS
# ============================================================

def list_tokens():

    print()
    print("=" * 70)
    print("CURRENTLY SUBSCRIBED TOKENS")
    print("=" * 70)

    configured_tokens = {
        (EXCHANGE_MAP[item["exchange"]], item["token"]): item
        for item in load_token_list()
    }

    with token_lock:

        if not subscribed_tokens:

            print("No tokens subscribed.")

        else:

            for exchange_type, tokens in subscribed_tokens.items():

                exchange_name = EXCHANGE_NAME_MAP.get(
                    exchange_type,
                    str(exchange_type)
                )

                print()
                print(exchange_name)

                for token in sorted(tokens):
                    item = configured_tokens.get((exchange_type, token), {})
                    symbol = item.get("symbol", "")
                    level = item.get("level", "")
                    price = latest_prices.get((exchange_type, token), "waiting")
                    formatted_price = f"{price:.2f}" if isinstance(price, float) else price
                    print(
                        f"    {EXCHANGE_NAME_MAP.get(exchange_type, exchange_type):<4} | "
                        f"{symbol:<24} | {token:<10} | LTP = {formatted_price} | "
                        f"LEVEL = {level}"
                    )

    print()
    print("=" * 70)
    print()


# ============================================================
# HELP
# ============================================================

def show_help():

    print()
    print("=" * 70)
    print("COMMANDS")
    print("=" * 70)

    print()
    print("ADD TOKEN")
    print("    ADD NSE 3045")
    print("    ADD NSE 26000")
    print("    ADD NFO 64325")
    print("    ADD BSE 999190")
    print("    ADD MCX 123456")

    print()
    print("REMOVE TOKEN")
    print("    REMOVE NSE 3045")

    print()
    print("SHOW SUBSCRIBED TOKENS")
    print("    LIST")

    print()
    print("SHOW HELP")
    print("    HELP")

    print()
    print("EXIT PROGRAM")
    print("    EXIT")

    print()
    print("=" * 70)
    print()


# ============================================================
# COMMAND LINE THREAD
# ============================================================

def command_loop():

    while True:

        try:

            command = input("Command > ").strip()

        except (KeyboardInterrupt, EOFError):

            print()
            print("Exiting...")

            break

        if not command:
            continue

        parts = command.split()

        action = parts[0].upper()

        # ----------------------------------------------------
        # ADD
        # ----------------------------------------------------

        if action == "ADD":

            if len(parts) != 3:

                print(
                    "Usage: ADD NSE 3045"
                )

                continue

            exchange = parts[1]
            token = parts[2]

            add_token(
                exchange,
                token
            )

        # ----------------------------------------------------
        # REMOVE
        # ----------------------------------------------------

        elif action == "REMOVE":

            if len(parts) != 3:

                print(
                    "Usage: REMOVE NSE 3045"
                )

                continue

            exchange = parts[1]
            token = parts[2]

            remove_token(
                exchange,
                token
            )

        # ----------------------------------------------------
        # LIST
        # ----------------------------------------------------

        elif action == "LIST":

            list_tokens()

        # ----------------------------------------------------
        # HELP
        # ----------------------------------------------------

        elif action == "HELP":

            show_help()

        # ----------------------------------------------------
        # EXIT
        # ----------------------------------------------------

        elif action in ("EXIT", "QUIT"):

            print("Closing WebSocket...")

            try:
                sws.close_connection()
            except Exception:
                pass

            break

        # ----------------------------------------------------
        # UNKNOWN
        # ----------------------------------------------------

        else:

            print(
                "Unknown command."
            )

            print(
                "Type HELP for available commands."
            )


# ============================================================
# MAIN
# ============================================================

def main():

    global sws

    print()
    print("=" * 70)
    print("ANGEL ONE SMARTAPI - DYNAMIC LTP WEBSOCKET")
    print("=" * 70)
    print()

    # --------------------------------------------------------
    # LOGIN
    # --------------------------------------------------------

    auth_token, feed_token = login()

    # --------------------------------------------------------
    # CREATE WEBSOCKET
    # --------------------------------------------------------

    sws = SmartWebSocketV2(
        auth_token=auth_token,
        api_key=API_KEY,
        client_code=CLIENT_CODE,
        feed_token=feed_token,

        # Retry configuration
        max_retry_attempt=5,
        retry_strategy=0,
        retry_delay=5,
        retry_duration=30
    )

    # --------------------------------------------------------
    # CALLBACKS
    # --------------------------------------------------------

    sws.on_open = on_open
    sws.on_data = on_data
    sws.on_error = on_error
    sws.on_close = on_close

    # --------------------------------------------------------
    # START WEBSOCKET IN BACKGROUND THREAD
    # --------------------------------------------------------

    websocket_thread = threading.Thread(
        target=sws.connect,
        daemon=True
    )

    websocket_thread.start()

    threading.Thread(target=token_file_loop, name="token-file-sync", daemon=True).start()
    threading.Thread(target=print_prices_loop, name="price-report", daemon=True).start()

    # --------------------------------------------------------
    # WAIT FOR CONNECTION
    # --------------------------------------------------------

    print("Connecting to Angel One WebSocket...")

    for _ in range(100):

        if websocket_connected:
            break

        time.sleep(0.1)

    if not websocket_connected:

        print()
        print(
            "WARNING: WebSocket connection "
            "not established yet."
        )

        print(
            "You can wait and try again."
        )

    # --------------------------------------------------------
    # COMMAND LINE
    # --------------------------------------------------------

    show_help()

    command_loop()

    print()
    print("Program terminated.")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()