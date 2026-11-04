import os
import json
import time
import subprocess
import requests
import ccxt
import pandas as pd

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "")

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
TIMEFRAMES = ["15m", "1h", "4h", "1d"]

RSI_PERIOD = 14
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0

# RSI가 34 이상(또는 66 이하)으로 회복되면 즉시 다음 알람 대기 상태로 잠금 해제
RESET_OVERSOLD = 34.0
RESET_OVERBOUGHT = 66.0

STATE_FILE = "state.json"
LOOP_DURATION_SECONDS = 50 * 60  # 50분간 연속 감시
CHECK_INTERVAL_SECONDS = 25      # 25초마다 체크

# 미국 깃허브 서버 IP 차단(403)을 피하기 위해 bytick.com 우회 도메인 사용
exchange_primary = ccxt.bybit({
    'hostname': 'bytick.com',
    'enableRateLimit': True,
    'options': {'defaultType': 'linear'}
})

exchange_fallback = ccxt.bybit({
    'enableRateLimit': True,
    'options': {'defaultType': 'linear'}
})

def fetch_ohlcv_safe(symbol: str, tf: str, limit: int = 100):
    try:
        return exchange_primary.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
    except Exception:
        return exchange_fallback.fetch_ohlcv(symbol, timeframe=tf, limit=limit)

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

def get_candle_rsi(df: pd.DataFrame, period: int = 14):
    curr_rsi = float(calc_rsi_series(df['close'], period).iloc[-1])

    low_series = df['close'].copy()
    low_series.iloc[-1] = df['low'].iloc[-1]
    low_rsi = float(calc_rsi_series(low_series, period).iloc[-1])

    high_series = df['close'].copy()
    high_series.iloc[-1] = df['high'].iloc[-1]
    high_rsi = float(calc_rsi_series(high_series, period).iloc[-1])

    return round(curr_rsi, 2), round(low_rsi, 2), round(high_rsi, 2)

def check_market(state: dict, send_summary: bool = False) -> bool:
    state_changed = False
    summary_lines = ["📊 *[Bybit RSI 실시간 가동 점검]*"]

    for symbol in SYMBOLS:
        coin = symbol.split('/')[0]
        tf_texts = []
        for tf in TIMEFRAMES:
            key = f"{coin}_{tf}"
            try:
                ohlcv = fetch_ohlcv_safe(symbol, tf, limit=100)
                if not ohlcv or len(ohlcv) < RSI_PERIOD + 2:
                    continue

                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                curr_rsi, low_rsi, high_rsi = get_candle_rsi(df, RSI_PERIOD)
                curr_candle_time = int(df['timestamp'].iloc[-1])
                curr_price = float(df['close'].iloc[-1])

                tf_texts.append(f"{tf}: `{curr_rsi}`")

                item_state = state.get(key, {"status": "NORMAL", "candle_time": 0})
                status = item_state.get("status", "NORMAL")
                last_candle_time = item_state.get("candle_time", 0)

                # 1. 정상 범위(34~66) 복귀 시 즉시 잠금 해제
                if status == "OVERSOLD" and curr_rsi >= RESET_OVERSOLD:
                    status = "NORMAL"
                    state[key] = {"status": "NORMAL", "candle_time": curr_candle_time}
                    state_changed = True

                elif status == "OVERBOUGHT" and curr_rsi <= RESET_OVERBOUGHT:
                    status = "NORMAL"
                    state[key] = {"status": "NORMAL", "candle_time": curr_candle_time}
                    state_changed = True

                # 2. 과매도(30 이하) 감시 (직전 봉 조건 없이 잠금 상태가 아니면 즉시 발송)
                if status != "OVERSOLD":
                    is_oversold_now = (curr_rsi <= RSI_OVERSOLD)
                    is_wick_oversold = (curr_candle_time != last_candle_time) and (low_rsi <= RSI_OVERSOLD)

                    if is_oversold_now or is_wick_oversold:
                        touch_info = f"{curr_rsi}" if is_oversold_now else f"{low_rsi} (꼬리 터치 후 반등, 현재 {curr_rsi})"
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

                # 3. 과매수(70 이상) 감시
                elif status != "OVERBOUGHT":
                    is_overbought_now = (curr_rsi >= RSI_OVERBOUGHT)
                    is_wick_overbought = (curr_candle_time != last_candle_time) and (high_rsi >= RSI_OVERBOUGHT)

                    if is_overbought_now or is_wick_overbought:
                        touch_info = f"{curr_rsi}" if is_overbought_now else f"{high_rsi} (꼬리 터치 후 조정, 현재 {curr_rsi})"
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
                tf_texts.append(f"{tf}: 에러")

        if tf_texts:
            summary_lines.append(f"• *{coin}*: " + " | ".join(tf_texts))

    if send_summary and len(summary_lines) > 1:
        send_telegram("\n".join(summary_lines))

    return state_changed

def main():
    state, is_first_run = load_state()

    # 수동으로 Run workflow를 누른 경우, 이전 잠금을 모두 초기화하고 현재 RSI 현황판을 즉시 전송
    is_manual = (EVENT_NAME == "workflow_dispatch")
    if is_manual:
        state = {}
        state_changed = check_market(state, send_summary=True)
        save_state_to_github(state)
    else:
        state_changed = check_market(state, send_summary=is_first_run)
        if state_changed or is_first_run:
            save_state_to_github(state)

    start_time = time.time()
    while time.time() - start_time < LOOP_DURATION_SECONDS:
        time.sleep(CHECK_INTERVAL_SECONDS)
        changed = check_market(state, send_summary=False)
        if changed:
            save_state_to_github(state)

if __name__ == "__main__":
    main()
