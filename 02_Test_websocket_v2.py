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

    ADD NSE 3045
    ADD NSE 26000
    ADD BSE 999190
    ADD NFO 64325
    ADD MCX 123456

    REMOVE NSE 3045

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

    except Exception as e:

        print("Error processing tick:", e)

        # Useful while debugging
        print("Raw message:", message)


def load_token_list():
    with TOKENS_FILE.open(encoding="utf-8") as file:
        tokens = json.load(file)
    if not isinstance(tokens, list):
        raise ValueError("token file must contain a JSON list")
    return [(item["exchange"].upper(), str(item["token"])) for item in tokens]


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

    desired = {(EXCHANGE_MAP[exchange], token) for exchange, token in tokens}
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
        time.sleep(2)
        try:
            tokens = load_token_list()
            timestamp = time.strftime("%H:%M:%S")
            print(f"\n{timestamp} | CURRENT TOKEN PRICES")
            with token_lock:
                prices = dict(latest_prices)
            for exchange_name, token in tokens:
                exchange_type = EXCHANGE_MAP[exchange_name]
                price = prices.get((exchange_type, token), "waiting")
                formatted_price = f"{price:.2f}" if isinstance(price, float) else price
                print(f"{exchange_name:<4} | {token:<10} | LTP = {formatted_price}")
        except Exception as error:
            print("PRICE REPORT ERROR:", error)


def token_file_loop():
    while True:
        sync_token_list()
        time.sleep(1)


# ============================================================
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

                    print(
                        f"    {token}"
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