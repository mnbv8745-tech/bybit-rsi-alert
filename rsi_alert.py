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

# 확실하게 반등/조정 후 다시 눌릴 때만 재알림이 오도록 복귀 기준을 35 / 65로 설정
RESET_OVERSOLD = 35.0
RESET_OVERBOUGHT = 65.0

STATE_FILE = "state.json"
LOOP_DURATION_SECONDS = 25 * 60  # 25분 주기 회전 (대기열 이어달리기 최적화)
CHECK_INTERVAL_SECONDS = 20      # 20초마다 촘촘하게 체크

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

def get_candle_rsi_extremes(df: pd.DataFrame, idx: int, period: int = 14):
    """특정 캔들(idx: -1 현재봉, -2 직전봉)의 종가/저가꼬리/고가꼬리 RSI 계산"""
    sub_df = df.iloc[:len(df) + idx + 1].copy() if idx < -1 else df.copy()
    close_rsi = float(calc_rsi_series(sub_df['close'], period).iloc[-1])

    low_series = sub_df['close'].copy()
    low_series.iloc[-1] = sub_df['low'].iloc[-1]
    low_rsi = float(calc_rsi_series(low_series, period).iloc[-1])

    high_series = sub_df['close'].copy()
    high_series.iloc[-1] = sub_df['high'].iloc[-1]
    high_rsi = float(calc_rsi_series(high_series, period).iloc[-1])

    return round(close_rsi, 2), round(low_rsi, 2), round(high_rsi, 2)

def check_market(state: dict) -> bool:
    state_changed = False

    for symbol in SYMBOLS:
        coin = symbol.split('/')[0]
        for tf in TIMEFRAMES:
            key = f"{coin}_{tf}"
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=100)
                if len(ohlcv) < RSI_PERIOD + 5:
                    continue

                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                # 현재 진행 봉(-1) 및 직전 마감 봉(-2)의 RSI 및 꼬리 RSI 계산
                curr_rsi, curr_low_rsi, curr_high_rsi = get_candle_rsi_extremes(df, -1, RSI_PERIOD)
                prev_rsi, prev_low_rsi, prev_high_rsi = get_candle_rsi_extremes(df, -2, RSI_PERIOD)

                curr_candle_time = int(df['timestamp'].iloc[-1])
                prev_candle_time = int(df['timestamp'].iloc[-2])
                curr_price = float(df['close'].iloc[-1])

                item_state = state.get(key, {"status": "NORMAL", "candle_time": 0})
                status = item_state["status"]
                last_alert_candle = item_state["candle_time"]

                # 1. 정상 범위(35~65)로 확실히 복귀 시 알림 잠금 해제
                if status == "OVERSOLD" and curr_candle_time > last_alert_candle:
                    if curr_rsi >= RESET_OVERSOLD and curr_low_rsi > RSI_OVERSOLD:
                        status = "NORMAL"
                        state[key] = {"status": "NORMAL", "candle_time": last_alert_candle}
                        state_changed = True

                elif status == "OVERBOUGHT" and curr_candle_time > last_alert_candle:
                    if curr_rsi <= RESET_OVERBOUGHT and curr_high_rsi < RSI_OVERBOUGHT:
                        status = "NORMAL"
                        state[key] = {"status": "NORMAL", "candle_time": last_alert_candle}
                        state_changed = True

                # 2. 과매도(30 이하) 검사: 현재 봉 또는 (봇 교체 시 놓친) 직전 봉에서 터치했는지 확인
                if status != "OVERSOLD":
                    curr_touched = (curr_rsi <= RSI_OVERSOLD or curr_low_rsi <= RSI_OVERSOLD)
                    prev_missed = (prev_candle_time > last_alert_candle) and (prev_rsi <= RSI_OVERSOLD or prev_low_rsi <= RSI_OVERSOLD)

                    if curr_touched or prev_missed:
                        if curr_touched:
                            min_r = min(curr_rsi, curr_low_rsi)
                            touch_info = f"{curr_rsi}" if curr_rsi <= RSI_OVERSOLD else f"{min_r} (꼬리 터치 후 반등, 현재 {curr_rsi})"
                            alert_candle = curr_candle_time
                        else:
                            min_r = min(prev_rsi, prev_low_rsi)
                            touch_info = f"{min_r} (직전 봉 터치 포착, 현재 {curr_rsi})"
                            alert_candle = prev_candle_time

                        msg = (
                            f"🟢 *[RSI 30 이하 터치]*\n"
                            f"• 코인: *{coin}* (Bybit 선물)\n"
                            f"• 주기: *{tf}*\n"
                            f"• RSI: *{touch_info}*\n"
                            f"• 현재가: {curr_price:,.2f} USDT"
                        )
                        send_telegram(msg)
                        state[key] = {"status": "OVERSOLD", "candle_time": alert_candle}
                        state_changed = True
                        print(f"[{key}] 과매도 알림 발송 완료")

                # 3. 과매수(70 이상) 검사: 현재 봉 또는 직전 봉에서 터치했는지 확인
                elif status != "OVERBOUGHT":
                    curr_touched = (curr_rsi >= RSI_OVERBOUGHT or curr_high_rsi >= RSI_OVERBOUGHT)
                    prev_missed = (prev_candle_time > last_alert_candle) and (prev_rsi >= RSI_OVERBOUGHT or prev_high_rsi >= RSI_OVERBOUGHT)

                    if curr_touched or prev_missed:
                        if curr_touched:
                            max_r = max(curr_rsi, curr_high_rsi)
                            touch_info = f"{curr_rsi}" if curr_rsi >= RSI_OVERBOUGHT else f"{max_r} (꼬리 터치 후 조정, 현재 {curr_rsi})"
                            alert_candle = curr_candle_time
                        else:
                            max_r = max(prev_rsi, prev_high_rsi)
                            touch_info = f"{max_r} (직전 봉 터치 포착, 현재 {curr_rsi})"
                            alert_candle = prev_candle_time

                        msg = (
                            f"🔴 *[RSI 70 이상 터치]*\n"
                            f"• 코인: *{coin}* (Bybit 선물)\n"
                            f"• 주기: *{tf}*\n"
                            f"• RSI: *{touch_info}*\n"
                            f"• 현재가: {curr_price:,.2f} USDT"
                        )
                        send_telegram(msg)
                        state[key] = {"status": "OVERBOUGHT", "candle_time": alert_candle}
                        state_changed = True
                        print(f"[{key}] 과매수 알림 발송 완료")

            except Exception as e:
                print(f"오류 ({key}): {e}")

    return state_changed

def main():
    state, is_first_run = load_state()

    if is_first_run:
        send_telegram("✅ *Bybit RSI 실시간 감시 봇 세팅 완료*\n• 종목: BTC, ETH\n• 주기: 15m, 1h, 4h, 1d\n• 조건: RSI 30·70 최초 터치 (꼬리 포함)")
        save_state_to_github(state)

    start_time = time.time()
    while time.time() - start_time < LOOP_DURATION_SECONDS:
        changed = check_market(state)
        if changed:
            save_state_to_github(state)
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
