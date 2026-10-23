"""Deterministic, long-only trading research engine. No exchange orders here."""
import math
import random
import statistics
from dataclasses import dataclass, asdict
from challenge_rules import assert_market_allowed, assert_capital, DURATION_SECONDS, RULE_VERSION

DEFAULTS = dict(symbol='BTCTMN', source='demo', model='logistic', strategy='adaptive',
                capital=5_000_000, allocation=20, stop_loss=1.5, take_profit=3,
                max_drawdown=5, fee=0.2, slippage=0.05, confidence=56, seed=42)
MODELS = ('logistic', 'knn', 'ensemble', 'hybrid_llm')
STRATEGIES = ('adaptive', 'momentum', 'reversion', 'breakout', 'trend_hold', 'ta_trend', 'ai_regime')
SYMBOLS = {'BTCTMN': 6_200_000_000, 'ETHTMN': 230_000_000, 'USDTTMN': 105_000,
           'PEPETMN': 1, 'SOLTMN': 20_000_000, 'XRPTMN': 250_000}
BAR_SECONDS = 900


def validate(config):
    result = {**DEFAULTS, **config}
    if set(config) - set(DEFAULTS):
        raise ValueError('تنظیم ناشناخته یا تلاش برای تغییر قواعد ثابت طرح.')
    result['symbol'] = assert_market_allowed(result['symbol'])
    assert_capital(result['capital'])
    if result['symbol'] not in SYMBOLS or result['model'] not in MODELS or result['strategy'] not in STRATEGIES:
        raise ValueError('مدل، الگوریتم یا بازار نامعتبر است.')
    if result['source'] not in ('demo', 'wallex', 'wallex_mcp'):
        raise ValueError('منبع داده نامعتبر است.')
    limits = dict(capital=(100_000, 1e12), allocation=(1, 50), stop_loss=(0.1, 15),
                  take_profit=(0.2, 30), max_drawdown=(0.1, 30), fee=(0, 2),
                  slippage=(0, 2), confidence=(50, 95), seed=(0, 1000000))
    for key, (low, high) in limits.items():
        try:
            value = float(result[key])
        except (ValueError, TypeError):
            raise ValueError('مقدار نامعتبر: ' + key)
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError('مقدار خارج از محدوده: ' + key)
        result[key] = int(value) if key == 'seed' else value
    return {key: result[key] for key in DEFAULTS}


def demo_candles(count=480, seed=42, symbol='BTCTMN'):
    symbol = assert_market_allowed(symbol)
    rng = random.Random(int(seed))
    price = SYMBOLS[symbol]
    bars = []
    for i in range(count):
        previous = price
        change = .0028 * math.sin(i / 17) + .0015 * math.cos(i / 7) + rng.gauss(0, .0026)
        price *= 1 + change
        bars.append(dict(t=1_780_000_000 + i * BAR_SECONDS, o=previous, c=price,
                         h=max(previous, price) * (1 + rng.random() * .002),
                         l=min(previous, price) * (1 - rng.random() * .002), v=100 + rng.random() * 500))
    return bars


def indicators(bars):
    prices = [b['c'] for b in bars]
    fast = statistics.mean(prices[-8:])
    slow = statistics.mean(prices[-21:])
    differences = [b - a for a, b in zip(prices[-15:-1], prices[-14:])]
    up = sum(max(0, d) for d in differences)
    down = sum(max(0, -d) for d in differences)
    rsi = 100 * up / (up + down) if up + down else 50
    std = statistics.pstdev(prices[-20:]) or prices[-1] * .0001
    z = (prices[-1] - statistics.mean(prices[-20:])) / std
    volatility = statistics.pstdev([b / a - 1 for a, b in zip(prices[-21:-1], prices[-20:])])
    features = [(fast / slow - 1) * 100, (rsi - 50) / 50,
                max(-3, min(3, z)) / 3, (prices[-1] / prices[-5] - 1) * 100]
    return dict(fast=fast, slow=slow, rsi=rsi, z=z, volatility=volatility, features=features)


def sigmoid(x):
    return 1 / (1 + math.exp(-max(-30, min(30, x))))


def predict(bars, model):
    # Only labels whose next close is already observed enter the training set.
    samples = []
    start = max(24, len(bars) - 100)
    for end in range(start, len(bars)):
        features = indicators(bars[:end])['features']
        label = int(bars[end]['c'] > bars[end - 1]['c'])
        samples.append((features, label))
    x = indicators(bars)['features']
    weights = [0.0] * 5
    for _ in range(32):
        for features, label in samples:
            extended = [1] + features
            error = label - sigmoid(sum(w * f for w, f in zip(weights, extended)))
            weights = [w + .025 * (error * f - .008 * w) for w, f in zip(weights, extended)]
    logistic = sigmoid(sum(w * f for w, f in zip(weights, [1] + x)))
    neighbors = sorted(samples, key=lambda s: sum((a - b) ** 2 for a, b in zip(s[0], x)))[:9]
    knn = (sum(label for _, label in neighbors) + 1) / (len(neighbors) + 2)
    probability = logistic if model == 'logistic' else knn if model == 'knn' else (logistic + knn) / 2
    return probability, len(samples)


def decide(bars, config, entry_allowed=True, in_cooldown=False, holding_position=False):
    if len(bars) < 45:
        return dict(action='HOLD', confidence=50, reason='در حال جمع‌آوری داده برای آموزش مدل', indicators={}, samples=0)
    if config['strategy'] in ('ta_trend', 'ai_regime') and len(bars) < 60:
        return dict(action='HOLD', confidence=50, reason='در حال جمع‌آوری ۶۰ کندل برای ADX و ATR', indicators={}, samples=0)
    data = indicators(bars)
    probability, samples = predict(bars, config['model'])
    strategy = config['strategy']
    if strategy == 'adaptive':
        strategy = 'momentum' if abs(data['fast'] / data['slow'] - 1) > .003 else 'reversion'
    if strategy == 'momentum':
        buy = data['fast'] > data['slow'] and data['rsi'] < 82
        sell = data['fast'] < data['slow']
        reason = 'روند میانگین‌های ۸ و ۲۱ کندل'
    elif strategy == 'ta_trend':
        from ta_indicators import snapshot
        technical = snapshot(bars)
        round_trip_cost = 2 * (config['fee'] + config['slippage'])
        buy = (technical['ema_fast'] > technical['ema_slow']
               and bars[-1]['c'] > technical['ema_fast']
               and technical['adx'] >= 22
               and technical['plus_di'] > technical['minus_di']
               and 45 <= technical['rsi'] <= 78
               and 4 * technical['atr_pct'] > 1.2 * round_trip_cost)
        sell = technical['ema_fast'] < technical['ema_slow'] or bars[-1]['c'] < technical['ema_slow']
        reason = 'روند EMA، ADX و ATR از کتابخانه متن‌باز ta'
    elif strategy == 'ai_regime':
        from ta_indicators import snapshot
        technical = snapshot(bars)
        round_trip_cost = 2 * (config['fee'] + config['slippage'])
        trend_aligned = (technical['ema_fast'] > technical['ema_slow']
                         and technical['plus_di'] > technical['minus_di']
                         and technical['adx'] >= 22)
        hurdle_cleared = (4 * technical['atr_pct'] > 1.5 * round_trip_cost)
        dist_from_fast = (bars[-1]['c'] / technical['ema_fast'] - 1) * 100
        pullback_ok = (45 <= technical['rsi'] <= 66
                       and bars[-1]['c'] >= technical['ema_fast'] * 0.995
                       and dist_from_fast <= 1.5)
        buy = trend_aligned and hurdle_cleared and pullback_ok
        sell = technical['ema_fast'] < technical['ema_slow'] or bars[-1]['c'] < technical['ema_slow']
        reason = 'تشخیص رژیم روند صعودی، سد کارمزد و اصلاح قیمت'
    elif strategy == 'breakout':
        # A completed close above the previous 20 highs; no same-bar/future high.
        buy = bars[-1]['c'] > max(bar['h'] for bar in bars[-21:-1]) and data['rsi'] < 85
        sell = bars[-1]['c'] < min(bar['l'] for bar in bars[-11:-1])
        reason = 'شکست سقف ۲۰ کندل بسته‌شده'
    elif strategy == 'trend_hold':
        trend_pct = (data['fast'] / data['slow'] - 1) * 100
        recent_pct = (bars[-1]['c'] / bars[-5]['c'] - 1) * 100
        cost_pct = 2 * (config['fee'] + config['slippage'])
        buy = (trend_pct > cost_pct and recent_pct > cost_pct / 2
               and bars[-1]['c'] > data['fast'] and 50 < data['rsi'] < 80)
        sell = data['fast'] < data['slow']
        reason = 'روند قوی‌تر از هزینه رفت‌وبرگشت'
    else:
        buy = data['z'] < -.65 and data['rsi'] < 48
        sell = data['z'] > .65
        reason = 'بازگشت قیمت به میانگین ۲۰ کندل'
    threshold = config['confidence'] / 100
    action = 'BUY' if buy and probability >= threshold else 'SELL' if sell and probability <= 1 - threshold else 'HOLD'
    if not entry_allowed and action == 'BUY':
        action = 'HOLD'
    confidence = probability if action == 'BUY' else 1 - probability if action == 'SELL' else max(probability, 1 - probability)
    detail = f'{reason}؛ احتمال رشد مدل {probability:.1%}؛ RSI={data["rsi"]:.1f}'
    if action == 'HOLD':
        if holding_position:
            detail += '؛ موقعیت خرید از قبل باز است'
        elif in_cooldown and buy:
            detail += '؛ ورود مجدد موقتاً پس از خروج قبلی در حال مدیریت نوسان است'
        else:
            detail += '؛ توافق کافی بین مدل و استراتژی وجود ندارد'
    decision = dict(action=action, confidence=round(confidence * 100, 1), reason=detail,
                indicators={k: v for k, v in data.items() if k != 'features'}, samples=samples,
                probability=probability, regime=strategy)
    if strategy in ('ta_trend', 'ai_regime'):
        decision['indicators'].update(technical)
    if config['model'] == 'hybrid_llm' and action == 'BUY' and entry_allowed:
        from llm_gateway import review
        # Numeric market features only, with relative prices/time; no private account data.
        anchor = bars[-1]['c']
        features = dict(regime=strategy, probability=probability, rsi=data['rsi'], z=data['z'],
                        trend_pct=(data['fast'] / data['slow'] - 1) * 100,
                        volatility_pct=data['volatility'] * 100,
                        round_trip_cost_pct=2 * (config['fee'] + config['slippage']),
                        past_bars=[{k: round(bar[k] / anchor, 8) for k in ('o', 'h', 'l', 'c')}
                                   for bar in bars[-24:]])
        if strategy in ('ta_trend', 'ai_regime'):
            features['technical'] = technical
            features['adx'] = technical['adx']
            features['atr_pct'] = technical['atr_pct']
            features['dist_from_fast_pct'] = round((bars[-1]['c'] / technical['ema_fast'] - 1) * 100, 2)
            features['hurdle_ratio'] = round(4 * technical['atr_pct'] / (2 * (config['fee'] + config['slippage'])), 2)
        vote = review(features)
        decision['llm'] = vote
        approved = vote['approve'] and vote['confidence'] >= config['confidence']
        if not approved:
            decision['action'] = 'HOLD'
        ai_reason = {'trend':'روند قابل قبول', 'weak_edge':'مزیت ناکافی',
                     'uncertain':'عدم قطعیت', 'costs':'هزینه معاملات'}[vote['reason_code']]
        decision['reason'] += ('؛ رأی AI: ' + ('ورود تأیید شد' if approved else 'ورود رد شد')
                               + '؛ ' + ai_reason + '؛ وضعیت ' + vote['status'])
    return decision


@dataclass
class Account:
    cash: float = 5_000_000
    quantity: float = 0
    cost: float = 0
    entry: float = 0
    realized: float = 0
    fees: float = 0
    peak: float = 5_000_000
    drawdown: float = 0
    halted: bool = False
    pending_exit: str = ''
    expires_at: float = 0
    last_exit_time: float = 0

    def equity(self, price):
        return self.cash + self.quantity * price

    def execute(self, action, price, config, reason, timestamp):
        assert_market_allowed(config['symbol'])
        assert_capital(config['capital'])
        if action == 'BUY':
            if self.expires_at and timestamp >= self.expires_at:
                raise ValueError('خرید پس از پایان مهلت ۳ روزه ممنوع است.')
            if not self.expires_at:
                self.expires_at = timestamp + DURATION_SECONDS
        fee = config['fee'] / 100
        slip = config['slippage'] / 100
        pnl = None
        if action == 'BUY' and not self.quantity and not self.halted:
            budget = min(self.cash, self.equity(price) * config['allocation'] / 100)
            fill = price * (1 + slip)
            amount = budget / (fill * (1 + fee))
            charge = amount * fill * fee
            self.cash -= budget
            self.quantity, self.cost, self.entry = amount, budget, fill
        elif action == 'SELL' and self.quantity:
            fill, amount = price * (1 - slip), self.quantity
            charge = amount * fill * fee
            proceeds = amount * fill - charge
            pnl = proceeds - self.cost
            self.realized += pnl
            self.cash += proceeds
            self.quantity = self.cost = self.entry = 0
            self.pending_exit = ''
            self.last_exit_time = timestamp
        else:
            return None
        self.fees += charge
        return dict(action=action, price=fill, quantity=amount, fee=charge, pnl=pnl,
                    reason=reason, t=timestamp, notional=fill * amount, rule_version=RULE_VERSION)

    def risk(self, price, config):
        equity = self.equity(price)
        self.peak = max(self.peak, equity)
        current_dd = (1 - equity / self.peak) * 100
        self.drawdown = max(self.drawdown, current_dd)
        if current_dd >= config['max_drawdown']:
            self.halted = True
            return 'توقف ریسک: سقف افت سرمایه فعال شد'
        if self.quantity and self.entry:
            change = (price / self.entry - 1) * 100
            if change <= -config['stop_loss']:
                return 'خروج خودکار: حد ضرر'
            if change >= config['take_profit']:
                return 'خروج خودکار: حد سود'
        return None


def new_account(config, started_at=None):
    assert_capital(config['capital'])
    assert_market_allowed(config['symbol'])
    return Account(cash=config['capital'], peak=config['capital'],
                   expires_at=started_at + DURATION_SECONDS if started_at is not None else 0)


def step(account, history, bar, config):
    assert_market_allowed(config['symbol'])
    expired = bool(account.expires_at and bar['t'] >= account.expires_at)
    # Decide with previous closed bars; execute at the following open, never the signal close.
    opening_risk = account.risk(bar['o'], config)
    risk = account.pending_exit or opening_risk
    account.pending_exit = ''
    # Forced exits and expiry must never wait for an LLM or depend on its answer.
    in_cooldown = bool(account.last_exit_time and bar['t'] - account.last_exit_time < 2 * BAR_SECONDS)
    holding_position = bool(account.quantity)
    entry_allowed = not holding_position and not in_cooldown
    decision = (dict(action='HOLD', confidence=0, reason='توقف یا خروج اجباری', indicators={}, samples=0)
                if expired or risk or account.halted else decide(history, config, entry_allowed=entry_allowed,
                                                                 in_cooldown=in_cooldown,
                                                                 holding_position=holding_position))
    action = 'SELL' if risk and account.quantity else decision['action']
    if account.halted and action != 'SELL':
        action = 'HOLD'
    reason = risk or decision['reason']
    if expired:
        action = 'SELL' if account.quantity else 'HOLD'
        reason = 'پایان مهلت ۳ روزه؛ تسویه دفتر آزمایشی به تومان'
        account.halted = True
    trade = account.execute(action, bar['o'], config, reason, bar['t'])
    close_risk = account.risk(bar['c'], config)
    if close_risk and account.quantity:
        account.pending_exit = close_risk
    # Closing-bar checks are executed at next bar's open (including gaps).
    return {**decision, 'action': action, 'reason': reason, 't': bar['t'], 'price': bar['o'],
            'trade': trade, 'equity': account.equity(bar['c']), 'pending_risk': close_risk}


def backtest(bars, config):
    config = validate(config)
    if len(bars) < 80:
        raise ValueError('حداقل ۸۰ کندل لازم است.')
    account = new_account(config, started_at=bars[60]['t'])
    decisions, trades, curve = [], [], []
    last_index = 59
    for index in range(60, len(bars)):
        if bars[index]['t'] + BAR_SECONDS > account.expires_at:
            break
        item = step(account, bars[max(0, index - 150):index], bars[index], config)
        last_index = index
        decisions.append(item)
        if item['trade']:
            trades.append(item['trade'])
        curve.append(dict(t=bars[index]['t'], equity=item['equity']))
    # Deterministic terminal liquidation, including fees and adverse slippage.
    terminal_bar = bars[last_index]
    terminal_time = min(terminal_bar['t'] + BAR_SECONDS, account.expires_at)
    terminal = account.execute('SELL', terminal_bar['c'], config, 'تسویه پایان بک‌تست یا سقف ۷۲ ساعت به تومان', terminal_time)
    if terminal:
        trades.append(terminal)
        curve[-1]['equity'] = account.cash
    sells = [t for t in trades if t['pnl'] is not None]
    gains = sum(max(0, t['pnl']) for t in sells)
    losses = -sum(min(0, t['pnl']) for t in sells)
    account.risk(terminal_bar['c'], config)
    return dict(config=config, source='synthetic' if config['source'] == 'demo' else 'wallex_or_csv',
                rule_version=RULE_VERSION, duration_limit_seconds=DURATION_SECONDS,
                evaluated_from=bars[60]['t'], evaluated_until=terminal_time, credit_fee_tmn=0,
                account=asdict(account), trades=trades, decisions=decisions, curve=curve,
                metrics=dict(net_return=(account.cash / config['capital'] - 1) * 100,
                             realized=account.realized, max_drawdown=account.drawdown,
                             win_rate=100 * sum(t['pnl'] > 0 for t in sells) / len(sells) if sells else 0,
                             trades=len(trades), round_trips=len(sells), fees=account.fees,
                             profit_factor=gains / losses if losses else None,
                             volume=sum(t['notional'] for t in trades)))
