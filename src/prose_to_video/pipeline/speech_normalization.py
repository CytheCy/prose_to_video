"""Comparison keys only: never rewrite manuscript text or manufacture timestamps."""
from decimal import Decimal
import re
import unicodedata

_SMALL = dict(zip('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split(), range(20)))
_TENS = dict(zip('twenty thirty forty fifty sixty seventy eighty ninety'.split(), range(20, 100, 10)))
_SCALES = {'thousand': 1000, 'million': 1000000, 'billion': 1000000000}
_ALIASES = {'okay': 'ok', 'mister': 'mr', 'doctor': 'dr', 'missus': 'mrs'}


def _number(parts):
    """Strict cardinals, plus spoken years such as twenty twenty three."""
    if not parts:
        return None
    if len(parts) in (2, 3) and parts[0] in ('nineteen', 'twenty', 'twentyone'):
        tail = _number(parts[1:])
        if tail is not None and 10 <= tail < 100:
            return _SMALL.get(parts[0], 20 if parts[0] == 'twenty' else 21) * 100 + tail
    if parts[-1] in _SCALES:
        prefix = _number(parts[:-1])
        return prefix * _SCALES[parts[-1]] if prefix is not None and 0 < prefix < _SCALES[parts[-1]] else None
    for scale, multiplier in reversed(list(_SCALES.items())):
        if scale in parts:
            i = parts.index(scale)
            left, right = _number(parts[:i]), _number(parts[i+1:])
            if left is not None and right is not None and 0 < left < multiplier and right < multiplier:
                return left * multiplier + right
            return None
    if 'hundred' in parts:
        i = parts.index('hundred')
        if i != 1 or parts[0] not in _SMALL or not 0 < _SMALL[parts[0]] < 10:
            return None
        tail = parts[2:]
        if tail[:1] == ['and']:
            tail = tail[1:]
        value = _number(tail) if tail else 0
        return _SMALL[parts[0]] * 100 + value if value is not None and value < 100 else None
    if len(parts) == 1:
        return _SMALL.get(parts[0], _TENS.get(parts[0]))
    if len(parts) == 2 and parts[0] in _TENS and parts[1] in _SMALL and 0 < _SMALL[parts[1]] < 10:
        return _TENS[parts[0]] + _SMALL[parts[1]]
    return None


def comparison_key(text):
    text = ''.join(c for c in unicodedata.normalize('NFKD', text.casefold()) if not unicodedata.combining(c))
    # Dotted acronyms and apostrophes do not change pronunciation.
    text = re.sub(r"\b(?:[a-z]\.){2,}", lambda m: m.group().replace('.', ''), text)
    text = re.sub(r"['’]", '', text)
    parts = re.findall(r'\d+(?:,\d{3})*(?:\.\d+)?|\w+', text.replace('_', ''), re.UNICODE)
    result = []
    i = 0
    while i < len(parts):
        if re.fullmatch(r'\d+(?:,\d{3})*(?:\.\d+)?', parts[i]):
            number = format(Decimal(parts[i].replace(',', '')).normalize(), 'f')
            result.append('#' + number + '#'); i += 1; continue
        for end in range(min(len(parts), i + 8), i, -1):
            value = _number(parts[i:end])
            if value is not None:
                result.append('#' + str(value) + '#'); i = end; break
        else:
            result.append(_ALIASES.get(parts[i], parts[i])); i += 1
    return ''.join(result)
