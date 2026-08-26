"""Manage the SmartAPI token list used by 02_Test_websocket_v2.py.

Commands:
    ADD EXCHANGE TOKEN
    REMOVE EXCHANGE TOKEN
    LIST
    HELP
    EXIT

Examples:
    ADD NSE 99926000
    REMOVE NSE 99926000
"""

import json
import os
import tempfile
from pathlib import Path


TOKENS_FILE = Path(__file__).with_name("smartapi_tokens.json")
SUPPORTED_EXCHANGES = {"NSE", "NFO", "BSE", "BFO", "MCX", "NCX", "CDE"}


def load_tokens():
    if not TOKENS_FILE.exists():
        return []
    with TOKENS_FILE.open(encoding="utf-8") as file:
        tokens = json.load(file)
    if not isinstance(tokens, list):
        raise ValueError("token file must contain a JSON list")
    return tokens


def save_tokens(tokens):
    directory = TOKENS_FILE.parent
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix="smartapi_tokens_",
        suffix=".json",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file:
            json.dump(tokens, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_name, TOKENS_FILE)
    except Exception:
        if os.path.exists(temporary_name):
            os.remove(temporary_name)
        raise


def add_token(tokens, exchange, token):
    exchange = exchange.upper()
    if exchange not in SUPPORTED_EXCHANGES:
        raise ValueError(f"unsupported exchange: {exchange}")
    item = {"exchange": exchange, "token": str(token)}
    if item not in tokens:
        tokens.append(item)


def remove_token(tokens, exchange, token):
    item = {"exchange": exchange.upper(), "token": str(token)}
    tokens[:] = [current for current in tokens if current != item]


def list_tokens(tokens):
    if not tokens:
        print("No instruments configured.")
        return
    for item in tokens:
        print(f"{item['exchange']:<4} {item['token']}")


def show_help():
    print("ADD EXCHANGE TOKEN")
    print("  ADD NSE 99926000")
    print("  ADD NFO 123456")
    print("REMOVE EXCHANGE TOKEN")
    print("  REMOVE NSE 99926000")
    print("  REMOVE NFO 123456")
    print("LIST")
    print("HELP")
    print("EXIT")


def main():
    print(f"Managing {TOKENS_FILE}")
    show_help()

    while True:
        try:
            command = input("Token command > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not command:
            continue

        parts = command.split()
        action = parts[0].upper()

        try:
            tokens = load_tokens()
            if action in ("ADD", "REMOVE") and len(parts) == 3:
                exchange, token = parts[1], parts[2]
                if action == "ADD":
                    add_token(tokens, exchange, token)
                else:
                    remove_token(tokens, exchange, token)
                save_tokens(tokens)
                print(f"{action.title()} {exchange.upper()} {token}")
            elif action == "LIST" and len(parts) == 1:
                list_tokens(tokens)
            elif action == "HELP" and len(parts) == 1:
                show_help()
            elif action in ("EXIT", "QUIT") and len(parts) == 1:
                return
            else:
                print("Invalid command. Type HELP for usage.")
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(f"ERROR: {error}")


if __name__ == "__main__":
    main()
