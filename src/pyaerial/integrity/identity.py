"""US N-number <-> ICAO address mapping (FAA block A00001-ADF7C7).

The FAA assigns US Mode S addresses algorithmically from the registration, so
an N-number callsign or registration that disagrees with the transmitted
address means the address is cloned, made up, or misprogrammed.
"""

from __future__ import annotations

import re

_CHARSET = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # no I or O
_DIGITS = "0123456789"
_SUFFIX_SIZE = 1 + len(_CHARSET) * (1 + len(_CHARSET))  # 601
_BUCKET4 = 1 + len(_CHARSET) + len(_DIGITS)  # 35
_BUCKET3 = len(_DIGITS) * _BUCKET4 + _SUFFIX_SIZE  # 951
_BUCKET2 = len(_DIGITS) * _BUCKET3 + _SUFFIX_SIZE  # 10111
_BUCKET1 = len(_DIGITS) * _BUCKET2 + _SUFFIX_SIZE  # 101711
US_FIRST = 0xA00001
US_LAST = 0xADF7C7

N_NUMBER_RE = re.compile(r"^N[1-9][0-9A-HJ-NP-Z]{0,4}$")


def _suffix(offset: int) -> str:
    if offset == 0:
        return ""
    first = _CHARSET[(offset - 1) // (len(_CHARSET) + 1)]
    rem = (offset - 1) % (len(_CHARSET) + 1)
    return first if rem == 0 else first + _CHARSET[rem - 1]


def is_us_address(icao: str) -> bool:
    try:
        value = int(icao, 16)
    except ValueError:
        return False
    return US_FIRST <= value <= US_LAST


def icao_to_n_number(icao: str) -> str | None:
    """N-number for a US ICAO address, or ``None`` outside the US block."""
    try:
        value = int(icao, 16)
    except ValueError:
        return None
    if not US_FIRST <= value <= US_LAST:
        return None
    rem = value - US_FIRST
    out = "N" + str(rem // _BUCKET1 + 1)
    rem %= _BUCKET1
    for bucket in (_BUCKET2, _BUCKET3):
        if rem < _SUFFIX_SIZE:
            return out + _suffix(rem)
        rem -= _SUFFIX_SIZE
        out += str(rem // bucket)
        rem %= bucket
    if rem < _SUFFIX_SIZE:
        return out + _suffix(rem)
    rem -= _SUFFIX_SIZE
    out += str(rem // _BUCKET4)
    rem %= _BUCKET4
    if rem == 0:
        return out
    return out + (_CHARSET + _DIGITS)[rem - 1]


def looks_like_n_number(text: str | None) -> bool:
    return bool(text) and bool(N_NUMBER_RE.match(text.strip().upper()))
