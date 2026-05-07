# Seoul Flight Checker ✈️

東京↔ソウルの往復航空券を定期チェックし、設定した閾値を下回ったらメールで通知するツールです。

## 仕組み

```
[毎日 08:00 / 20:00 の2回チェック]
      ↓
[SerpAPI → Google Flights 検索]
  - 出発: 東京 (TYO)  ※目的地はソウル (SEL) 固定
  - 1週間刻みで今日から90日先まで
  - 3/4/5/7泊のパターンを検索
      ↓
[SQLite に価格履歴を保存]
      ↓
[閾値以下の便があれば → メール通知]
```

## セットアップ

### 1. 依存パッケージのインストール

```bash
pip install -r requirements.txt
```

### 2. 環境変数の設定

```bash
cp .env.example .env
```

`.env` を編集して以下を設定してください：

| 変数 | 説明 |
|------|------|
| `SERPAPI_KEY` | [SerpAPI](https://serpapi.com/) のAPIキー（月100回まで無料） |
| `SMTP_USER` | 送信元メールアドレス（Gmailの場合はアプリパスワードを使用） |
| `SMTP_PASS` | SMTPパスワード |
| `ALERT_TO` | 通知先メールアドレス |
| `PRICE_THRESHOLD` | アラートを送る価格の上限（円）。デフォルト: 50000 |
| `DAYS_AHEAD` | 何日先まで検索するか。デフォルト: 90 |
| `TRIP_DURATIONS` | 検索する泊数（カンマ区切り）。デフォルト: 3,4,5,7 |

チェック時刻は `flight_checker.py` 内の `CHECK_TIMES = ("08:00", "20:00")` で変更できます。

### Gmail アプリパスワードの取得

1. Google アカウント → セキュリティ → 2段階認証を有効化
2. 「アプリパスワード」を生成
3. 生成された16桁のパスワードを `SMTP_PASS` に設定

### 3. 実行

```bash
python flight_checker.py
```

起動直後に1回チェックを実行し、以降は `CHECK_INTERVAL_MINUTES` の間隔で繰り返します。

## 価格履歴の確認

SQLite データベース (`prices.db`) に全チェック履歴が保存されます。

```bash
# 最安値トップ10を表示
sqlite3 prices.db "SELECT depart_date, return_date, price_jpy, airline FROM price_history ORDER BY price_jpy LIMIT 10;"
```

## 定期実行（サーバー/常時起動PC の場合）

バックグラウンドで常時実行する場合：

```bash
nohup python flight_checker.py > /dev/null 2>&1 &
```

またはsystemdサービスとして登録することも可能です。

## ログ

実行ログは `flight_checker.log` に記録されます。
