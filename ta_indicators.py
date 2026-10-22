"""Causal indicator snapshot from the open-source `ta` library (MIT license)."""
import math


def snapshot(bars):
    if len(bars) < 60:
        raise ValueError('حداقل ۶۰ کندل بسته‌شده برای اندیکاتورهای روند لازم است.')
    # Import on demand so demo and older strategies remain available without ta.
    import pandas as pd
    from ta.momentum import RSIIndicator
    from ta.trend import ADXIndicator, EMAIndicator
    from ta.volatility import AverageTrueRange

    observed = bars[-150:]
    close = pd.Series([bar['c'] for bar in observed], dtype='float64')
    high = pd.Series([bar['h'] for bar in observed], dtype='float64')
    low = pd.Series([bar['l'] for bar in observed], dtype='float64')
    adx = ADXIndicator(high=high, low=low, close=close, window=14, fillna=False)
    result = dict(ema_fast=float(EMAIndicator(close, window=12).ema_indicator().iloc[-1]),
                  ema_slow=float(EMAIndicator(close, window=26).ema_indicator().iloc[-1]),
                  rsi=float(RSIIndicator(close, window=14).rsi().iloc[-1]),
                  adx=float(adx.adx().iloc[-1]),
                  plus_di=float(adx.adx_pos().iloc[-1]),
                  minus_di=float(adx.adx_neg().iloc[-1]),
                  atr_pct=float(AverageTrueRange(high, low, close, window=14).average_true_range().iloc[-1]
                                / close.iloc[-1] * 100))
    if not all(math.isfinite(value) for value in result.values()):
        raise ValueError('اندیکاتور ناپایدار است؛ سیگنال ورود صادر نشد.')
    return result
