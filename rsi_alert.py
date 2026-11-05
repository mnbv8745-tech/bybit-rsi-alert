import os
import json
import time
import subprocess
import urllib.parse
import requests
import pandas as pd

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

COINS = ["BTC", "ETH"]
TIMEFRAMES = ["15m", "1h", "4h", "1d"]

RSI_PERIOD = 14
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0

RESET_OVERSOLD = 34.0
RESET_OVERBOUGHT = 66.0

STATE_FILE = "state.json"
LOOP_DURATION_SECONDS = 50 * 60  # 50분간 연속 감시
CHECK_INTERVAL_SECONDS = 25      # 25초마다 체크

HEADERS = {"User-Agent": "Mozilla/5.0"}

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"텔레그램 전송 실패: {e}")

def fetch_futures_ohlcv(coin: str, tf: str) -> list:
    symbol = f"{coin}USDT"
    bybit_map = {"15m": "15", "1h": "60", "4h": "240", "1d": "D"}
    bybit_iv = bybit_map[tf]

    bybit_target = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval={bybit_iv}&limit=200"
    encoded_target = urllib.parse.quote(bybit_target, safe='')
    bybit_urls = [
        f"https://api.allorigins.win/raw?url={encoded_target}",
        f"https://api.codetabs.com/v1/proxy?quest={encoded_target}",
        bybit_target,
        f"https://api.bytick.com/v5/market/kline?category=linear&symbol={symbol}&interval={bybit_iv}&limit=200"
    ]

    for url in bybit_urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=8)
            if r.status_code == 200:
                data = r.json()
                raw_list = data.get("result", {}).get("list", [])
                if raw_list and len(raw_list) >= RSI_PERIOD + 10:
                    rows = [
                        [int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])]
                        for x in raw_list
                    ]
                    rows.sort(key=lambda x: x[0])
                    return rows
        except Exception:
            pass

    bitget_map = {"15m": "15m", "1h": "1H", "4h": "4H", "1d": "1Dutc"}
    bitget_url = (
        f"https://api.bitget.com/api/v2/mix/market/candles"
        f"?symbol={symbol}&productType=USDT-FUTURES&granularity={bitget_map[tf]}&limit=200"
    )
    try:
        r = requests.get(bitget_url, headers=HEADERS, timeout=8)
        if r.status_code == 200:
            data = r.json()
            raw_list = data.get("data", [])
            if raw_list and len(raw_list) >= RSI_PERIOD + 10:
                rows = [
                    [int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])]
                    for x in raw_list
                ]
                rows.sort(key=lambda x: x[0])
                return rows
    except Exception:
        pass

    okx_url = f"https://www.okx.com/api/v5/market/candles?instId={coin}-USDT-SWAP&bar={bitget_map[tf]}&limit=200"
    r = requests.get(okx_url, headers=HEADERS, timeout=8)
    data = r.json()
    raw_list = data.get("data", [])
    rows = [
        [int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])]
        for x in raw_list
    ]
    rows.sort(key=lambda x: x[0])
    return rows

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and data.get("_initialized"):
                    return data, False
        except Exception:
            pass
    return {"_initialized": True}, True

def save_state_to_github(state):
    state["_initialized"] = True
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False)
        subprocess.run(["git", "add", STATE_FILE], check=False)
        subprocess.run(["git", "commit", "-m", "Update RSI state [skip ci]"], check=False)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
        subprocess.run(["git", "push", "origin", "HEAD:main"], check=False)
    except Exception as e:
        print(f"Git 상태 저장 오류: {e}")

def trigger_next_run():
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return
    url = f"https://api.github.com/repos/{repo}/actions/workflows/rsi_alert.yml/dispatches"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json"
    }
    try:
        requests.post(url, headers=headers, json={"ref": "main"}, timeout=8)
    except Exception as e:
        print(f"다음 실행 호출 실패: {e}")

def calc_rsi_series(close_series: pd.Series, period: int = 14) -> pd.Series:
    delta = close_series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def get_candle_rsi_at(df: pd.DataFrame, idx: int, period: int = 14):
    sub_df = df.iloc[:len(df) + idx + 1].copy() if idx < -1 else df.copy()
    close_rsi = float(calc_rsi_series(sub_df['close'], period).iloc[-1])

    low_series = sub_df['close'].copy()
    low_series.iloc[-1] = sub_df['low'].iloc[-1]
    low_rsi = float(calc_rsi_series(low_series, period).iloc[-1])

    high_series = sub_df['close'].copy()
    high_series.iloc[-1] = sub_df['high'].iloc[-1]
    high_rsi = float(calc_rsi_series(high_series, period).iloc[-1])

    return round(close_rsi, 2), round(low_rsi, 2), round(high_rsi, 2), int(sub_df['timestamp'].iloc[-1])

def check_market(state: dict) -> bool:
    state_changed = False

    for coin in COINS:
        for tf in TIMEFRAMES:
            key = f"{coin}_{tf}"
            try:
                ohlcv = fetch_futures_ohlcv(coin, tf)
                if not ohlcv or len(ohlcv) < RSI_PERIOD + 10:
                    continue

                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                curr_rsi, curr_low_rsi, curr_high_rsi, curr_candle_time = get_candle_rsi_at(df, -1, RSI_PERIOD)
                curr_price = float(df['close'].iloc[-1])

                item_state = state.get(key, {"status": "NORMAL", "candle_time": 0})
                status = item_state.get("status", "NORMAL")
                last_alert_candle = item_state.get("candle_time", 0)

                # 1. 정상 범위(34~66) 복귀 시 잠금 해제
                if status == "OVERSOLD" and curr_rsi >= RESET_OVERSOLD:
                    status = "NORMAL"
                    state[key] = {"status": "NORMAL", "candle_time": last_alert_candle}
                    state_changed = True

                elif status == "OVERBOUGHT" and curr_rsi <= RESET_OVERBOUGHT:
                    status = "NORMAL"
                    state[key] = {"status": "NORMAL", "candle_time": last_alert_candle}
                    state_changed = True

                # 2. 현재 봉(-1)부터 최근 지나간 봉(-2, -3)까지 스캔하여 터치 포착
                lookback_indices = [-1] if last_alert_candle == 0 else [-3, -2, -1]

                if status != "OVERSOLD":
                    for idx in lookback_indices:
                        c_rsi, l_rsi, _, c_time = get_candle_rsi_at(df, idx, RSI_PERIOD)
                        if c_time <= last_alert_candle:
                            continue
                        if c_rsi <= RSI_OVERSOLD or l_rsi <= RSI_OVERSOLD:
                            min_r = min(c_rsi, l_rsi)
                            if idx == -1:
                                touch_info = f"{curr_rsi}" if curr_rsi <= RSI_OVERSOLD else f"{min_r} (꼬리 터치 후 반등, 현재 {curr_rsi})"
                            else:
                                touch_info = f"{min_r} (최근 마감봉 터치 포착, 현재 {curr_rsi})"
                            msg = (
                                f"🟢 *[RSI 30 이하 터치]*\n"
                                f"• 코인: *{coin}* (USDT 선물)\n"
                                f"• 주기: *{tf}*\n"
                                f"• RSI: *{touch_info}*\n"
                                f"• 현재가: {curr_price:,.2f} USDT"
                            )
                            send_telegram(msg)
                            state[key] = {"status": "OVERSOLD", "candle_time": curr_candle_time}
                            state_changed = True
                            break

                if state.get(key, {}).get("status") != "OVERBOUGHT" and status != "OVERBOUGHT":
                    for idx in lookback_indices:
                        c_rsi, _, h_rsi, c_time = get_candle_rsi_at(df, idx, RSI_PERIOD)
                        if c_time <= last_alert_candle:
                            continue
                        if c_rsi >= RSI_OVERBOUGHT or h_rsi >= RSI_OVERBOUGHT:
                            max_r = max(c_rsi, h_rsi)
                            if idx == -1:
                                touch_info = f"{curr_rsi}" if curr_rsi >= RSI_OVERBOUGHT else f"{max_r} (꼬리 터치 후 조정, 현재 {curr_rsi})"
                            else:
                                touch_info = f"{max_r} (최근 마감봉 터치 포착, 현재 {curr_rsi})"
                            msg = (
                                f"🔴 *[RSI 70 이상 터치]*\n"
                                f"• 코인: *{coin}* (USDT 선물)\n"
                                f"• 주기: *{tf}*\n"
                                f"• RSI: *{touch_info}*\n"
                                f"• 현재가: {curr_price:,.2f} USDT"
                            )
                            send_telegram(msg)
                            state[key] = {"status": "OVERBOUGHT", "candle_time": curr_candle_time}
                            state_changed = True
                            break

            except Exception as e:
                print(f"오류 ({key}): {e}")

    return state_changed

def main():
    state, is_first_run = load_state()

    changed = check_market(state)
    if changed or is_first_run:
        save_state_to_github(state)

    start_time = time.time()
    while time.time() - start_time < LOOP_DURATION_SECONDS:
        time.sleep(CHECK_INTERVAL_SECONDS)
        changed = check_market(state)
        if changed:
            save_state_to_github(state)

    trigger_next_run()

if __name__ == "__main__":
    main()
