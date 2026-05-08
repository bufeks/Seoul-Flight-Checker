#!/usr/bin/env python3
"""
Seoul Flight Price Checker
SerpAPI (Google Flights) を使って東京↔ソウルの最安値を定期チェックし、
閾値を下回ったらメールで通知する。
"""

import argparse
import logging
import os
import smtplib
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import serpapi
from dotenv import load_dotenv

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
DESTINATION = "SEL"  # ソウル固定 (ICN/GMP)

# 蓄積データが少ない間のデフォルト時刻
# - 07:00: 前夜の需要変動をキャプチャ（LCCは深夜バッチで価格更新することが多い）
# - 21:00: 当日の販売状況を反映した夕方以降の調整をキャプチャ
DEFAULT_CHECK_TIMES = ("07:00", "21:00")

# 自動調整に必要な最低チェック回数（約2週間分）
MIN_SAMPLES_FOR_ADAPTATION = 28


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


def get_price_stats(origin, destination, depart_date, return_date) -> dict:
    """底値・平均・サンプル数を返す。データなしは None。"""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """SELECT MIN(price_jpy), AVG(price_jpy), COUNT(*)
               FROM price_history
               WHERE origin=? AND destination=? AND depart_date=? AND return_date=?""",
            (origin, destination, depart_date.isoformat(), return_date.isoformat()),
        ).fetchone()
    if not row or not row[2]:
        return {"low": None, "avg": None, "count": 0}
    return {"low": row[0], "avg": round(row[1]), "count": row[2]}


# ── スケジュール最適化 ─────────────────────────────────────────────────────────

def analyze_best_check_times() -> tuple[str, str]:
    """
    蓄積した価格履歴から「価格が最も下落した時間帯」を2つ選んで返す。
    データ不足時はデフォルト値を返す。

    手法: 同一フライト(出発日・帰着日)の連続チェック間の価格差分を時間帯別に集計し、
    下落幅の合計が最大だった上位2時間を選ぶ。
    """
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT checked_at, depart_date, return_date, price_jpy
            FROM price_history
            ORDER BY depart_date, return_date, checked_at
        """).fetchall()

    if len(rows) < MIN_SAMPLES_FOR_ADAPTATION:
        log.info("データが少ないためデフォルト時刻を使用 (%d/%d サンプル)",
                 len(rows), MIN_SAMPLES_FOR_ADAPTATION)
        return DEFAULT_CHECK_TIMES

    # 同一フライトの連続レコードを比較して、各時間帯の「平均価格下落幅」を集計
    drop_by_hour: dict[int, list[int]] = defaultdict(list)
    by_flight: dict[tuple, list] = defaultdict(list)
    for checked_at, depart_date, return_date, price in rows:
        by_flight[(depart_date, return_date)].append((checked_at, price))

    for records in by_flight.values():
        records.sort()
        for i in range(1, len(records)):
            prev_time, prev_price = records[i - 1]
            curr_time, curr_price = records[i]
            drop = prev_price - curr_price          # 正値 = 価格下落
            if drop > 0:
                hour = datetime.fromisoformat(curr_time).hour
                drop_by_hour[hour].append(drop)

    if not drop_by_hour:
        return DEFAULT_CHECK_TIMES

    # 各時間帯の平均下落幅で降順ソートし、上位2時間を選ぶ
    ranked = sorted(drop_by_hour.items(), key=lambda x: sum(x[1]) / len(x[1]), reverse=True)
    top2_hours = sorted(h for h, _ in ranked[:2])

    times = tuple(f"{h:02d}:00" for h in top2_hours)
    log.info("最適チェック時刻を算出: %s (分析対象フライト数: %d)", " / ".join(times), len(by_flight))
    return times  # type: ignore[return-value]


# ── SerpAPI ─────────────────────────────────────────────────────────────────

def fetch_cheapest_flight(origin: str, destination: str, depart_date: date, return_date: date) -> dict | None:
    """Google Flights で往復最安値を1件返す。見つからなければ None。"""
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
        results = serpapi.search(params)
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

def _build_smtp():
    import socket
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", 587))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    # IPv4 を明示して接続（IPv6 非対応環境向け）
    ipv4 = socket.getaddrinfo(smtp_host, smtp_port, socket.AF_INET)[0][4]
    server = smtplib.SMTP()
    server.connect(ipv4[0], ipv4[1])
    server.starttls(server_hostname=smtp_host)
    server.login(smtp_user, smtp_pass)
    return server, smtp_user


def _deal_rows_html(deals: list[dict]) -> str:
    def fmt_stats(stats: dict) -> str:
        low = f"¥{stats['low']:,}" if stats.get("low") else "—"
        avg = f"¥{stats['avg']:,}" if stats.get("avg") else "—"
        return low, avg

    rows = ""
    for d in deals:
        low, avg = fmt_stats(d.get("stats", {}))
        is_low = d.get("stats", {}).get("low") and d["price_jpy"] <= d["stats"]["low"]
        price_cell = (
            f"<td style='color:{'red' if is_low else 'green'};font-weight:bold'>"
            f"¥{d['price_jpy']:,}{'&nbsp;★' if is_low else ''}</td>"
        )
        rows += (
            f"<tr>"
            f"<td>{d['depart_date']}</td>"
            f"<td>{d['return_date']}</td>"
            f"<td>{d['nights']}泊</td>"
            f"{price_cell}"
            f"<td>{low}</td>"
            f"<td>{avg}</td>"
            f"<td>{d['airline']}</td>"
            f"<td><a href='{d['deep_link']}'>検索</a></td>"
            f"</tr>"
        )
    return rows


def _email_table(rows_html: str, threshold: int) -> str:
    return (
        "<html><body style='font-family:sans-serif'>"
        "<h2>✈️ ソウル格安便が見つかりました</h2>"
        f"<p>設定閾値: <strong>¥{threshold:,}</strong> 以下　"
        f"<span style='color:red'>★ = 過去最安値</span></p>"
        "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-size:14px'>"
        "<thead style='background:#f0f0f0'><tr>"
        "<th>出発日</th><th>帰国日</th><th>泊数</th>"
        "<th>現在価格</th><th>底値</th><th>平均価格</th>"
        "<th>航空会社</th><th>リンク</th>"
        f"</tr></thead><tbody>{rows_html}</tbody></table>"
        "<p style='color:gray;font-size:11px;margin-top:16px'>"
        "底値・平均は本ツールの計測開始以降の履歴に基づきます。"
        "Seoul Flight Checker が自動送信しました</p>"
        "</body></html>"
    )


def send_test_email():
    alert_to  = os.environ["ALERT_TO"]
    smtp_user = os.environ["SMTP_USER"]
    threshold = int(os.environ.get("PRICE_THRESHOLD", 50000))

    # 実際のアラートメールと同じレイアウトでサンプルデータを表示
    sample_deals = [
        {
            "depart_date": "2026-07-04", "return_date": "2026-07-08",
            "nights": 4, "price_jpy": 38500, "airline": "Jeju Air",
            "deep_link": "https://www.google.com/travel/flights",
            "stats": {"low": 38500, "avg": 45200, "count": 12},
        },
        {
            "depart_date": "2026-07-11", "return_date": "2026-07-14",
            "nights": 3, "price_jpy": 42000, "airline": "T'way Air",
            "deep_link": "https://www.google.com/travel/flights",
            "stats": {"low": 41000, "avg": 47800, "count": 8},
        },
    ]
    rows = _deal_rows_html(sample_deals)
    body_html = (
        "<html><body style='font-family:sans-serif'>"
        "<h2>✈️ Seoul Flight Checker — 接続テスト</h2>"
        "<p>メール通知の設定は正常です。実際のアラートはこのような形式で届きます（以下はサンプルデータ）。</p>"
        + _email_table(rows, threshold).replace("<html><body style='font-family:sans-serif'>", "")
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "✈️ Seoul Flight Checker — 接続テスト（サンプル付き）"
    msg["From"]    = smtp_user
    msg["To"]      = alert_to
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    server, smtp_user = _build_smtp()
    try:
        server.sendmail(smtp_user, [alert_to], msg.as_string())
        log.info("テストメールを %s に送信しました", alert_to)
    finally:
        server.quit()


def send_alert_email(deals: list[dict]):
    alert_to  = os.environ["ALERT_TO"]
    threshold = int(os.environ.get("PRICE_THRESHOLD", 50000))

    subject = f"✈️ ソウル格安便アラート！ {len(deals)}件 ¥{threshold:,}以下"
    rows    = _deal_rows_html(deals)
    body_html = _email_table(rows, threshold)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = os.environ["SMTP_USER"]
    msg["To"]      = alert_to
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    server, smtp_user = _build_smtp()
    try:
        server.sendmail(smtp_user, [alert_to], msg.as_string())
        log.info("Alert email sent to %s (%d deals)", alert_to, len(deals))
    except Exception as exc:
        log.error("Failed to send email: %s", exc)
    finally:
        server.quit()


# ── メインチェック ────────────────────────────────────────────────────────────

def run_check():
    origin    = os.environ.get("ORIGIN", "TYO")
    days_ahead = int(os.environ.get("DAYS_AHEAD", 90))
    threshold  = int(os.environ.get("PRICE_THRESHOLD", 50000))
    durations  = [int(d) for d in os.environ.get("TRIP_DURATIONS", "3,4,5,7").split(",")]

    today = date.today()
    deals_found = []

    log.info("=== チェック開始: %s → %s (閾値 ¥%s) ===", origin, DESTINATION, f"{threshold:,}")

    for days_out in range(7, days_ahead + 1, 7):          # 1週間刻みで検索
        depart = today + timedelta(days=days_out)
        for nights in durations:
            ret = depart + timedelta(days=nights)
            result = fetch_cheapest_flight(origin, DESTINATION, depart, ret)
            if result is None:
                continue

            price     = result["price_jpy"]
            airline   = result["airline"]
            deep_link = result["deep_link"]

            save_price(origin, DESTINATION, depart, ret, price, airline, deep_link)

            stats       = get_price_stats(origin, DESTINATION, depart, ret)
            is_new_low  = stats["low"] and price < stats["low"]

            flag = " ★最安値更新!" if is_new_low else ""
            log.info("  %s → %s (%d泊) ¥%s [%s]%s",
                     depart, ret, nights, f"{price:,}", airline, flag)

            if price <= threshold:
                deals_found.append({**result, "nights": nights, "stats": stats})

    if deals_found:
        log.info("閾値以下の便が %d 件見つかりました。", len(deals_found))
        if _email_configured():
            send_alert_email(deals_found)
        else:
            log.warning("メール未設定のため通知をスキップ。.env に SMTP_USER/SMTP_PASS/ALERT_TO を追加してください。")
    else:
        log.info("閾値以下の便は見つかりませんでした。")

    log.info("=== チェック完了 ===")


# ── エントリーポイント ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Seoul Flight Price Checker")
    parser.add_argument("--test", action="store_true",
                        help="メール設定の疎通確認用テストメールを送信して終了")
    args = parser.parse_args()

    _validate_env(require_serpapi=not args.test)
    init_db()

    if args.test:
        send_test_email()
        return

    # 最適チェック時刻をログに記録（参考情報）
    check_times = analyze_best_check_times()
    log.info("Seoul Flight Checker 起動 (推奨チェック時刻: %s)", " / ".join(check_times))
    run_check()


def _email_configured() -> bool:
    return all(os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASS", "ALERT_TO"))


def _validate_env(require_serpapi: bool = True):
    required = []
    if require_serpapi:
        required.append("SERPAPI_KEY")
    else:
        # --test モードはメール設定が必須
        required += ["SMTP_USER", "SMTP_PASS", "ALERT_TO"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"必須の環境変数が未設定です: {', '.join(missing)}\n"
            f".env.example を参考に .env ファイルを作成してください。"
        )


if __name__ == "__main__":
    main()
