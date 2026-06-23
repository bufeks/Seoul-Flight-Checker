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
DESTINATION = "ICN"  # 仁川国際空港（ソウル固定）

# 蓄積データが少ない間のデフォルト時刻
# - 07:00: 前夜の需要変動をキャプチャ（LCCは深夜バッチで価格更新することが多い）
# - 21:00: 当日の販売状況を反映した夕方以降の調整をキャプチャ
DEFAULT_CHECK_TIMES = ("07:00", "21:00")

# 自動調整に必要な最低チェック回数（約2週間分）
MIN_SAMPLES_FOR_ADAPTATION = 28

# 価格履歴の保持期間
HISTORY_DAYS = 365

PRICE_LEVEL_LABEL = {
    "low":     "🟢 割安",
    "typical": "🟡 標準",
    "high":    "🔴 割高",
}

WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def _fmt_date(d) -> str:
    """'2026-06-14(日)' 形式に変換"""
    if isinstance(d, str):
        d = date.fromisoformat(d)
    w = WEEKDAY_JA[d.weekday()]
    return f"{d}({w})"


# ── DB ──────────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                checked_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                origin        TEXT    NOT NULL,
                destination   TEXT    NOT NULL,
                depart_date   TEXT    NOT NULL,
                return_date   TEXT    NOT NULL,
                price_jpy     INTEGER NOT NULL,
                airline       TEXT,
                deep_link     TEXT,
                depart_time   TEXT,
                arrive_time   TEXT,
                duration_min  INTEGER,
                flight_number TEXT,
                is_direct     INTEGER DEFAULT 1
            )
        """)
        # 既存テーブルへのカラム追加（マイグレーション）
        for col, typedef in [
            ("depart_time",    "TEXT"),
            ("arrive_time",    "TEXT"),
            ("duration_min",   "INTEGER"),
            ("flight_number",  "TEXT"),
            ("is_direct",      "INTEGER DEFAULT 1"),
            ("origin_ap",      "TEXT"),
            ("dest_ap",        "TEXT"),
            ("ret_depart_time","TEXT"),
            ("ret_arrive_time","TEXT"),
            ("ret_duration_min","INTEGER"),
            ("ret_flight_number","TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE price_history ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass
        conn.commit()


def save_price(origin, destination, depart_date, return_date, price_jpy, airline, deep_link,
               depart_time=None, arrive_time=None, duration_min=None,
               flight_number=None, is_direct=1,
               origin_ap=None, dest_ap=None,
               ret_depart_time=None, ret_arrive_time=None,
               ret_duration_min=None, ret_flight_number=None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO price_history
               (origin, destination, depart_date, return_date, price_jpy, airline, deep_link,
                depart_time, arrive_time, duration_min, flight_number, is_direct,
                origin_ap, dest_ap,
                ret_depart_time, ret_arrive_time, ret_duration_min, ret_flight_number)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (origin, destination,
             depart_date.isoformat(), return_date.isoformat(),
             price_jpy, airline, deep_link,
             depart_time, arrive_time, duration_min, flight_number, is_direct,
             origin_ap, dest_ap,
             ret_depart_time, ret_arrive_time, ret_duration_min, ret_flight_number),
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


def cleanup_old_records():
    """HISTORY_DAYS より古い価格履歴を削除する。"""
    cutoff = (datetime.now() - timedelta(days=HISTORY_DAYS)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("DELETE FROM price_history WHERE checked_at < ?", (cutoff,))
        if cur.rowcount:
            log.info("古い価格履歴を %d 件削除しました（%d日超）", cur.rowcount, HISTORY_DAYS)
        conn.commit()


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

    out_legs  = cheapest.get("flights") or [{}]
    first_leg = out_legs[0]
    last_leg  = out_legs[-1]

    airline   = first_leg.get("airline", "不明")
    deep_link = results.get("search_metadata", {}).get("google_flights_url", "")

    def _t(airport_dict):
        t = (airport_dict or {}).get("time", "")
        return t.split(" ")[-1] if t else ""   # "2026-06-12 08:00" → "08:00"

    def _ap(airport_dict):
        return (airport_dict or {}).get("id", "")

    # 往路
    depart_time   = _t(first_leg.get("departure_airport"))
    arrive_time   = _t(last_leg.get("arrival_airport"))
    origin_ap     = _ap(first_leg.get("departure_airport"))
    dest_ap       = _ap(last_leg.get("arrival_airport"))
    duration_min  = cheapest.get("total_duration")
    flight_number = first_leg.get("flight_number", "")
    is_direct     = not bool(cheapest.get("layovers"))

    # 復路（SerpAPIが返す場合）
    ret_legs = cheapest.get("return_flights") or []
    if ret_legs and isinstance(ret_legs, list):
        ret_first = ret_legs[0] if isinstance(ret_legs[0], dict) else {}
        ret_last  = ret_legs[-1] if isinstance(ret_legs[-1], dict) else {}
        ret_depart_time   = _t(ret_first.get("departure_airport"))
        ret_arrive_time   = _t(ret_last.get("arrival_airport"))
        ret_duration_min  = sum(r.get("duration", 0) for r in ret_legs) or None
        ret_flight_number = ret_first.get("flight_number", "")
    else:
        ret_depart_time = ret_arrive_time = ret_flight_number = ""
        ret_duration_min = None

    # Google Flights 価格インサイト
    insights      = results.get("price_insights", {})
    price_level   = insights.get("price_level", "")
    typical_range = insights.get("typical_price_range", [])

    # セール・プロモーションタグ
    sale_kws = {"sale", "promo", "deal", "special", "セール", "特価", "割引"}
    promo_tags = [
        ext for f in out_legs
        for ext in (f.get("extensions") or [])
        if any(kw in ext.lower() for kw in sale_kws)
    ]

    return {
        "price_jpy":     int(price),
        "airline":       airline,
        "deep_link":     deep_link,
        "depart_date":   depart_date,
        "return_date":   return_date,
        "depart_time":      depart_time,
        "arrive_time":      arrive_time,
        "duration_min":     duration_min,
        "flight_number":    flight_number,
        "is_direct":        is_direct,
        "origin_ap":        origin_ap,
        "dest_ap":          dest_ap,
        "ret_depart_time":  ret_depart_time,
        "ret_arrive_time":  ret_arrive_time,
        "ret_duration_min": ret_duration_min,
        "ret_flight_number":ret_flight_number,
        "price_level":   price_level,
        "typical_range": typical_range,
        "promo_tags":    promo_tags,
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
    server._host = smtp_host  # starttls の TLS SNI に必要
    server.starttls()
    server.login(smtp_user, smtp_pass)
    return server, smtp_user


def _deal_rows_html(deals: list[dict]) -> str:
    rows = ""
    for d in deals:
        stats = d.get("stats", {})
        low   = f"¥{stats['low']:,}" if stats.get("low") else "—"
        avg   = f"¥{stats['avg']:,}" if stats.get("avg") else "—"

        is_hist_low = stats.get("low") and d["price_jpy"] <= stats["low"]
        price_color = "red" if is_hist_low else "green"
        price_cell  = (
            f"<td style='color:{price_color};font-weight:bold'>"
            f"¥{d['price_jpy']:,}{'&nbsp;★' if is_hist_low else ''}</td>"
        )

        tier = d.get("tier", "buy")
        tier_cell = (
            "<td style='color:#c00;font-weight:bold'>🔥 かなり安い</td>"
            if tier == "great" else
            "<td style='color:#2a7;font-weight:bold'>✅ 買いライン</td>"
        )

        level_label = PRICE_LEVEL_LABEL.get(d.get("price_level", ""), "")
        tr = d.get("typical_range", [])
        typical = f"¥{tr[0]:,}〜¥{tr[1]:,}" if len(tr) == 2 else "—"

        promo_html = ""
        for tag in d.get("promo_tags", []):
            promo_html += f"<br><span style='color:orange;font-size:11px'>🏷 {tag}</span>"

        dep_str = _fmt_date(d['depart_date'])
        ret_str = _fmt_date(d['return_date'])

        # フライト詳細
        orig_ap = d.get("origin_ap", "") or os.environ.get("ORIGIN", "NRT")
        dest_ap = d.get("dest_ap", "") or DESTINATION
        dep_t   = d.get("depart_time", "")
        arr_t   = d.get("arrive_time", "")
        dur     = d.get("duration_min")
        fno     = d.get("flight_number", "")
        direct  = d.get("is_direct", True)
        ret_dep = d.get("ret_depart_time", "")
        ret_arr = d.get("ret_arrive_time", "")
        ret_dur = d.get("ret_duration_min")
        ret_fno = d.get("ret_flight_number", "")

        def _fc(ap_f, t_f, ap_t, t_t, dm, fn):
            if not t_f: return "—"
            d_str = f"{dm//60}h{dm%60:02d}m" if dm else ""
            fn_s  = f" <span style='color:#999;font-size:11px'>{fn}</span>" if fn else ""
            return f"<b>{ap_f}</b> {t_f}→<b>{ap_t}</b> {t_t} {d_str}{fn_s}"

        direct_badge = (
            "<span style='color:#2a7;font-size:11px'>✈直行</span>" if direct
            else "<span style='color:#e65;font-size:11px'>🔄乗継</span>"
        )
        go_str  = _fc(orig_ap, dep_t, dest_ap, arr_t, dur, fno)
        ret_str2 = _fc(dest_ap, ret_dep, orig_ap, ret_arr, ret_dur, ret_fno)

        rows += (
            f"<tr>"
            f"<td>{dep_str}</td>"
            f"<td>{ret_str}</td>"
            f"<td>{d['nights']}泊</td>"
            f"{price_cell}"
            f"{tier_cell}"
            f"<td>行: {go_str}<br>帰: {ret_str2}<br>{direct_badge}</td>"
            f"<td>{level_label}</td>"
            f"<td>{typical}</td>"
            f"<td>{low}</td>"
            f"<td>{avg}</td>"
            f"<td>{d['airline']}{promo_html}</td>"
            f"<td><a href='{d['deep_link']}'>検索</a></td>"
            f"</tr>"
        )
    return rows


def _email_table(rows_html: str, threshold_great: int, threshold_buy: int) -> str:
    return (
        "<html><body style='font-family:sans-serif'>"
        "<h2>✈️ ソウル格安便が見つかりました</h2>"
        f"<p>🔥 かなり安い: <strong>¥{threshold_great:,}以下</strong> ／ "
        f"✅ 買いライン: <strong>¥{threshold_buy:,}以下</strong>　"
        "<span style='color:red'>★ = 過去最安値</span></p>"
        "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-size:14px'>"
        "<thead style='background:#f0f0f0'><tr>"
        "<th>出発日</th><th>帰国日</th><th>泊数</th>"
        "<th>現在価格</th><th>ランク</th><th>時刻/所要時間</th><th>価格レベル</th><th>典型価格帯</th>"
        "<th>底値</th><th>平均価格</th>"
        "<th>航空会社</th><th>リンク</th>"
        f"</tr></thead><tbody>{rows_html}</tbody></table>"
        "<p style='color:gray;font-size:11px;margin-top:16px'>"
        "価格レベル・典型価格帯はGoogle Flights提供。底値・平均は計測開始以降の履歴。"
        "Seoul Flight Checker が自動送信しました</p>"
        "</body></html>"
    )


def send_test_email():
    alert_to         = os.environ["ALERT_TO"]
    smtp_user        = os.environ["SMTP_USER"]
    threshold_great  = int(os.environ.get("THRESHOLD_GREAT", 18000))
    threshold_buy    = int(os.environ.get("THRESHOLD_BUY",   25000))

    sample_deals = [
        {
            "depart_date": "2026-07-04", "return_date": "2026-07-08",
            "nights": 4, "price_jpy": 16800, "airline": "Jeju Air",
            "deep_link": "https://www.google.com/travel/flights",
            "stats": {"low": 16800, "avg": 28500, "count": 12},
            "price_level": "low", "typical_range": [22000, 38000],
            "promo_tags": ["Summer Sale -20%"],
            "tier": "great",
        },
        {
            "depart_date": "2026-07-11", "return_date": "2026-07-14",
            "nights": 3, "price_jpy": 23400, "airline": "T'way Air",
            "deep_link": "https://www.google.com/travel/flights",
            "stats": {"low": 21000, "avg": 31200, "count": 8},
            "price_level": "typical", "typical_range": [20000, 35000],
            "promo_tags": [],
            "tier": "buy",
        },
    ]
    rows = _deal_rows_html(sample_deals)
    body_html = (
        "<html><body style='font-family:sans-serif'>"
        "<h2>✈️ Seoul Flight Checker — 接続テスト</h2>"
        "<p>メール通知の設定は正常です。実際のアラートはこのような形式で届きます（以下はサンプルデータ）。</p>"
        + _email_table(rows, threshold_great, threshold_buy).replace("<html><body style='font-family:sans-serif'>", "")
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


def send_alert_email(deals: list[dict], threshold_great: int, threshold_buy: int):
    alert_to  = os.environ["ALERT_TO"]
    great_cnt = sum(1 for d in deals if d.get("tier") == "great")
    buy_cnt   = len(deals) - great_cnt
    parts = []
    if great_cnt: parts.append(f"🔥かなり安い {great_cnt}件")
    if buy_cnt:   parts.append(f"✅買いライン {buy_cnt}件")
    subject = f"✈️ ソウル格安便！ {' / '.join(parts)}"

    rows      = _deal_rows_html(deals)
    body_html = _email_table(rows, threshold_great, threshold_buy)

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
    origin          = os.environ.get("ORIGIN", "NRT")
    days_start      = int(os.environ.get("DAYS_START", 7))
    days_ahead      = int(os.environ.get("DAYS_AHEAD", 21))
    days_step       = int(os.environ.get("DAYS_STEP",  7))
    threshold_great = int(os.environ.get("THRESHOLD_GREAT", 18000))
    threshold_buy   = int(os.environ.get("THRESHOLD_BUY",   25000))
    durations       = [int(d) for d in os.environ.get("TRIP_DURATIONS", "3,5").split(",")]

    today = date.today()
    deals_found = []

    # TARGET_WEEKDAYS が指定されていれば曜日指定、なければ step 刻み
    target_wdays_str = os.environ.get("TARGET_WEEKDAYS", "")
    if target_wdays_str:
        target_wdays = set(int(x) for x in target_wdays_str.split(","))
        depart_dates = [
            today + timedelta(days=i)
            for i in range(days_start, days_ahead + 1)
            if (today + timedelta(days=i)).weekday() in target_wdays
        ]
        label = f"{days_start}〜{days_ahead}日後 曜日指定"
    else:
        depart_dates = [today + timedelta(days=i) for i in range(days_start, days_ahead + 1, days_step)]
        label = f"{days_start}〜{days_ahead}日後 step{days_step}"

    cleanup_old_records()
    log.info("=== チェック開始: %s → %s %s (🔥¥%s / ✅¥%s) ===",
             origin, DESTINATION, label, f"{threshold_great:,}", f"{threshold_buy:,}")

    for depart in depart_dates:
        for nights in durations:
            ret = depart + timedelta(days=nights)
            result = fetch_cheapest_flight(origin, DESTINATION, depart, ret)
            if result is None:
                continue

            price     = result["price_jpy"]
            airline   = result["airline"]
            deep_link = result["deep_link"]

            save_price(origin, DESTINATION, depart, ret, price, airline, deep_link,
                       depart_time=result.get("depart_time"),
                       arrive_time=result.get("arrive_time"),
                       duration_min=result.get("duration_min"),
                       flight_number=result.get("flight_number"),
                       is_direct=int(result.get("is_direct", True)),
                       origin_ap=result.get("origin_ap"),
                       dest_ap=result.get("dest_ap"),
                       ret_depart_time=result.get("ret_depart_time"),
                       ret_arrive_time=result.get("ret_arrive_time"),
                       ret_duration_min=result.get("ret_duration_min"),
                       ret_flight_number=result.get("ret_flight_number"))

            stats      = get_price_stats(origin, DESTINATION, depart, ret)
            is_new_low = stats["low"] and price < stats["low"]

            flag = " ★最安値更新!" if is_new_low else ""
            log.info("  %s → %s (%d泊) ¥%s [%s]%s",
                     depart, ret, nights, f"{price:,}", airline, flag)

            if price <= threshold_buy:
                tier = "great" if price <= threshold_great else "buy"
                deals_found.append({**result, "nights": nights, "stats": stats, "tier": tier})

    if deals_found:
        log.info("閾値以下の便が %d 件見つかりました。", len(deals_found))
        if _email_configured():
            send_alert_email(deals_found, threshold_great, threshold_buy)
        else:
            log.warning("メール未設定のため通知をスキップ。.env に SMTP_USER/SMTP_PASS/ALERT_TO を追加してください。")
    else:
        log.info("閾値以下の便は見つかりませんでした。")

    log.info("=== チェック完了 ===")


# ── HTMLレポート ──────────────────────────────────────────────────────────────

def generate_report(out_path: str = "index.html"):
    """prices.db から最新価格一覧の index.html を生成する。"""
    today           = datetime.now().strftime("%Y-%m-%d %H:%M JST")
    threshold_great = int(os.environ.get("THRESHOLD_GREAT", 18000))
    threshold_buy   = int(os.environ.get("THRESHOLD_BUY",   25000))

    with sqlite3.connect(DB_PATH) as conn:
        # 各フライト(出発日・帰着日)の最新チェック価格・底値・平均を取得
        rows = conn.execute("""
            SELECT
                h.depart_date,
                h.return_date,
                CAST(julianday(h.return_date) - julianday(h.depart_date) AS INTEGER) AS nights,
                h.price_jpy    AS latest_price,
                h.airline,
                h.deep_link,
                stats.low_price,
                stats.avg_price,
                stats.samples,
                h.depart_time,
                h.arrive_time,
                h.duration_min,
                h.flight_number,
                h.is_direct,
                COALESCE(h.origin_ap, ?) AS origin_ap,
                COALESCE(h.dest_ap,   ?) AS dest_ap,
                h.ret_depart_time,
                h.ret_arrive_time,
                h.ret_duration_min,
                h.ret_flight_number
            FROM price_history h
            JOIN (
                SELECT depart_date, return_date,
                       MIN(price_jpy)  AS low_price,
                       ROUND(AVG(price_jpy)) AS avg_price,
                       COUNT(*)        AS samples,
                       MAX(checked_at) AS latest_at
                FROM price_history
                WHERE origin=? AND destination=?
                  AND depart_date >= date('now')
                GROUP BY depart_date, return_date
            ) stats ON h.depart_date=stats.depart_date
                   AND h.return_date=stats.return_date
                   AND h.checked_at=stats.latest_at
                   AND h.origin=? AND h.destination=?
            ORDER BY h.price_jpy ASC
        """, (
            os.environ.get("ORIGIN", "NRT"), DESTINATION,
            os.environ.get("ORIGIN", "NRT"), DESTINATION,
            os.environ.get("ORIGIN", "NRT"), DESTINATION,
        )).fetchall()

    def price_class(price):
        if price <= threshold_great:          return "great"
        if price <= threshold_buy:            return "deal"
        if price <= threshold_buy * 1.2:      return "near"
        return "normal"

    def _day_cell(ds):
        d = date.fromisoformat(ds)
        w = WEEKDAY_JA[d.weekday()]
        cls_map = {5: "sat", 6: "sun", 4: "fri"}
        span = f"<span class='{cls_map[d.weekday()]}'>" if d.weekday() in cls_map else "<span>"
        return f"<td>{d}{span}({w})</span></td>"

    tbody = ""
    for depart, ret, nights, price, airline, link, low, avg, samples, \
        dep_t, arr_t, dur, fno, is_dir, orig_ap, dest_ap, \
        ret_dep_t, ret_arr_t, ret_dur, ret_fno in rows:
        cls     = price_class(price)
        low_str = f"¥{low:,}" if low else "—"
        avg_str = f"¥{avg:,}" if avg else "—"
        star    = " ★" if low and price <= low else ""

        def _flight_cell(ap_from, t_from, ap_to, t_to, d_min, fn):
            if not t_from:
                return "—"
            t   = f"<b>{ap_from}</b> {t_from} → <b>{ap_to}</b> {t_to}"
            dur = f"{d_min//60}h{d_min%60:02d}m" if d_min else ""
            fn2 = f"<span class='fno'>{fn}</span>" if fn else ""
            return f"{t}<br><small>{dur} {fn2}</small>"

        dir_badge = "<span class='direct'>直行</span>" if is_dir else "<span class='transit'>乗継</span>"
        go_cell  = _flight_cell(orig_ap, dep_t, dest_ap, arr_t, dur, fno)

        tbody += (
            f"<tr class='{cls}'>"
            + _day_cell(depart) + _day_cell(ret) + f"<td>{nights}泊</td>"
            f"<td class='price'>¥{price:,}{star}</td>"
            f"<td>{go_cell}<br>{dir_badge}</td>"
            f"<td>{low_str}</td><td>{avg_str}</td>"
            f"<td>{samples}</td><td>{airline}</td>"
            f"<td><a href='{link}' target='_blank'>検索</a></td>"
            f"</tr>"
        )

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>✈️ ソウル航空券 最安値一覧</title>
<style>
  body {{ font-family: sans-serif; max-width: 1000px; margin: 0 auto; padding: 16px; background: #f9f9f9; }}
  h1 {{ font-size: 1.4rem; }}
  .meta {{ color: #666; font-size: .85rem; margin-bottom: 12px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; font-size: .9rem; }}
  th {{ background: #333; color: #fff; padding: 8px 10px; text-align: left; cursor: pointer; }}
  th:hover {{ background: #555; }}
  td {{ padding: 7px 10px; border-bottom: 1px solid #eee; }}
  tr.great td {{ background: #fde8e8; }}
  tr.deal td {{ background: #e8f5e9; }}
  tr.near td {{ background: #fff9e6; }}
  tr:hover td {{ filter: brightness(.95); }}
  .price {{ font-weight: bold; }}
  tr.great .price {{ color: #c00; font-weight: bold; }}
  tr.deal .price {{ color: #2a7; }}
  tr.near .price {{ color: #e65; }}
  .sat {{ color: #1565c0; font-weight: bold; }}
  .sun {{ color: #c62828; font-weight: bold; }}
  .fri {{ color: #e65100; }}
  .direct {{ background:#e8f5e9; color:#2a7; border-radius:3px; padding:1px 4px; font-size:11px; }}
  .transit {{ background:#fff3e0; color:#e65; border-radius:3px; padding:1px 4px; font-size:11px; }}
  .fno {{ color:#999; font-size:11px; margin-left:4px; }}
  .legend {{ font-size: .8rem; margin-top: 8px; color: #555; }}
  input#filter {{ margin-bottom: 10px; padding: 6px; width: 200px; border: 1px solid #ccc; border-radius: 4px; }}
</style>
</head>
<body>
<h1>✈️ ソウル航空券 最安値一覧</h1>
<div class="meta">最終更新: {today}
  <span style="margin-left:12px">🔴 かなり安い(¥{threshold_great:,}以下) &nbsp; 🟩 買いライン(¥{threshold_buy:,}以下) &nbsp; 🟨 参考(¥{int(threshold_buy*1.2):,}以内)</span>
</div>
<input id="filter" type="text" placeholder="航空会社・日付で絞込…" oninput="filterTable(this.value)">
<table id="tbl">
<thead><tr>
  <th onclick="sort(0)">出発日 ↕</th>
  <th onclick="sort(1)">帰国日 ↕</th>
  <th onclick="sort(2)">泊数 ↕</th>
  <th onclick="sort(3)">現在価格 ↕</th>
  <th onclick="sort(4)">行き時刻/所要時間 ↕</th>
  <th onclick="sort(5)">底値 ↕</th>
  <th onclick="sort(6)">平均 ↕</th>
  <th onclick="sort(7)">記録数 ↕</th>
  <th onclick="sort(8)">航空会社 ↕</th>
  <th>リンク</th>
</tr></thead>
<tbody>{tbody}</tbody>
</table>
<div class="legend">★ = 過去最安値更新 ／ 🔴かなり安い(¥{threshold_great:,}以下) ／ 🟩買いライン(¥{threshold_buy:,}以下) ／ 底値・平均はツール計測開始以降の履歴</div>
<script>
let asc = {{}};
function sort(col) {{
  const tb = document.querySelector('#tbl tbody');
  const rows = [...tb.rows];
  asc[col] = !asc[col];
  rows.sort((a, b) => {{
    let av = a.cells[col].innerText.replace(/[¥,★]/g,'').trim();
    let bv = b.cells[col].innerText.replace(/[¥,★]/g,'').trim();
    const an = parseFloat(av), bn = parseFloat(bv);
    const cmp = isNaN(an) ? av.localeCompare(bv, 'ja') : an - bn;
    return asc[col] ? cmp : -cmp;
  }});
  rows.forEach(r => tb.appendChild(r));
}}
function filterTable(q) {{
  q = q.toLowerCase();
  for (const r of document.querySelectorAll('#tbl tbody tr'))
    r.style.display = r.innerText.toLowerCase().includes(q) ? '' : 'none';
}}
</script>
</body>
</html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    log.info("レポートを生成しました: %s (%d件)", out_path, len(rows))


# ── エントリーポイント ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Seoul Flight Price Checker")
    parser.add_argument("--test",   action="store_true", help="テストメール送信")
    parser.add_argument("--report", metavar="OUT", nargs="?", const="index.html",
                        help="HTML レポートを生成して終了 (デフォルト: index.html)")
    args = parser.parse_args()

    init_db()

    if args.test:
        _validate_env(require_serpapi=False)
        send_test_email()
        return

    if args.report:
        generate_report(args.report)
        return

    _validate_env(require_serpapi=True)

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
