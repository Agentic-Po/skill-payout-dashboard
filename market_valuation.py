"""Dated market USD for consumption; separate from sealed payout classification.

Historical observations are retained as published. Only the actual current UTC
calendar day may provisionally borrow yesterday's observation. Missing history
never receives a live quote or an arbitrarily old prior-day value.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

POLICY_ID = 'treasury-market-day-v2'


def market_price(day, prices, now):
    today = now.astimezone(timezone.utc).date()
    reference = day
    provisional = False
    # The current UTC day cannot have a completed daily close. A mistakenly
    # retained partial or future entry must not masquerade as historical truth.
    if day >= today.isoformat():
        reference = (today - timedelta(days=1)).isoformat()
        if day != today.isoformat() or reference not in prices:
            return None, None, False
        provisional = True
    elif day not in prices:
        return None, None, False
    value = Decimal(str(prices[reference]))
    if not value.is_finite() or value <= 0:
        raise ValueError('market_price_invalid')
    return value, reference, provisional


def value_consumption(rows, prices, now):
    subtotal = Decimal(0)
    missing_amount = Decimal(0)
    missing_rows = provisional_rows = 0
    sources = {}
    for row in rows:
        amount = Decimal(str(row['val']))
        if not amount.is_finite() or amount < 0:
            raise ValueError('market_amount_invalid')
        day = row['ts'][:10]
        price, reference, provisional = market_price(day, prices, now)
        sources[day] = {'price_date': reference, 'provisional': provisional,
                        'rate_usd': None if price is None else str(price)}
        if price is None and amount:
            missing_rows += 1
            missing_amount += amount
        elif price is not None:
            subtotal += amount * price
            provisional_rows += int(provisional)
    rounded = float(subtotal.quantize(Decimal('.01')))
    return {'usd': None if missing_rows else rounded,
            'priced_usd_subtotal': rounded, 'unpriced_rows': missing_rows,
            'unpriced_mente': str(missing_amount),
            'provisional_rows': provisional_rows, 'rate_sources': sources}
