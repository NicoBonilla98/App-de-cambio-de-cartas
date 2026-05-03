from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django import template


register = template.Library()


@register.filter
def money2(value):
    if value in (None, ''):
        return '0.00'
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return value
    return f"{amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"
