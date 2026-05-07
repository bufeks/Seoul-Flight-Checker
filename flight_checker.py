#!/usr/bin/env python3
"""
Seoul Flight Price Checker
SerpAPI (Google Flights) を使って東京↔ソウルの最安値を定期チェックし、
閾値を下回ったらメールで通知する。
"""

import json
import logging
import os
import smtplib
import sqlite3
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import schedule
import time

from dotenv import load_dotenv
from serpapi import GoogleSearch

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("flight_checker.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

DB_PATH = "prices.db"


# ── DB ──────────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                checked_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                origin      TEXT    NOT NULL,
                destination TEXT    NOT NULL,
                depart_date TEXT    NOT NULL,
                return_date TEXT    NOT NULL,
                price_jpy   INTEGER NOT NULL,
                airline     TEXT,
                deep_link   TEXT
            )
        """)
        conn.commit()


def save_price(origin, destination, depart_date, return_date, price_jpy, airline, deep_link):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO price_history
               (origin, destination, depart_date, return_date, price_jpy, airline, deep_link)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (origin, destination,
             depart_date.isoformat(), return_date.isoformat(),
             price_jpy, airline, deep_link),
        )
        conn.commit()


def get_historic_low(origin, destination, depart_date, return_date):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """SELECT MIN(price_jpy) FROM price_history
               WHERE origin=? AND destination=? AND depart_date=? AND return_date=?""",
            (origin, destination, depart_date.isoformat(), return_date.isoformat()),
        ).fetchone()
    return row[0] if row and row[0] else None


# ── SerpAPI ─────────────────────────────────────────────────────────────────

def fetch_cheapest_flight(origin: str, destination: str, depart_date: date, return_date: date) -> dict | None:
    """Google Flights で往復最安値を 1 件返す。見つからなければ None。"""
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": depart_date.strftime("%Y-%m-%d"),
        "return_date": return_date.strftime("%Y-%m-%d"),
        "currency": "JPY",
        "hl": "ja",
        "gl": "jp",
        "type": "1",          # 1 = 往復
        "api_key": os.environ["SERPAPI_KEY"],
    }
    try:
        results = GoogleSearch(params).get_dict()
    except Exception as exc:
        log.error("SerpAPI error: %s", exc)
        return None

    best_flights = results.get("best_flights") or results.get("other_flights")
    if not best_flights:
        log.warning("No flights found for %s→%s on %s/%s",
                    origin, destination, depart_date, return_date)
        return None

    cheapest = min(best_flights, key=lambda f: f.get("price", float("inf")))
    price = cheapest.get("price")
    if price is None:
        return None

    airline = (cheapest.get("flights") or [{}])[0].get("airline", "不明")
    deep_link = results.get("search_metadata", {}).get("google_flights_url", "")

    return {
        "price_jpy": int(price),
        "airline": airline,
        "deep_link": deep_link,
        "depart_date": depart_date,
        "return_date": return_date,
    }


# ── メール通知 ────────────────────────────────────────────────────────────────

def send_alert_email(deals: list[dict]):
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", 587))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    alert_to  = os.environ["ALERT_TO"]
    threshold = int(os.environ.get("PRICE_THRESHOLD", 50000))

    subject = f"✈️ ソウル格安便アラート！ {len(deals)}件 ¥{threshold:,}以下"

    rows = ""
    for d in deals:
        rows += (
            f"<tr>"
            f"<td>{d['depart_date']}</td>"
            f"<td>{d['return_date']}</td>"
            f"<td>{d['nights']}泊</td>"
            f"<td style='color:green;font-weight:bold'>¥{d['price_jpy']:,}</td>"
            f"<td>{d['airline']}</td>"
            f"<td><a href='{d['deep_link']}'>検索</a></td>"
            f"</tr>"
        )

    body_html = f"""
    <html><body>
    <h2>✈️ ソウル格安便が見つかりました</h2>
    <p>設定閾値: <strong>¥{threshold:,}</strong> 以下</p>
    <table border="1" cellpadding="6" style="border-collapse:collapse">
      <thead><tr>
        <th>出発日</th><th>帰国日</th><th>泊数</th>
        <th>価格(往復)</th><th>航空会社</th><th>リンク</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p style="color:gray;font-size:12px">Seoul Flight Checker が自動送信しました</p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_user
    msg["To"]      = alert_to
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [alert_to], msg.as_string())
        log.info("Alert email sent to %s (%d deals)", alert_to, len(deals))
    except Exception as exc:
        log.error("Failed to send email: %s", exc)


# ── メインチェック ────────────────────────────────────────────────────────────

def run_check():
    origin      = os.environ.get("ORIGIN", "TYO")
    destination = os.environ.get("DESTINATION", "SEL")
    days_ahead  = int(os.environ.get("DAYS_AHEAD", 90))
    threshold   = int(os.environ.get("PRICE_THRESHOLD", 50000))
    durations   = [int(d) for d in os.environ.get("TRIP_DURATIONS", "3,4,5,7").split(",")]

    today = date.today()
    deals_found = []

    log.info("=== チェック開始: %s → %s (閾値 ¥%s) ===", origin, destination, f"{threshold:,}")

    for days_out in range(7, days_ahead + 1, 7):          # 1週間刻みで検索
        depart = today + timedelta(days=days_out)
        for nights in durations:
            ret = depart + timedelta(days=nights)
            result = fetch_cheapest_flight(origin, destination, depart, ret)
            if result is None:
                continue

            price = result["price_jpy"]
            airline = result["airline"]
            deep_link = result["deep_link"]

            save_price(origin, destination, depart, ret, price, airline, deep_link)

            historic_low = get_historic_low(origin, destination, depart, ret)
            is_new_low = historic_low and price < historic_low

            flag = " ★最安値更新!" if is_new_low else ""
            log.info("  %s → %s (%d泊) ¥%s [%s]%s",
                     depart, ret, nights, f"{price:,}", airline, flag)

            if price <= threshold:
                deals_found.append({**result, "nights": nights})

    if deals_found:
        log.info("閾値以下の便が %d 件見つかりました。メール送信します。", len(deals_found))
        send_alert_email(deals_found)
    else:
        log.info("閾値以下の便は見つかりませんでした。")

    log.info("=== チェック完了 ===")


# ── エントリーポイント ──────────────────────────────────────────────────────────

def main():
    _validate_env()
    init_db()

    interval = int(os.environ.get("CHECK_INTERVAL_MINUTES", 60))

    log.info("Seoul Flight Checker 起動 (チェック間隔: %d 分)", interval)
    run_check()                                     # 起動直後に即実行

    schedule.every(interval).minutes.do(run_check)
    while True:
        schedule.run_pending()
        time.sleep(30)


def _validate_env():
    required = ["SERPAPI_KEY", "SMTP_USER", "SMTP_PASS", "ALERT_TO"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"必須の環境変数が未設定です: {', '.join(missing)}\n"
                         f".env.example を参考に .env ファイルを作成してください。")


if __name__ == "__main__":
    main()
