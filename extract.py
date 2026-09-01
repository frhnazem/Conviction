"""Rule-based extraction. Never invent a pair. No signals."""

from __future__ import annotations

import re

COINS = [
    ("BTCUSDT", ["btcusdt", "btcusd", "bitcoin", "btc", "xbt"]),
    ("ETHUSDT", ["ethusdt", "ethusd", "ethereum", "ether", "eth"]),
    ("SOLUSDT", ["solusdt", "solusd", "solana", "sol"]),
    ("BNBUSDT", ["bnbusdt", "bnbusd", "binance", "bnb"]),
    ("XRPUSDT", ["xrpusdt", "xrpusd", "ripple", "xrp"]),
    ("ADAUSDT", ["adausdt", "adausd", "cardano", "ada"]),
    ("DOGEUSDT", ["dogeusdt", "dogeusd", "dogecoin", "doge"]),
    ("PEPEUSDT", ["pepeusdt", "pepeusd", "pepe"]),
    ("AVAXUSDT", ["avaxusdt", "avaxusd", "avalanche", "avax"]),
    ("LINKUSDT", ["linkusdt", "linkusd", "chainlink", "link"]),
    ("DOTUSDT", ["dotusdt", "dotusd", "polkadot", "dot"]),
    ("SUIUSDT", ["suiusdt", "suiusd", "sui"]),
    ("NEARUSDT", ["nearusdt", "nearusd", "near"]),
    ("WIFUSDT", ["wifusdt", "wifusd", "wif"]),
    ("SHIBUSDT", ["shibusdt", "shibusd", "shib"]),
]


def _mentions(lower: str, alias: str) -> bool:
    return re.search(rf"\b{re.escape(alias)}\b", lower, re.I) is not None


def sanitize_pair(pair: str | None, raw_text: str) -> str | None:
    if not pair:
        return None
    normalized = re.sub(r"[^A-Z0-9]", "", pair.strip().upper())
    if not normalized:
        return None
    if normalized.endswith("USDT"):
        as_usdt = normalized
    elif normalized.endswith("USD"):
        as_usdt = normalized + "T"
    else:
        as_usdt = normalized + "USDT"
    lower = (raw_text or "").lower()
    for coin, aliases in COINS:
        if coin == as_usdt:
            return coin if any(_mentions(lower, a) for a in aliases) else None
    base = as_usdt.replace("USDT", "").lower()
    if len(base) < 2 or len(base) > 12:
        return None
    if _mentions(lower, as_usdt.lower()) or _mentions(lower, base + "usd") or _mentions(lower, base):
        return as_usdt
    return None


def pair_from_note(raw_text: str) -> str | None:
    lower = (raw_text or "").lower()
    for coin, aliases in COINS:
        for alias in sorted(aliases, key=len, reverse=True):
            if _mentions(lower, alias):
                return coin
    m = re.search(r"\b([a-z0-9]{2,12})usdt\b", lower)
    if m:
        return m.group(1).upper() + "USDT"
    m = re.search(r"\b([a-z0-9]{2,12})usd\b", lower)
    if m:
        return m.group(1).upper() + "USDT"
    return None


def extract_with_rules(raw_text: str) -> dict:
    text = (raw_text or "").strip()
    lower = text.lower()
    result = {
        "pair": None,
        "bias": None,
        "invalidation": None,
        "target": None,
        "size_note": None,
        "confidence_in_parse": 0.0,
        "notes_for_user": None,
    }
    if not text:
        result["notes_for_user"] = "What is the trade idea you want to log?"
        return result

    result["pair"] = pair_from_note(text)

    if re.search(r"\b(long|buy|bullish|going long|enter long)\b", lower):
        result["bias"] = "long"
    elif re.search(r"\b(short|sell|bearish|going short|enter short)\b", lower):
        result["bias"] = "short"
    elif re.search(r"\b(flat|skip|out|no trade|stay out|pass|neutral)\b", lower):
        result["bias"] = "flat"

    inv_pats = [
        r"(?:stop|sl|invalidation|invalid(?:ate)?(?:s|d)?|if\s+(?:it\s+)?(?:loses|breaks|goes\s+below|drops\s+below|falls\s+below))\s*(?:at|@|below|under|around|near)?\s*([0-9]+(?:\.[0-9]+)?k?)",
        r"(?:below|under)\s+([0-9]+(?:\.[0-9]+)?k?)\s*(?:stop|invalidation|invalid)?",
        r"stop\s*[:\s]*([0-9]+(?:\.[0-9]+)?k?)",
    ]
    for pat in inv_pats:
        m = re.search(pat, lower)
        if m:
            result["invalidation"] = m.group(1)
            break

    for pat in [
        r"(?:target(?:ing|s)?|tp|take\s*profit|aim(?:ing)?\s*(?:for)?)\s*(?:at|@|around|near)?\s*([0-9]+(?:\.[0-9]+)?k?)",
        r"(?:targeting|tp)\s+([0-9]+(?:\.[0-9]+)?k?)",
    ]:
        m = re.search(pat, lower)
        if m:
            result["target"] = m.group(1)
            break

    for pat in [
        r"(\d+(?:\.\d+)?\s*%?\s*(?:of\s+)?(?:portfolio|account|size|position|risk|capital|stack))",
        r"((?:size|risk|position)\s*(?:of|:)?\s*\d+(?:\.\d+)?\s*%?)",
        r"(\d+(?:\.\d+)?\s*x\s*(?:leverage|lev))",
        r"(risk(?:ing)?\s+\d+(?:\.\d+)?\s*%)",
    ]:
        m = re.search(pat, lower)
        if m:
            result["size_note"] = m.group(1).strip()
            break

    score = 0.0
    if result["pair"]:
        score += 0.25
    if result["bias"]:
        score += 0.25
    if result["invalidation"]:
        score += 0.25
    if result["target"]:
        score += 0.15
    if result["size_note"]:
        score += 0.1
    if len(text) < 15:
        score *= 0.6
    result["confidence_in_parse"] = round(max(0.0, min(1.0, score)), 2)

    if not result["pair"] and not result["bias"] and score < 0.3:
        result["notes_for_user"] = "What pair and direction are you considering?"
    elif not result["pair"] and result["bias"]:
        result["notes_for_user"] = "Which pair is this for?"
    elif not result["bias"] and result["pair"]:
        result["notes_for_user"] = "Is this a long, short, or flat idea?"
    return result
