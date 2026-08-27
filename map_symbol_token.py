import flet as ft
import sqlite3
import os


# ============================================================
# FILES
# ============================================================

EXPIRY_DB = "expiry_date.db"
SCRIP_DB = "scrip_master_small.db"


# ============================================================
# DEFAULT EXPIRY VALUES
# ============================================================

DEFAULT_EXPIRY = {
    "Nifty": "Nifty_exp",
    "Sensex": "Sensex_exp",
    "Crudeoil": "Crude_exp",
}


# ============================================================
# CREATE EXPIRY DATABASE IF NOT AVAILABLE
# ============================================================

def create_expiry_db_if_missing():

    if os.path.exists(EXPIRY_DB):
        return

    con = sqlite3.connect(EXPIRY_DB)

    cur = con.cursor()

    cur.execute("""
        CREATE TABLE expiry_date (
            instrument TEXT PRIMARY KEY,
            expirystring TEXT
        )
    """)

    for instrument, expirystring in DEFAULT_EXPIRY.items():
        cur.execute(
            "INSERT INTO expiry_date (instrument, expirystring) VALUES (?, ?)",
            (instrument, expirystring)
        )

    con.commit()
    con.close()


# ============================================================
# GET EXPIRY STRING
# ============================================================

def get_expiry_string(instrument):

    create_expiry_db_if_missing()

    con = sqlite3.connect(EXPIRY_DB)

    cur = con.cursor()

    cur.execute(
        "SELECT expirystring FROM expiry_date WHERE instrument = ?",
        (instrument,)
    )

    row = cur.fetchone()

    con.close()

    if row:
        return row[0]

    # If instrument is not present, create it
    expirystring = DEFAULT_EXPIRY.get(instrument, "")

    con = sqlite3.connect(EXPIRY_DB)

    cur = con.cursor()

    cur.execute(
        "INSERT OR REPLACE INTO expiry_date "
        "(instrument, expirystring) VALUES (?, ?)",
        (instrument, expirystring)
    )

    con.commit()
    con.close()

    return expirystring


def save_expiry_string(instrument, expirystring):

    create_expiry_db_if_missing()

    expirystring = expirystring.strip()

    if not expirystring:
        raise ValueError("Please enter expiry string.")

    con = sqlite3.connect(EXPIRY_DB)

    con.execute(
        "INSERT OR REPLACE INTO expiry_date "
        "(instrument, expirystring) VALUES (?, ?)",
        (instrument, expirystring),
    )

    con.commit()
    con.close()


# ============================================================
# SEARCH SCRIPT MASTER
# ============================================================

def search_token(expiry_string):

    if not os.path.exists(SCRIP_DB):
        return None, f"ERROR: {SCRIP_DB} not found."

    expiry_string = expiry_string.strip()

    if not expiry_string:
        return None, "Please enter expiry string."

    con = sqlite3.connect(SCRIP_DB)

    cur = con.cursor()

    # --------------------------------------------------------
    # FIRST: EXACT SYMBOL MATCH
    # --------------------------------------------------------

    cur.execute("""
        SELECT token, symbol, exch_seg
        FROM scrip_master
        WHERE UPPER(symbol) = UPPER(?)
    """, (expiry_string,))

    rows = cur.fetchall()

    if len(rows) == 1:
        con.close()

        token, symbol, exch_seg = rows[0]

        return token, (
            f"Token : {token}\n"
            f"Symbol : {symbol}\n"
            f"Exchange : {exch_seg}"
        )

    # --------------------------------------------------------
    # SECOND: PREFIX / PARTIAL MATCH
    # --------------------------------------------------------

    cur.execute("""
        SELECT token, symbol, exch_seg
        FROM scrip_master
        WHERE UPPER(symbol) LIKE UPPER(?)
        ORDER BY symbol
    """, (expiry_string + "%",))

    rows = cur.fetchall()

    con.close()

    if not rows:
        return None, f"No matching symbol found for:\n{expiry_string}"

    # One match
    if len(rows) == 1:

        token, symbol, exch_seg = rows[0]

        return token, (
            f"Token : {token}\n"
            f"Symbol : {symbol}\n"
            f"Exchange : {exch_seg}"
        )

    # Multiple matches
    result = f"Multiple matches found ({len(rows)}):\n\n"

    for token, symbol, exch_seg in rows:
        result += (
            f"Token: {token}   "
            f"Symbol: {symbol}   "
            f"Exchange: {exch_seg}\n"
        )

    return None, result


# ============================================================
# FLET APPLICATION
# ============================================================

def main(page: ft.Page):

    page.title = "Expiry Token Finder"

    page.window_width = 750
    page.window_height = 600

    page.padding = 25

    page.theme_mode = ft.ThemeMode.LIGHT


    # --------------------------------------------------------
    # INSTRUMENT DROPDOWN
    # --------------------------------------------------------

    instrument_dropdown = ft.Dropdown(
        label="Select Instrument",
        width=300,
        value="Nifty",
        options=[
            ft.dropdown.Option("Nifty"),
            ft.dropdown.Option("Sensex"),
            ft.dropdown.Option("Crudeoil"),
        ],
    )


    # --------------------------------------------------------
    # EXPIRY STRING
    # --------------------------------------------------------

    expiry_text = ft.TextField(
        label="Expiry String",
        width=500,
        value=get_expiry_string("Nifty"),
        hint_text="Enter expiry / symbol string",
    )

    option_strike_text = ft.TextField(
        label="Option Strike Price",
        width=240,
        hint_text="Example: 24300",
        keyboard_type=ft.KeyboardType.NUMBER,
    )

    option_type_dropdown = ft.Dropdown(
        label="Option Type",
        width=180,
        value="CE",
        options=[
            ft.dropdown.Option("CE"),
            ft.dropdown.Option("PE"),
        ],
    )


    # --------------------------------------------------------
    # RESULT TEXT AREA
    # --------------------------------------------------------

    result_text = ft.TextField(
        label="Token / Search Result",
        multiline=True,
        min_lines=5,
        max_lines=12,
        width=680,
        read_only=False,
    )


    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------

    status_text = ft.Text("")


    # --------------------------------------------------------
    # INSTRUMENT CHANGE
    # --------------------------------------------------------

    def instrument_changed(e):

        instrument = instrument_dropdown.value

        expiry_text.value = get_expiry_string(instrument)

        option_strike_text.value = ""
        option_type_dropdown.value = "CE"

        result_text.value = ""

        status_text.value = ""

        page.update()


    instrument_dropdown.on_change = instrument_changed


    # --------------------------------------------------------
    # SAVE EXPIRY STRING
    # --------------------------------------------------------

    def save_expiry(e):

        try:
            save_expiry_string(
                instrument_dropdown.value,
                expiry_text.value,
            )
            status_text.value = (
                f"Expiry string saved for {instrument_dropdown.value}."
            )
        except ValueError as error:
            status_text.value = str(error)

        page.update()


    # --------------------------------------------------------
    # SHOW TOKEN
    # --------------------------------------------------------

    def show_token(e):

        instrument_symbols = {
            "Nifty": "Nifty 50",
            "Sensex": "SENSEX",
            "Crudeoil": "MCXCRUDEX",
        }
        expiry_string = expiry_text.value.strip()
        strike = option_strike_text.value.strip()
        option_type = option_type_dropdown.value

        if strike:
            try:
                strike_value = float(strike)
                if not strike_value.is_integer():
                    raise ValueError
                expiry_string = f"{expiry_string}{int(strike_value)}{option_type}"
            except ValueError:
                result_text.value = "Option strike price must be a whole number."
                status_text.value = "Invalid strike price."
                page.update()
                return
        else:
            expiry_string = instrument_symbols[instrument_dropdown.value]

        token, result = search_token(expiry_string)

        result_text.value = result

        if token:

            status_text.value = "Token found successfully."

        else:

            status_text.value = "Check search result."

        page.update()


    # --------------------------------------------------------
    # COPY TOKEN
    # --------------------------------------------------------

    def copy_token(e):

        text = result_text.value.strip()

        if not text:
            status_text.value = "Nothing to copy."
            page.update()
            return

        # If result contains exactly one token, extract it
        if text.startswith("Token :"):

            token = text.split("\n")[0]
            token = token.replace("Token :", "").strip()

            page.set_clipboard(token)

            status_text.value = f"Token {token} copied to clipboard."

        else:

            status_text.value = (
                "Multiple tokens found. "
                "Use an exact symbol to copy one token."
            )

        page.update()


    # --------------------------------------------------------
    # BUTTONS
    # --------------------------------------------------------

    show_button = ft.ElevatedButton(
        text="SHOW TOKEN",
        icon=ft.icons.SEARCH,
        on_click=show_token,
    )

    copy_button = ft.ElevatedButton(
        text="COPY TOKEN",
        icon=ft.icons.CONTENT_COPY,
        on_click=copy_token,
    )

    save_expiry_button = ft.ElevatedButton(
        text="SAVE EXPIRY",
        icon=ft.icons.SAVE,
        on_click=save_expiry,
    )


    # --------------------------------------------------------
    # UI
    # --------------------------------------------------------

    page.add(

        ft.Text(
            "Expiry Token Finder",
            size=28,
            weight=ft.FontWeight.BOLD,
        ),

        ft.Divider(),

        ft.Row(
            [
                instrument_dropdown,
            ]
        ),

        ft.Row(
            [
                expiry_text,
            ]
        ),

        ft.Row(
            [
                option_strike_text,
                option_type_dropdown,
            ],
            spacing=15,
        ),

        ft.Row(
            [
                save_expiry_button,
                show_button,
                copy_button,
            ],
            spacing=15,
        ),

        ft.Divider(),

        result_text,

        status_text,
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    create_expiry_db_if_missing()

    ft.app(target=main)