"""Read numeric tokens without rewriting the user's text or source offsets."""
from decimal import Decimal
import re


CHINESE_DIGITS = '零〇一二两三四五六七八九'
CHINESE_INTEGER = rf'[{CHINESE_DIGITS}十百千万亿]+'
NUMBER = rf'(?:\d+(?:\.\d+)?|\.\d+|{CHINESE_INTEGER}(?:点[{CHINESE_DIGITS}]+)?)'
_DIGITS = dict(zip('零一二三四五六七八九', range(10)))


def _integer(text):
    for symbol, scale in (('亿', 100000000), ('万', 10000)):
        if symbol in text:
            if text.count(symbol) != 1:
                raise ValueError('repeated numeric magnitude')
            head, tail = text.split(symbol)
            prefix = _integer(head)
            # 一万二, like 一百二, omits the place of the final digit.
            if tail and not tail.startswith('零') and not any(c in tail for c in '十百千万'):
                raise ValueError('ambiguous abbreviated number')
            suffix = _integer(tail.lstrip('零')) if tail else 0
            if prefix <= 0 or suffix >= scale:
                raise ValueError('invalid numeric magnitude')
            return prefix * scale + suffix
    if not text or text.endswith('零') and text != '零':
        raise ValueError('incomplete number')
    if all(c in _DIGITS for c in text):
        return int(''.join(str(_DIGITS[c]) for c in text))
    total, digit, previous, zero = 0, None, 10000, False
    for c in text:
        if c == '零':
            if digit is not None or zero or previous == 10000:
                raise ValueError('invalid zero placeholder')
            zero = True
        elif c in _DIGITS:
            if digit is not None:
                raise ValueError('multiple coefficients for a magnitude')
            digit = _DIGITS[c]
        else:
            power = {'十': 10, '百': 100, '千': 1000}[c]
            if power >= previous or digit is None and not (power == 10 and total == 0):
                raise ValueError('invalid numeric magnitude order')
            total += (digit if digit is not None else 1) * power
            digit, previous, zero = None, power, False
    if zero and digit is None or digit is not None and previous >= 100 and not zero:
        raise ValueError('ambiguous abbreviated number')
    return total + (digit or 0)


def number(value):
    text = str(value)
    if re.fullmatch(r'-?(?:\d+(?:\.\d+)?|\.\d+)', text):
        return Decimal(text)
    text = text.translate(str.maketrans({'两': '二', '〇': '零'}))
    sign = -1 if text.startswith('-') else 1
    text = text.removeprefix('-')
    if not re.fullmatch(NUMBER, text):
        raise ValueError('unsupported number')
    integer, point, fractional = text.partition('点')
    result = Decimal(_integer(integer))
    if point:
        result += Decimal('0.' + ''.join(str(_DIGITS[c]) for c in fractional))
    return sign * result
