import os
import argparse
import time
from datetime import datetime, time as dt_time
from typing import List, Tuple, Optional, Dict

import pandas as pd
import numpy as np
import requests


# ============================================================
# CONFIGURATION
# ============================================================
LOOKBACK_4H = 20
MIN_BARS = 25
MIN_VOLUME_RATIO = 1.20
STRONG_VOLUME_RATIO = 2.00
MIN_PRICE_CHANGE_PCT = 0.50
MIN_3BAR_CHANGE_PCT = 1.00
MIN_RANGE_EXPANSION = 1.10
BT_HORIZON_BARS = 1
CONSERVATIVE_AMBIGUOUS = True

YAHOO_RANGE = "1mo"
YAHOO_INTERVAL = "1h"
REQUEST_TIMEOUT = 15
REQUEST_DELAY = 0.15
DATE_PROMPT_TIMEOUT_SECONDS = 15


# ============================================================
# SCAN-DATE PROMPT
# ============================================================
def parse_scan_date(value: str):
    """Accept YYYY-MM-DD (preferred) or DD-MM-YYYY and return a date."""
    value = (value or "").strip()
    if not value:
        return datetime.now().date()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError("Invalid date. Use YYYY-MM-DD, for example 2026-10-01.")


def prompt_scan_date(timeout_seconds: int = DATE_PROMPT_TIMEOUT_SECONDS):
    """Prompt immediately; default to today after timeout or blank input."""
    today = datetime.now().date()
    prompt = (
        f"Enter scan date [YYYY-MM-DD] (press Enter for today; "
        f"auto-defaults to {today} after {timeout_seconds} seconds): "
    )

    # Windows: wait for console input without blocking the scanner forever.
    try:
        import threading
        import queue
        q = queue.Queue(maxsize=1)

        def _reader():
            try:
                q.put(input(prompt))
            except Exception:
                q.put("")

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        try:
            raw = q.get(timeout=timeout_seconds)
        except queue.Empty:
            print(f"\n[INFO] No date entered within {timeout_seconds} seconds. Using today: {today}")
            return today
    except Exception:
        # Non-interactive environments: use today rather than failing.
        print(f"[INFO] Interactive date prompt unavailable. Using today: {today}")
        return today

    raw = (raw or "").strip()
    if not raw:
        print(f"[INFO] No date entered. Using today: {today}")
        return today

    try:
        selected = parse_scan_date(raw)
    except ValueError as exc:
        print(f"[ERROR] {exc}")
        print(f"[INFO] Using today: {today}")
        return today

    if selected > today:
        print(f"[ERROR] Future scan date {selected} is not allowed. Using today: {today}")
        return today

    print(f"[OK] Requested scan date: {selected}")
    return selected


# ============================================================
# DYNAMIC NSE F&O UNIVERSE
# Logic retained from the older scanner.
# ============================================================
def get_fno_tickers() -> List[str]:
    urls_to_try = [
        "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv",
        "https://archives.nseindia.com/content/fo/fo_mktlots.csv",
    ]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    }

    session = requests.Session()
    session.headers.update(headers)

    try:
        session.get("https://www.nseindia.com", timeout=REQUEST_TIMEOUT)
    except Exception:
        pass

    excluded = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NAN"}

    for url in urls_to_try:
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()

            text_data = response.text.upper()
            if "SYMBOL" not in text_data and "UNDERLYING" not in text_data:
                continue

            lines = response.text.strip().splitlines()
            symbol_idx = None
            symbols = []

            for line in lines:
                if not line.strip():
                    continue

                parts = [p.strip().strip('"').upper() for p in line.split(",")]

                if symbol_idx is None:
                    if "SYMBOL" in parts:
                        symbol_idx = parts.index("SYMBOL")
                    elif "UNDERLYING" in parts:
                        symbol_idx = parts.index("UNDERLYING")
                    continue

                if symbol_idx is not None and len(parts) > symbol_idx:
                    sym = parts[symbol_idx]
                    if sym and sym not in excluded:
                        symbols.append(sym)

            symbols = list(dict.fromkeys(symbols))
            if symbols:
                print(f"[OK] F&O universe: {len(symbols)} symbols from {url}")
                return symbols

        except Exception as exc:
            print(f"[WARN] NSE universe source failed: {url} -> {exc}")

    raise RuntimeError(
        "Could not obtain the current NSE F&O universe. "
        "The scanner stops rather than silently using an outdated list."
    )


# ============================================================
# LIVE 1H DATA -> SESSION-ANCHORED 4H DATA
# ============================================================
class NSEFno4HourVCPMomentumScanner:
    """
    Latest RVME + VCP scanner.

    Data acquisition is taken from the older scanner:
      NSE dynamic F&O universe -> Yahoo Finance 1H data.

    Signal calculations are retained from the latest scanner:
      VCP + Reverse-VCP Momentum Expansion (RVME).
    """

    def __init__(self, watchlist=None):
        self.watchlist = watchlist if watchlist is not None else get_fno_tickers()

        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://finance.yahoo.com/",
        }

        self.min_bars = MIN_BARS
        self.lookback_4h = LOOKBACK_4H
        self.min_volume_ratio = MIN_VOLUME_RATIO
        self.strong_volume_ratio = STRONG_VOLUME_RATIO
        self.min_price_change_pct = MIN_PRICE_CHANGE_PCT
        self.min_3bar_change_pct = MIN_3BAR_CHANGE_PCT
        self.min_range_expansion = MIN_RANGE_EXPANSION

    def fetch_1hour_history(self, ticker: str) -> Optional[pd.DataFrame]:
        """
        Fetch 1H OHLCV data from Yahoo Finance.

        The old scanner used Yahoo's chart endpoint with interval=1h and
        range=1mo. That acquisition logic is retained here.
        """
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{ticker}.NS?interval={YAHOO_INTERVAL}&range={YAHOO_RANGE}"
        )

        try:
            response = requests.get(
                url, headers=self.headers, timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()

            payload = response.json()
            result = (payload.get("chart") or {}).get("result")

            if not result:
                return None

            result = result[0]
            timestamps = result.get("timestamp", [])
            quote = (result.get("indicators") or {}).get("quote", [{}])[0]

            if not timestamps:
                return None

            df = pd.DataFrame(
                {
                    "Datetime": pd.to_datetime(
                        timestamps, unit="s", utc=True
                    ).tz_convert("Asia/Kolkata").tz_localize(None),
                    "Open": quote.get("open", []),
                    "High": quote.get("high", []),
                    "Low": quote.get("low", []),
                    "Close": quote.get("close", []),
                    "Volume": quote.get("volume", []),
                }
            )

            if df.empty:
                return None

            numeric_cols = ["Open", "High", "Low", "Close", "Volume"]
            for col in numeric_cols:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            df = df.dropna(subset=["Datetime", "High", "Low", "Close"]).copy()

            # NSE cash-market trading window.
            df = df[
                (df["Datetime"].dt.dayofweek < 5)
                & (df["Datetime"].dt.time >= pd.Timestamp("09:15").time())
                & (df["Datetime"].dt.time <= pd.Timestamp("15:30").time())
            ].copy()

            return df.sort_values("Datetime").reset_index(drop=True)

        except Exception as exc:
            print(f"[WARN] {ticker}: 1H download failed -> {exc}")
            return None

    def build_session_4h(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        """
        Build the latest scanner's session-anchored blocks:
          Block 0: 09:15-13:15
          Block 1: 13:15-15:30

        This replaces the older script's clock-based resample while retaining
        its 1H data extraction method.
        """
        if df is None or df.empty:
            return None

        x = df.copy()
        x["SessionDate"] = x["Datetime"].dt.date

        minutes = (
            x["Datetime"].dt.hour * 60
            + x["Datetime"].dt.minute
            - (9 * 60 + 15)
        )
        x["Block"] = (minutes // 240).astype(int)
        x = x[(x["Block"] >= 0) & (x["Block"] <= 1)].copy()

        if x.empty:
            return None

        grouped = x.groupby(["SessionDate", "Block"], sort=True)

        bars = grouped.agg(
            Datetime=("Datetime", "first"),
            Open=("Open", "first"),
            High=("High", "max"),
            Low=("Low", "min"),
            Close=("Close", "last"),
            Volume=("Volume", "sum"),
            HourlyBars=("Close", "size"),
        ).reset_index()

        bars = bars[bars["HourlyBars"] >= 2].copy()

        if bars.empty:
            return None

        bars["Date_Str"] = bars["SessionDate"].astype(str)
        return bars.reset_index(drop=True)

    # ========================================================
    # LATEST RVME / VCP LOGIC
    # ========================================================
    def _calculate_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        if len(df) < 2:
            return 0.0

        high_low = df["High"] - df["Low"]
        high_close = np.abs(df["High"] - df["Close"].shift())
        low_close = np.abs(df["Low"] - df["Close"].shift())

        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = ranges.max(axis=1)
        atr = true_range.rolling(window=period, min_periods=1).mean()

        return float(atr.iloc[-1]) if len(atr) else 0.0

    def _get_volume_percentile(
        self, volumes: pd.Series, current_idx: int, lookback: int = 60
    ) -> float:
        start_idx = max(0, current_idx - lookback)
        historical = volumes.iloc[start_idx:current_idx]

        if historical.empty:
            return 50.0

        current_volume = volumes.iloc[current_idx]
        return float((historical < current_volume).mean() * 100)

    def _metrics(self, df: pd.DataFrame, i: int) -> Optional[Dict]:
        if i < self.min_bars:
            return None

        cur = df.iloc[i]
        closes = df["Close"]
        volumes = df["Volume"]
        ranges = df["High"] - df["Low"]

        atr = self._calculate_atr(df.iloc[: i + 1])
        atr_pct = (atr / cur["Close"]) * 100 if cur["Close"] > 0 else 0

        adaptive_min_price = max(self.min_price_change_pct, atr_pct * 0.35)
        adaptive_min_3bar = max(self.min_3bar_change_pct, atr_pct * 0.90)

        sma20 = closes.iloc[i - 19 : i + 1].mean()
        sma50 = closes.iloc[i - 49 : i + 1].mean() if i >= 49 else float("nan")

        lookback = df.iloc[max(0, i - self.lookback_4h) : i]
        wave_high = lookback["High"].max()
        wave_low = lookback["Low"].min()
        avg_volume = lookback["Volume"].mean()
        avg_range = ranges.iloc[max(0, i - self.lookback_4h) : i].mean()

        vol_ratio = cur["Volume"] / avg_volume if avg_volume > 0 else 0
        range_ratio = (
            (cur["High"] - cur["Low"]) / avg_range if avg_range > 0 else 0
        )

        vol_percentile = self._get_volume_percentile(volumes, i)

        r1 = (closes.iloc[i] / closes.iloc[i - 1] - 1) * 100
        r2 = (closes.iloc[i - 1] / closes.iloc[i - 2] - 1) * 100
        r3 = (closes.iloc[i - 2] / closes.iloc[i - 3] - 1) * 100
        three_bar = (closes.iloc[i] / closes.iloc[i - 3] - 1) * 100

        v0, v1, v2 = volumes.iloc[i], volumes.iloc[i - 1], volumes.iloc[i - 2]
        volume_accelerating = v0 > v1 > v2

        bull_accel = r1 > r2 > r3
        bear_accel = r1 < r2 < r3

        buy_momentum = (
            r1 >= adaptive_min_price
            and three_bar >= adaptive_min_3bar
            and bull_accel
            and volume_accelerating
            and vol_ratio >= self.min_volume_ratio
            and range_ratio >= self.min_range_expansion
        )

        sell_momentum = (
            r1 <= -adaptive_min_price
            and three_bar <= -adaptive_min_3bar
            and bear_accel
            and volume_accelerating
            and vol_ratio >= self.min_volume_ratio
            and range_ratio >= self.min_range_expansion
        )

        vcp_buy = (
            closes.iloc[i] > sma20
            and closes.iloc[i] >= wave_high * 0.92
            and vol_ratio < 1.50
        )

        vcp_sell = (
            closes.iloc[i] < sma20
            and closes.iloc[i] <= wave_low * 1.08
        )

        confluence = 0

        if buy_momentum:
            confluence += 40
            if vol_ratio >= self.strong_volume_ratio:
                confluence += 20
            if vol_percentile >= 80:
                confluence += 15
            if closes.iloc[i] > sma20:
                confluence += 10
            if not np.isnan(sma50) and closes.iloc[i] > sma50:
                confluence += 10
            if range_ratio >= 1.5:
                confluence += 5

        if sell_momentum:
            confluence += 40
            if vol_ratio >= self.strong_volume_ratio:
                confluence += 20
            if vol_percentile >= 80:
                confluence += 15
            if closes.iloc[i] < sma20:
                confluence += 10
            if not np.isnan(sma50) and closes.iloc[i] < sma50:
                confluence += 10
            if range_ratio >= 1.5:
                confluence += 5

        return {
            "Close": float(cur["Close"]),
            "High": float(cur["High"]),
            "Low": float(cur["Low"]),
            "Volume": float(cur["Volume"]),
            "SMA20": float(sma20),
            "SMA50": float(sma50),
            "WaveHigh": float(wave_high),
            "WaveLow": float(wave_low),
            "VolumeRatio": float(vol_ratio),
            "RangeRatio": float(range_ratio),
            "VolumePercentile": float(vol_percentile),
            "ATR": float(atr),
            "ATR_Pct": float(atr_pct),
            "R1": float(r1),
            "R2": float(r2),
            "R3": float(r3),
            "ThreeBarChange": float(three_bar),
            "BuyMomentumExpansion": bool(buy_momentum),
            "SellMomentumExpansion": bool(sell_momentum),
            "VCPBuy": bool(vcp_buy),
            "VCPSell": bool(vcp_sell),
            "StrongVolume": bool(vol_ratio >= self.strong_volume_ratio),
            "VolumeAccelerating": bool(volume_accelerating),
            "ConfluenceScore": int(confluence),
        }

    def scan_at_index(
        self, ticker: str, df: pd.DataFrame, i: int
    ) -> Tuple[Optional[Dict], Optional[Dict]]:
        m = self._metrics(df, i)

        if m is None:
            return None, None

        buy_trigger = round(
            max(m["High"], m["WaveHigh"] * 0.995) + 0.65, 2
        )
        sell_trigger = round(
            min(m["Low"], m["WaveLow"] * 1.005) - 0.65, 2
        )

        # RVME momentum + accelerating volume is mandatory for a candidate.
        # VCP is retained as contextual evidence, not as a standalone signal.
        buy = m["BuyMomentumExpansion"]
        sell = m["SellMomentumExpansion"] and not buy

        buy_row = None
        sell_row = None

        if buy:
            stop = round(m["Low"] * 0.985, 2)
            risk = max(buy_trigger - stop, 0.01)

            buy_row = {
                "Ticker": ticker,
                "Signal": (
                    "RVME BUY" if m["BuyMomentumExpansion"] else "VCP BUY"
                ),
                "LTP on 4H Close": round(m["Close"], 2),
                "4H Change %": round(m["R1"], 2),
                "3-Bar Momentum %": round(m["ThreeBarChange"], 2),
                "Volume Expansion Multiple": round(m["VolumeRatio"], 2),
                "Range Expansion Multiple": round(m["RangeRatio"], 2),
                "Volume Percentile": round(m["VolumePercentile"], 1),
                "ATR %": round(m["ATR_Pct"], 2),
                "Confluence Score": m["ConfluenceScore"],
                "Best Entry Price Point": buy_trigger,
                "Defensive Stop Loss": stop,
                "1R Target": round(buy_trigger + risk, 2),
                "Momentum Acceleration": "YES" if m["R1"] > m["R2"] > m["R3"] else "NO",
                "Volume Accelerating": "YES" if m["VolumeAccelerating"] else "NO",
                "Strong Volume": "YES" if m["StrongVolume"] else "NO",
                "High Volume Percentile": "YES" if m["VolumePercentile"] >= 80 else "NO",
                "Strong Range Expansion": "YES" if m["RangeRatio"] >= 1.5 else "NO",
                "VCP Context": "YES" if m["VCPBuy"] else "NO",
            }

        if sell:
            stop = round(m["High"] * 1.015, 2)
            risk = max(stop - sell_trigger, 0.01)

            sell_row = {
                "Ticker": ticker,
                "Signal": (
                    "RVME SELL" if m["SellMomentumExpansion"] else "VCP SELL"
                ),
                "LTP on 4H Close": round(m["Close"], 2),
                "4H Change %": round(m["R1"], 2),
                "3-Bar Momentum %": round(m["ThreeBarChange"], 2),
                "Volume Expansion Multiple": round(m["VolumeRatio"], 2),
                "Range Expansion Multiple": round(m["RangeRatio"], 2),
                "Volume Percentile": round(m["VolumePercentile"], 1),
                "ATR %": round(m["ATR_Pct"], 2),
                "Confluence Score": m["ConfluenceScore"],
                "Best Entry Price Point": sell_trigger,
                "Defensive Stop Loss": stop,
                "1R Target": round(sell_trigger - risk, 2),
                "Momentum Acceleration": "YES" if m["R1"] < m["R2"] < m["R3"] else "NO",
                "Volume Accelerating": "YES" if m["VolumeAccelerating"] else "NO",
                "Strong Volume": "YES" if m["StrongVolume"] else "NO",
                "High Volume Percentile": "YES" if m["VolumePercentile"] >= 80 else "NO",
                "Strong Range Expansion": "YES" if m["RangeRatio"] >= 1.5 else "NO",
                "VCP Context": "YES" if m["VCPSell"] else "NO",
            }

        return buy_row, sell_row

    # ========================================================
    # COMPLETED-BAR FILTER + DYNAMIC 15-BAR STUDY
    # ========================================================
    def keep_completed_bars(self, df_4h: pd.DataFrame, scan_date=None) -> pd.DataFrame:
        """Keep only completed 4H bars available as of the requested scan date."""
        if df_4h is None or df_4h.empty:
            return df_4h

        now = datetime.now()
        today = now.date()
        scan_date = scan_date or today
        keep = []

        for _, row in df_4h.iterrows():
            session_date = row["SessionDate"]
            block = int(row["Block"])

            if session_date < scan_date:
                keep.append(True)
            elif session_date > scan_date:
                keep.append(False)
            elif scan_date < today:
                # A historical requested date is already complete.
                keep.append(True)
            elif block == 0:
                keep.append(now.time() >= dt_time(13, 15))
            elif block == 1:
                keep.append(now.time() >= dt_time(15, 30))
            else:
                keep.append(False)

        return df_4h.loc[keep].reset_index(drop=True)

    def verify_last_15_bars(self, processed):
        """
        Study the last 15 historical 4H bars per ticker. For each bar, test
        hypothetical BUY and SELL setups with identical entry/stop/1R rules.
        This allows the study to compare criteria both when present and absent.
        The newest completed bar is excluded from historical signal origins.
        """
        all_rows = []
        feature_cols = [
            "Momentum Acceleration", "Volume Accelerating", "Strong Volume",
            "High Volume Percentile", "Strong Range Expansion", "VCP Context"
        ]

        for ticker, df_4h in processed:
            if len(df_4h) < self.min_bars + 2:
                continue

            start = max(self.min_bars, len(df_4h) - 16)
            end = len(df_4h) - 1

            for i in range(start, end):
                m = self._metrics(df_4h, i)
                if m is None:
                    continue
                next_bar = df_4h.iloc[i + 1]

                buy_entry = round(max(m["High"], m["WaveHigh"] * 0.995) + 0.65, 2)
                buy_stop = round(m["Low"] * 0.985, 2)
                buy_risk = max(buy_entry - buy_stop, 0.01)
                buy_target = round(buy_entry + buy_risk, 2)

                sell_entry = round(min(m["Low"], m["WaveLow"] * 1.005) - 0.65, 2)
                sell_stop = round(m["High"] * 1.015, 2)
                sell_risk = max(sell_stop - sell_entry, 0.01)
                sell_target = round(sell_entry - sell_risk, 2)

                for side in ["BUY", "SELL"]:
                    if side == "BUY":
                        entry, stop, target = buy_entry, buy_stop, buy_target
                        entry_hit = float(next_bar["High"]) >= entry
                        stop_hit = float(next_bar["Low"]) <= stop
                        target_hit = float(next_bar["High"]) >= target
                        momentum_accel = m["R1"] > 0 and m["R1"] > m["R2"] > m["R3"]
                        vcp_context = m["VCPBuy"]
                        qualified = m["BuyMomentumExpansion"]
                    else:
                        entry, stop, target = sell_entry, sell_stop, sell_target
                        entry_hit = float(next_bar["Low"]) <= entry
                        stop_hit = float(next_bar["High"]) >= stop
                        target_hit = float(next_bar["Low"]) <= target
                        momentum_accel = m["R1"] < 0 and m["R1"] < m["R2"] < m["R3"]
                        vcp_context = m["VCPSell"]
                        qualified = m["SellMomentumExpansion"]

                    if not entry_hit:
                        result = "NO ENTRY"
                    elif stop_hit and target_hit:
                        result = "STOP" if CONSERVATIVE_AMBIGUOUS else "AMBIGUOUS"
                    elif stop_hit:
                        result = "STOP"
                    elif target_hit:
                        result = "TARGET"
                    else:
                        result = "OPEN"

                    record = {
                        "Ticker": ticker,
                        "Signal Bar": df_4h.iloc[i]["Datetime"],
                        "Next 4H Bar": next_bar["Datetime"],
                        "Side": side,
                        "Historical Setup Type": "RVME Qualified" if qualified else "Diagnostic Only",
                        "4H Momentum %": round(m["R1"], 2),
                        "3-Bar Momentum %": round(m["ThreeBarChange"], 2),
                        "Volume Multiple": round(m["VolumeRatio"], 2),
                        "Range Multiple": round(m["RangeRatio"], 2),
                        "Volume Percentile": round(m["VolumePercentile"], 1),
                        "Confluence Score": m["ConfluenceScore"],
                        "Entry": entry,
                        "Stop": stop,
                        "1R Target": target,
                        "Risk %": round(abs(entry - stop) / max(abs(entry), 0.01) * 100.0, 3),
                        "MFE %": round(((float(next_bar["High"]) - entry) / max(abs(entry), 0.01) * 100.0) if side == "BUY" else ((entry - float(next_bar["Low"])) / max(abs(entry), 0.01) * 100.0), 3),
                        "MAE %": round(((entry - float(next_bar["Low"])) / max(abs(entry), 0.01) * 100.0) if side == "BUY" else ((float(next_bar["High"]) - entry) / max(abs(entry), 0.01) * 100.0), 3),
                        "MFE R": round((((float(next_bar["High"]) - entry) / max(abs(entry - stop), 0.01)) if side == "BUY" else ((entry - float(next_bar["Low"])) / max(abs(entry - stop), 0.01))), 3),
                        "MAE R": round((((entry - float(next_bar["Low"])) / max(abs(entry - stop), 0.01)) if side == "BUY" else ((float(next_bar["High"]) - entry) / max(abs(entry - stop), 0.01))), 3),
                        "Historical Risk %": round(abs(entry - stop) / max(abs(entry), 0.01), 6),
                        "Result": result,
                        "Momentum Acceleration": "YES" if momentum_accel else "NO",
                        "Volume Accelerating": "YES" if m["VolumeAccelerating"] else "NO",
                        "Strong Volume": "YES" if m["StrongVolume"] else "NO",
                        "High Volume Percentile": "YES" if m["VolumePercentile"] >= 80 else "NO",
                        "Strong Range Expansion": "YES" if m["RangeRatio"] >= 1.5 else "NO",
                        "VCP Context": "YES" if vcp_context else "NO",
                    }
                    all_rows.append(record)

        return pd.DataFrame(all_rows)

    def learn_dynamic_criteria(self, verification_df: pd.DataFrame):
        """
        Learn per-side criterion effectiveness from resolved TARGET/STOP results.
        A small prior shrinks low-sample win rates toward the side baseline.
        Returns a readable criterion report and per-side model.
        """
        features = [
            "Momentum Acceleration", "Volume Accelerating", "Strong Volume",
            "High Volume Percentile", "Strong Range Expansion", "VCP Context"
        ]
        report = []
        model = {}

        if verification_df is None or verification_df.empty:
            return pd.DataFrame(columns=[
                "Side", "Criterion", "Observations", "Targets", "Stops",
                "Raw Success %", "Smoothed Effectiveness %", "Side Baseline %", "Uplift pp"
            ]), model

        resolved = verification_df[verification_df["Result"].isin(["TARGET", "STOP"])].copy()
        for side in ["BUY", "SELL"]:
            side_resolved = resolved[resolved["Side"] == side]
            baseline = (
                float((side_resolved["Result"] == "TARGET").mean())
                if not side_resolved.empty else 0.5
            )
            model[side] = {"baseline": baseline, "resolved_n": len(side_resolved), "criteria": {}}

            for feature in features:
                if side_resolved.empty:
                    subset = side_resolved
                else:
                    subset = side_resolved[
                        side_resolved[feature].astype(str).str.upper().eq("YES")
                    ]
                n = len(subset)
                wins = int((subset["Result"] == "TARGET").sum()) if n else 0
                stops = int((subset["Result"] == "STOP").sum()) if n else 0
                raw_rate = wins / n if n else np.nan
                prior_n = 4.0
                smoothed = (wins + baseline * prior_n) / (n + prior_n) if n + prior_n else baseline
                uplift = smoothed - baseline
                model[side]["criteria"][feature] = {
                    "n": n, "wins": wins, "stops": stops,
                    "smoothed": smoothed, "uplift": uplift
                }
                report.append({
                    "Side": side,
                    "Criterion": feature,
                    "Observations": n,
                    "Targets": wins,
                    "Stops": stops,
                    "Raw Success %": round(raw_rate * 100, 1) if n else np.nan,
                    "Smoothed Effectiveness %": round(smoothed * 100, 1),
                    "Side Baseline %": round(baseline * 100, 1),
                    "Uplift pp": round(uplift * 100, 1),
                })

        return pd.DataFrame(report), model

    def calibrate_trade_levels(self, candidates: pd.DataFrame, side: str, verification_df: pd.DataFrame):
        """
        Calibrate current Entry/SL/Targets from recent historical outcomes.

        Historical TARGET trades are used to estimate:
          - successful entry buffer
          - successful risk distance (% of entry)
          - favorable excursion in R

        The current RVME structural trigger remains the anchor. Historical data
        adjusts the trade geometry rather than replacing the signal itself.
        Sample-size gates prevent a handful of observations from dominating.
        """
        if candidates is None or candidates.empty:
            return candidates

        features = [
            "Momentum Acceleration", "Volume Accelerating", "Strong Volume",
            "High Volume Percentile", "Strong Range Expansion", "VCP Context"
        ]
        out = candidates.copy()
        hist = verification_df.copy() if verification_df is not None else pd.DataFrame()

        if hist.empty:
            out["Entry"] = out["Best Entry Price Point"]
            out["Stop Loss"] = out["Defensive Stop Loss"]
            out["Target 1"] = out["1R Target"]
            out["Target 2"] = np.nan
            out["Risk / Share"] = (out["Entry"] - out["Stop Loss"]).abs().round(2)
            out["Target 1 R"] = 1.0
            out["Target 2 R"] = np.nan
            out["Historical Success %"] = np.nan
            out["Historical Success Sample"] = 0
            out["Historical Median MFE R"] = np.nan
            out["Level Calibration"] = "Structural/ATR fallback"
            return out

        side_hist = hist[hist["Side"] == side].copy()
        successes = side_hist[side_hist["Result"] == "TARGET"].copy()

        entries, stops, t1s, t2s = [], [], [], []
        risks, r1s, r2s = [], [], []
        success_rates, samples, mfe_rs, methods = [], [], [], []

        for _, row in out.iterrows():
            structural_entry = float(row["Best Entry Price Point"])
            structural_stop = float(row["Defensive Stop Loss"])
            structural_risk = max(abs(structural_entry - structural_stop), 0.01)
            structural_risk_pct = structural_risk / max(abs(structural_entry), 0.01)

            # Similarity is based on the six diagnostic conditions that define
            # the setup. Prefer historical successes with at least 4/6 matches.
            pool = successes.copy()
            if not pool.empty:
                match_scores = []
                for _, h in pool.iterrows():
                    score = 0
                    for f in features:
                        if str(row.get(f, "NO")).upper() == str(h.get(f, "NO")).upper():
                            score += 1
                    match_scores.append(score)
                pool = pool.assign(_match_score=match_scores)
                similar = pool[pool["_match_score"] >= 4]
                if len(similar) >= 3:
                    pool = similar

            n_success = len(pool)
            side_resolved = side_hist[side_hist["Result"].isin(["TARGET", "STOP"])]
            success_rate = (
                len(successes) / len(side_resolved) * 100.0
                if len(side_resolved) else np.nan
            )

            if n_success >= 5:
                hist_weight = 0.75
                method = "Dynamic Historical (>=5 successes)"
            elif n_success >= 3:
                hist_weight = 0.50
                method = "Blended Historical + Structural (3-4 successes)"
            else:
                hist_weight = 0.0
                method = "Structural/ATR fallback (<3 similar successes)"

            if hist_weight > 0:
                hist_risk_pct = float(pool["Historical Risk %"].median())
                hist_mfe_r = float(pool["MFE R"].median())
                # A second target uses a robust upper-success excursion, not an
                # arbitrary fixed multiple. Cap it to avoid one outlier dominating.
                hist_t2_r = float(pool["MFE R"].quantile(0.75))
                # Keep Target 2 meaningfully beyond Target 1 while still using
                # the observed upper-quartile favorable excursion as its anchor.
                hist_t2_r = float(np.clip(hist_t2_r, max(hist_mfe_r, 1.25), 5.0))

                calibrated_risk_pct = (
                    hist_weight * hist_risk_pct
                    + (1.0 - hist_weight) * structural_risk_pct
                )
                # Never allow historical calibration to make the risk absurdly
                # small/large relative to the current structural stop.
                calibrated_risk_pct = float(np.clip(
                    calibrated_risk_pct,
                    structural_risk_pct * 0.65,
                    structural_risk_pct * 1.50,
                ))
                risk = max(structural_entry * calibrated_risk_pct, 0.01)
                r1 = float(np.clip(hist_mfe_r, 1.0, 3.0))
                r2 = float(np.clip(max(hist_t2_r, r1 + 0.50), r1 + 0.50, 5.0))
            else:
                risk = structural_risk
                r1 = 1.0
                r2 = 2.0
                hist_mfe_r = np.nan

            if side == "BUY":
                entry = structural_entry
                stop = entry - risk
                target1 = entry + risk * r1
                target2 = entry + risk * r2
            else:
                entry = structural_entry
                stop = entry + risk
                target1 = entry - risk * r1
                target2 = entry - risk * r2

            entries.append(round(entry, 2))
            stops.append(round(stop, 2))
            t1s.append(round(target1, 2))
            t2s.append(round(target2, 2))
            risks.append(round(risk, 2))
            r1s.append(round(r1, 2))
            r2s.append(round(r2, 2))
            success_rates.append(round(success_rate, 1) if not np.isnan(success_rate) else np.nan)
            samples.append(n_success)
            mfe_rs.append(round(hist_mfe_r, 2) if not np.isnan(hist_mfe_r) else np.nan)
            methods.append(method)

        out["Entry"] = entries
        out["Stop Loss"] = stops
        out["Target 1"] = t1s
        out["Target 2"] = t2s
        out["Risk / Share"] = risks
        out["Target 1 R"] = r1s
        out["Target 2 R"] = r2s
        out["Historical Success %"] = success_rates
        out["Historical Success Sample"] = samples
        out["Historical Median MFE R"] = mfe_rs
        out["Level Calibration"] = methods
        return out

    def apply_dynamic_ranking(self, candidates: pd.DataFrame, side: str, model):
        """Add a bounded evidence-based score adjustment to current candidates."""
        if candidates is None or candidates.empty:
            return candidates
        learned = model.get(side, {"criteria": {}}).get("criteria", {})
        bonuses = []
        for _, row in candidates.iterrows():
            total = 0.0
            for feature, stats in learned.items():
                if str(row.get(feature, "NO")).upper() == "YES" and stats["n"] >= 3:
                    # Conservative weighting: only modestly adjust a fixed-rule score.
                    contribution = float(np.clip(stats["uplift"] * 25.0, -3.0, 3.0))
                    total += contribution
            bonuses.append(float(np.clip(total, -10.0, 10.0)))
        out = candidates.copy()
        out["Dynamic Study Bonus"] = [round(x, 2) for x in bonuses]
        out["Adaptive Rank Score"] = (
            pd.to_numeric(out["Confluence Score"], errors="coerce").fillna(0)
            + out["Dynamic Study Bonus"]
        ).round(2)
        out["Historical Sample Size"] = int(model.get(side, {}).get("resolved_n", 0))
        return out

    # ========================================================
    # LIVE SCAN: STUDY FIRST, THEN RANK FRESH CANDIDATES
    # ========================================================
    def scan_latest(self, scan_date=None):
        print("=" * 78)
        print(" ADAPTIVE NSE F&O 4H VCP + RVME MOMENTUM/VOLUME SCANNER ")
        print("=" * 78)
        scan_date = scan_date or datetime.now().date()
        print(f"F&O universe: {len(self.watchlist)} symbols")
        print(f"Requested scan date: {scan_date}")
        print("Data source: Yahoo Finance 1H -> session-anchored 4H")
        print("Workflow: historical 15-bar study -> criteria effectiveness -> fresh ranking")
        print()

        processed, failed = [], []
        for n, ticker in enumerate(self.watchlist, 1):
            print(f"\rFetching {n}/{len(self.watchlist)}: {ticker:<16}", end="", flush=True)
            df_1h = self.fetch_1hour_history(ticker)
            if df_1h is None or len(df_1h) < 50:
                failed.append(ticker)
                continue
            df_4h = self.build_session_4h(df_1h)
            if df_4h is None or len(df_4h) < self.min_bars + 2:
                failed.append(ticker)
                continue
            df_4h = self.keep_completed_bars(df_4h, scan_date=scan_date)
            if df_4h is None or len(df_4h) < self.min_bars + 2:
                failed.append(ticker)
                continue
            processed.append((ticker, df_4h))
            time.sleep(REQUEST_DELAY)

        print()
        print(f"[OK] Symbols with usable completed 4H data: {len(processed)}")
        print(f"[INFO] Symbols skipped: {len(failed)}")
        if not processed:
            raise RuntimeError(
                "No usable completed 4H data was obtained. Check internet access, "
                "Yahoo availability, and NSE/Yahoo symbol mapping."
            )

        # Historical study happens BEFORE today's candidate ranking.
        verification_df = self.verify_last_15_bars(processed)
        criteria_df, dynamic_model = self.learn_dynamic_criteria(verification_df)

        # Align candidates to the latest session date available across the usable universe.
        latest_dates = [df_4h.iloc[-1]["SessionDate"] for _, df_4h in processed]
        common_date = min(latest_dates)
        aligned = []
        for ticker, df_4h in processed:
            aligned_df = df_4h[df_4h["SessionDate"] <= common_date].reset_index(drop=True)
            if len(aligned_df) >= self.min_bars + 2:
                aligned.append((ticker, aligned_df))
        processed = aligned

        buy_candidates, sell_candidates = [], []
        for ticker, df_4h in processed:
            i = len(df_4h) - 1
            b, s = self.scan_at_index(ticker, df_4h, i)
            if b:
                buy_candidates.append(b)
            if s:
                sell_candidates.append(s)

        buy_df = self.apply_dynamic_ranking(pd.DataFrame(buy_candidates), "BUY", dynamic_model)
        sell_df = self.apply_dynamic_ranking(pd.DataFrame(sell_candidates), "SELL", dynamic_model)

        # Historical outcomes now calibrate the actual trade geometry. Ranking
        # and price-level calibration are deliberately separate so a score
        # improvement cannot silently change the signal itself.
        buy_df = self.calibrate_trade_levels(buy_df, "BUY", verification_df)
        sell_df = self.calibrate_trade_levels(sell_df, "SELL", verification_df)

        if not buy_df.empty:
            buy_df = buy_df.sort_values(
                ["Adaptive Rank Score", "Confluence Score", "Volume Expansion Multiple"],
                ascending=False
            ).head(10)
        if not sell_df.empty:
            sell_df = sell_df.sort_values(
                ["Adaptive Rank Score", "Confluence Score", "Volume Expansion Multiple"],
                ascending=False
            ).head(10)

        actual_date = str(common_date) if latest_dates else datetime.now().date().isoformat()
        print(f"[OK] Latest completed 4H data date represented: {actual_date}")
        print(f"[INFO] Historical verification rows: {len(verification_df)}")
        print(f"[INFO] Resolved TARGET/STOP observations: {int(verification_df['Result'].isin(['TARGET','STOP']).sum()) if not verification_df.empty else 0}")
        return buy_df, sell_df, processed, failed, actual_date, verification_df, criteria_df


# ============================================================
# EXCEL OUTPUT
# ============================================================
def _reorder_trade_columns(df):
    """Put the calibrated live-trade levels prominently at the front of Excel."""
    if df is None or df.empty:
        return df
    preferred = [
        "Ticker", "Signal", "LTP on 4H Close", "Entry", "Stop Loss",
        "Target 1", "Target 2", "Risk / Share", "Target 1 R", "Target 2 R",
        "Historical Success %", "Historical Success Sample",
        "Historical Median MFE R", "Level Calibration", "Adaptive Rank Score",
        "Dynamic Study Bonus", "Confluence Score",
    ]
    cols = [c for c in preferred if c in df.columns]
    cols += [c for c in df.columns if c not in cols]
    return df[cols]


def write_excel(
    output_path,
    buy_df,
    sell_df,
    verification_df,
    criteria_df,
    processed,
    failed,
    actual_date,
):
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        if buy_df.empty:
            pd.DataFrame(
                [{"Alert": "No stocks met the current RVME/VCP BUY filters"}]
            ).to_excel(writer, sheet_name="TOP 10 BUY", index=False)
        else:
            _reorder_trade_columns(buy_df).to_excel(writer, sheet_name="TOP 10 BUY", index=False)

        if sell_df.empty:
            pd.DataFrame(
                [{"Alert": "No stocks met the current RVME/VCP SELL filters"}]
            ).to_excel(writer, sheet_name="TOP 10 SELL", index=False)
        else:
            _reorder_trade_columns(sell_df).to_excel(writer, sheet_name="TOP 10 SELL", index=False)

        if verification_df.empty:
            pd.DataFrame(
                [{"Alert": "No RVME signals generated in verification window"}]
            ).to_excel(writer, sheet_name="15-Bar Verification", index=False)
        else:
            verification_df.to_excel(
                writer, sheet_name="15-Bar Verification", index=False
            )

        if criteria_df is None or criteria_df.empty:
            pd.DataFrame([{"Alert": "Insufficient resolved historical outcomes to estimate criterion effectiveness"}]).to_excel(
                writer, sheet_name="Criteria Effectiveness", index=False
            )
        else:
            criteria_df.to_excel(writer, sheet_name="Criteria Effectiveness", index=False)

        universe_df = pd.DataFrame(
            {
                "Ticker": [t for t, _ in processed],
                "Status": ["Usable 4H data"] * len(processed),
            }
        )

        failed_df = pd.DataFrame(
            {
                "Ticker": failed,
                "Status": ["Skipped / insufficient data"] * len(failed),
            }
        )

        pd.concat([universe_df, failed_df], ignore_index=True).to_excel(
            writer, sheet_name="Data Status", index=False
        )

        summary = pd.DataFrame(
            [
                ["Scan Date", actual_date],
                ["Usable Symbols", len(processed)],
                ["Skipped Symbols", len(failed)],
                ["BUY Candidates", len(buy_df)],
                ["SELL Candidates", len(sell_df)],
                ["Verification Rows", len(verification_df)],
            ],
            columns=["Metric", "Value"],
        )

        summary.to_excel(writer, sheet_name="Scan Summary", index=False)

        # Basic worksheet usability.
        for ws in writer.book.worksheets:
            ws.freeze_panes = "A2"
            for column_cells in ws.columns:
                max_length = 0
                for cell in column_cells:
                    value = "" if cell.value is None else str(cell.value)
                    max_length = max(max_length, len(value))
                ws.column_dimensions[column_cells[0].column_letter].width = min(
                    max(max_length + 2, 12), 45
                )


# ============================================================
# MAIN
# ============================================================
def main():
    # The date prompt is intentionally the first user interaction when the
    # script starts. If there is no response for 15 seconds, today's date is used.
    scan_date = prompt_scan_date(DATE_PROMPT_TIMEOUT_SECONDS)

    parser = argparse.ArgumentParser(
        description="Live NSE F&O 4H VCP + RVME Momentum/Volume Scanner"
    )
    parser.add_argument(
        "--verify15",
        action="store_true",
        help="Compatibility flag; the 15-bar adaptive study always runs.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional Excel output filename. If omitted, date and run time are included automatically.",
    )
    args = parser.parse_args()

    run_timestamp = datetime.now()
    scanner = NSEFno4HourVCPMomentumScanner()

    (
        buy_df, sell_df, processed, failed, actual_date,
        verification_df, criteria_df
    ) = scanner.scan_latest(scan_date=scan_date)

    # Include both the represented market date and the exact run time so
    # multiple intraday scans never overwrite each other.
    timestamp_label = run_timestamp.strftime("%Y-%m-%d_%H-%M-%S")
    output_name = args.output or f"NSE_RVME_VCP_4H_{actual_date}_RUN_{timestamp_label}.xlsx"
    output_path = os.path.abspath(output_name)

    write_excel(
        output_path,
        buy_df,
        sell_df,
        verification_df,
        criteria_df,
        processed,
        failed,
        actual_date,
    )

    print()
    print("=" * 78)
    print(" SCAN COMPLETE")
    print("=" * 78)

    print("\nTOP 10 BUY:")
    print(buy_df.to_string(index=False) if not buy_df.empty else "None")

    print("\nTOP 10 SELL:")
    print(sell_df.to_string(index=False) if not sell_df.empty else "None")

    print("\n15-BAR ADAPTIVE VERIFICATION:")
    if verification_df.empty:
        print("No RVME verification signals.")
    else:
        print(verification_df.groupby(["Side", "Result"]).size())
    print("\nCRITERIA EFFECTIVENESS:")
    print(criteria_df.to_string(index=False) if not criteria_df.empty else "Insufficient resolved outcomes")

    print(f"\n[OK] Excel generated: {output_path}")
    print(f"[OK] Requested scan date: {scan_date}")
    print(f"[OK] Run timestamp: {run_timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 78)


if __name__ == "__main__":
    main()
