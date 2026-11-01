import os
import json
import time
import subprocess
import requests
import ccxt
import pandas as pd

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
TIMEFRAMES = ["15m", "1h", "4h", "1d"]

RSI_PERIOD = 14
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0

# 재알림을 위한 정상 구간 복귀 기준
RESET_OVERSOLD = 32.0
RESET_OVERBOUGHT = 68.0

STATE_FILE = "state.json"
LOOP_DURATION_SECONDS = 55 * 60  # 1회 실행 시 55분간 연속 감시
CHECK_INTERVAL_SECONDS = 30      # 30초마다 체크

exchange = ccxt.bybit({
    'enableRateLimit': True,
    'options': {'defaultType': 'linear'}
})

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

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f), False
        except Exception:
            pass
    return {}, True

def save_state_to_github(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False)
        subprocess.run(["git", "add", STATE_FILE], check=False)
        subprocess.run(["git", "commit", "-m", "Update RSI state [skip ci]"], check=False)
        subprocess.run(["git", "pull", "--rebase"], check=False)
        subprocess.run(["git", "push"], check=False)
    except Exception as e:
        print(f"Git 상태 저장 오류: {e}")

def calc_rsi_series(close_series: pd.Series, period: int = 14) -> pd.Series:
    delta = close_series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def get_rsi_metrics(df: pd.DataFrame, period: int = 14):
    rsi_close = calc_rsi_series(df['close'], period)
    curr_rsi = float(rsi_close.iloc[-1])
    prev_rsi = float(rsi_close.iloc[-2])

    # 아래꼬리(Low) 찍었을 때의 순간 최저 RSI 계산
    low_series = df['close'].copy()
    low_series.iloc[-1] = df['low'].iloc[-1]
    low_rsi = float(calc_rsi_series(low_series, period).iloc[-1])

    # 위꼬리(High) 찍었을 때의 순간 최고 RSI 계산
    high_series = df['close'].copy()
    high_series.iloc[-1] = df['high'].iloc[-1]
    high_rsi = float(calc_rsi_series(high_series, period).iloc[-1])

    return round(curr_rsi, 2), round(low_rsi, 2), round(high_rsi, 2), round(prev_rsi, 2)

def check_market(state: dict) -> bool:
    state_changed = False

    for symbol in SYMBOLS:
        coin = symbol.split('/')[0]
        for tf in TIMEFRAMES:
            key = f"{coin}_{tf}"
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=100)
                if len(ohlcv) < RSI_PERIOD + 2:
                    continue

                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                curr_rsi, low_rsi, high_rsi, prev_rsi = get_rsi_metrics(df, RSI_PERIOD)

                curr_candle_time = int(df['timestamp'].iloc[-1])
                curr_price = float(df['close'].iloc[-1])

                item_state = state.get(key, {"status": "NORMAL", "candle_time": 0})
                status = item_state["status"]
                last_candle_time = item_state["candle_time"]

                # 정상 범위(32~68) 복귀 시 알림 잠금 해제
                if status == "OVERSOLD" and curr_candle_time != last_candle_time:
                    if prev_rsi >= RESET_OVERSOLD and curr_rsi > RSI_OVERSOLD:
                        status = "NORMAL"
                        state[key] = {"status": "NORMAL", "candle_time": curr_candle_time}
                        state_changed = True

                elif status == "OVERBOUGHT" and curr_candle_time != last_candle_time:
                    if prev_rsi <= RESET_OVERBOUGHT and curr_rsi < RSI_OVERBOUGHT:
                        status = "NORMAL"
                        state[key] = {"status": "NORMAL", "candle_time": curr_candle_time}
                        state_changed = True

                # 30 이하 최초 돌파/꼬리 터치 검사
                if status != "OVERSOLD" and (curr_rsi <= RSI_OVERSOLD or low_rsi <= RSI_OVERSOLD):
                    if prev_rsi > RSI_OVERSOLD or last_candle_time == 0:
                        touch_info = f"{curr_rsi}" if curr_rsi <= RSI_OVERSOLD else f"{low_rsi} (꼬리 터치 후 반등, 현재 {curr_rsi})"
                        msg = (
                            f"🟢 *[RSI 30 이하 터치]*\n"
                            f"• 코인: *{coin}* (Bybit 선물)\n"
                            f"• 주기: *{tf}*\n"
                            f"• RSI: *{touch_info}*\n"
                            f"• 현재가: {curr_price:,.2f} USDT"
                        )
                        send_telegram(msg)
                        state[key] = {"status": "OVERSOLD", "candle_time": curr_candle_time}
                        state_changed = True
                        print(f"[{key}] 과매도 알림 발송 완료")

                # 70 이상 최초 돌파/꼬리 터치 검사
                elif status != "OVERBOUGHT" and (curr_rsi >= RSI_OVERBOUGHT or high_rsi >= RSI_OVERBOUGHT):
                    if prev_rsi < RSI_OVERBOUGHT or last_candle_time == 0:
                        touch_info = f"{curr_rsi}" if curr_rsi >= RSI_OVERBOUGHT else f"{high_rsi} (꼬리 터치 후 조정, 현재 {curr_rsi})"
                        msg = (
                            f"🔴 *[RSI 70 이상 터치]*\n"
                            f"• 코인: *{coin}* (Bybit 선물)\n"
                            f"• 주기: *{tf}*\n"
                            f"• RSI: *{touch_info}*\n"
                            f"• 현재가: {curr_price:,.2f} USDT"
                        )
                        send_telegram(msg)
                        state[key] = {"status": "OVERBOUGHT", "candle_time": curr_candle_time}
                        state_changed = True
                        print(f"[{key}] 과매수 알림 발송 완료")

            except Exception as e:
                print(f"오류 ({key}): {e}")

    return state_changed

def main():
    state, is_first_run = load_state()

    if is_first_run:
        send_telegram("✅ *Bybit RSI 실시간 감시 봇 세팅 완료*\n• 종목: BTC, ETH\n• 주기: 15m, 1h, 4h\n• 조건: RSI 30·70 최초 터치 (꼬리 포함)")
        save_state_to_github(state)

    start_time = time.time()
    while time.time() - start_time < LOOP_DURATION_SECONDS:
        changed = check_market(state)
        if changed:
            save_state_to_github(state)
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
