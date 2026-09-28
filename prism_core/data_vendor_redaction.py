"""Keep third-party data-vendor names and their page URLs out of published text.

Presentation-only: numbers, dates and decisions are never changed. Internal logs,
fetchers and machine records may still name the provider.
"""

import re

_GENERIC = {"ko": "증권정보 제공사", "en": "a market data provider"}
# Upper-case WISE only as the vendor's industry classification, never the English word.
_VENDOR = r"(?:WiseReport|WiseFn|와이즈리포트|와이즈에프엔|(?-i:WISE)(?=[ \t]*(?:산업|업종)[ \t]*분류))"
_HOST = r"https?://(?:[\w-]+\.)*wisereport\.co\.kr[^\s<>)\]]*"
_LINK = re.compile(r"\[([^\]\n]+)\]\(\s*<?" + _HOST + r">?\s*\)", re.I)
_URL = re.compile(r"[ \t]*\(\s*<?" + _HOST + r">?\s*\)|<?" + _HOST + r">?", re.I)
# "WiseFn 선정 비교기업" / "WiseFn-selected peers" already say who selected them.
_SELECTED = re.compile(_VENDOR + r"[ \t]*-?[ \t]*(?=선정|selected)", re.I)
_NAME = re.compile(_VENDOR, re.I)


def redact_data_vendor_names(text, language="ko"):
    if not text:
        return text
    text = _URL.sub("", _LINK.sub(r"\1", text))
    text = _SELECTED.sub("", text)
    return _NAME.sub(_GENERIC["ko" if language == "ko" else "en"], text)
