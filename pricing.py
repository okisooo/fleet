"""API list prices, so subscription token counts can be shown as what the API would charge.

Rates are USD per million tokens and bundled here; nothing is fetched at runtime.
The ledger stores token counts, never dollars, so editing a rate re-prices history.
"""
from __future__ import annotations

import functools
import re

# (input, output, cache write 5m, cache write 1h, cache read)
RATES = {
    # Anthropic's published rates; they win over any other source.
    "claude-fable-5-1": (10, 50, 12.50, 20, 0.25),
    "claude-mythos-5-1": (10, 50, 12.50, 20, 0.25),
    "claude-fable-5": (10, 50, 12.50, 20, 1.00),
    "claude-mythos-5": (10, 50, 12.50, 20, 1.00),
    "claude-opus-5-5": (4, 20, 5, 8, 0.20),
    "claude-opus-5": (5, 25, 6.25, 10, 0.50),
    "claude-opus-4-8": (5, 25, 6.25, 10, 0.50),
    "claude-opus-4-7": (5, 25, 6.25, 10, 0.50),
    "claude-opus-4-6": (5, 25, 6.25, 10, 0.50),
    "claude-sonnet-5-5": (2, 10, 2.50, 4, 0.20),
    "claude-sonnet-5": (2, 10, 2.50, 4, 0.20),
    "claude-sonnet-4-6": (3, 15, 3.75, 6, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.20, 0.01),
    "claude-haiku-4-5": (1, 5, 1.25, 2, 0.10),
    # OpenAI: LiteLLM's model_prices_and_context_window.json, snapshot of
    # 2026-10-22. OpenAI has one cache-write rate, so it fills both columns.
    "gpt-6.1-sol": (2, 10, 2.50, 2.50, 0.10),
    "gpt-6-sol": (2, 10, 2.50, 2.50, 0.20),
    "gpt-6-luna": (0.10, 0.50, 0.125, 0.125, 0.01),
    "gpt-6-astra": (10, 50, 12.50, 12.50, 1.00),
    "gpt-5.6-sol": (4, 20, 5, 5, 0.40),
    "gpt-5.6-terra": (2, 12, 2.50, 2.50, 0.20),
    "gpt-5.6-luna": (0.20, 1.20, 0.25, 0.25, 0.02),
    "gpt-daybreak-blue-latest": (4, 20, 5, 5, 0.40),
    "gpt-5.5": (5, 30, 5, 5, 0.50),  # no cache-write rate listed: billed as input
}

# A request whose prompt (every input token, cached ones included) is above the
# threshold is billed entirely at the long-context rates. Other Claude models
# have no long-context surcharge.
LONG = {
    "claude-haiku-5-5": (100_000, (0.50, 2.50, 0.625, 1.00, 0.05)),
    "gpt-6.1-sol": (272_000, (4, 15, 5, 5, 0.20)),
    "gpt-6-sol": (272_000, (4, 15, 5, 5, 0.40)),
    "gpt-6-luna": (272_000, (0.20, 0.75, 0.25, 0.25, 0.02)),
    "gpt-6-astra": (272_000, (20, 75, 25, 25, 2.00)),
    "gpt-5.6-sol": (272_000, (8, 30, 10, 10, 0.80)),
    "gpt-5.6-terra": (272_000, (4, 18, 5, 5, 0.40)),
    "gpt-5.6-luna": (272_000, (0.40, 1.80, 0.50, 0.50, 0.04)),
    "gpt-daybreak-blue-latest": (272_000, (8, 30, 10, 10, 0.80)),
    "gpt-5.5": (272_000, (10, 45, 10, 10, 1.00)),
}

# Claude fast mode (Opus only) and OpenAI priority processing (Codex's fast
# service tier) multiply every rate. LiteLLM lists OpenAI's priority rates;
# they are exactly these multiples of the standard and long-context rates.
FAST = {
    "claude-opus-5-5": 2,
    "claude-opus-5": 2,
    "claude-opus-4-8": 2,
    "claude-opus-4-7": 2,
    "claude-opus-4-6": 2,
    "gpt-6.1-sol": 2,
    "gpt-6-sol": 2,
    "gpt-6-luna": 2,
    "gpt-6-astra": 2,
    "gpt-5.6-sol": 2,
    "gpt-5.6-terra": 2,
    "gpt-5.6-luna": 2,
    "gpt-5.5": 2.5,
}
# An OpenAI model with no published priority rate is assumed to follow the rule.
ASSUMED_FAST = 2

FAMILIES = ("opus", "sonnet", "haiku", "fable", "mythos", "sol", "luna", "astra", "terra", "daybreak")


@functools.lru_cache(maxsize=1024)
def normalize(model_id):
    """'us.anthropic.claude-opus-5-5-20260301-v1:0', 'claude-opus-5-5[1m]' -> 'claude-opus-5-5'."""
    text = str(model_id or "").strip().lower().split("|", 1)[0]
    text = re.sub(r"\[[^\]]*\]$", "", text)
    text = text.rsplit("/", 1)[-1]
    text = re.sub(r"^(?:(?:us|eu|apac|global|jp|au|anthropic|openai)\.)+", "", text)
    text = re.sub(r"-v\d+(?::\d+)?$", "", text)
    return re.sub(r"(?:[-@]\d{8}|-\d{4}-\d{2}-\d{2})$", "", text)


def _words(model):
    return re.split(r"[-.]", model)


def _version(model):
    return tuple(int(n) for n in re.findall(r"\d+", model))


def _family(model):
    """The newest priced model of the same family: any claude-opus-* -> claude-opus-5-5."""
    vendor = next((v for v in ("claude-", "gpt-") if model.startswith(v)), None)
    if not vendor:
        return None
    words = _words(model)
    name = next((n for n in FAMILIES if n in words), None)
    if name:
        peers = [m for m in RATES if m.startswith(vendor) and name in _words(m)]
    elif re.fullmatch(r"gpt-\d+(?:\.\d+)?", model):
        peers = [m for m in RATES if re.fullmatch(r"gpt-\d+(?:\.\d+)?", m)]
    else:
        return None
    return max(peers, key=_version) if peers else None


@functools.lru_cache(maxsize=1024)
def resolve(model_id):
    """(priced model id, estimated): the exact entry, else a family stand-in; (None, False) if unknown."""
    model = normalize(model_id)
    if model in RATES:
        return model, False
    if model.endswith("-codex") and model[:-6] in RATES:
        return model[:-6], True
    fallback = _family(model)
    return (fallback, True) if fallback else (None, False)


def key(model_id, fast=False, prompt=0):
    """The ledger key of one request: the normalised id plus the flags that set its
    price, e.g. 'claude-opus-5-5|fast', 'gpt-6.1-sol|long'. prompt = all input tokens."""
    model = normalize(model_id)
    priced, _ = resolve(model)
    flags = "|fast" if fast else ""
    if priced in LONG and prompt > LONG[priced][0]:
        flags += "|long"
    return model + flags


def split_key(model_key):
    """'gpt-6.1-sol|fast|long' -> ('gpt-6.1-sol', True, True)."""
    model, *flags = str(model_key).split("|")
    return model, "fast" in flags, "long" in flags


def cost(model_id, input=0, output=0, cache_write_5m=0, cache_write_1h=0, cache_read=0, fast=False, long=False):
    """(USD, estimated) at API list prices, or (None, False) for a model with no known price.

    model_id may be a ledger key; its flags count as fast/long. estimated is set
    when the rate is a family stand-in or an assumed priority multiplier.
    """
    model, key_fast, key_long = split_key(model_id)
    priced, estimated = resolve(model)
    if priced is None:
        return None, False
    rates = RATES[priced]
    if (long or key_long) and priced in LONG:
        rates = LONG[priced][1]
    if fast or key_fast:
        factor = FAST.get(priced)
        if factor is None and priced.startswith("gpt-"):
            factor, estimated = ASSUMED_FAST, True
        rates = tuple(rate * (factor or 1) for rate in rates)
    tokens = (input, output, cache_write_5m, cache_write_1h, cache_read)
    return sum(count * rate for count, rate in zip(tokens, rates)) / 1e6, estimated


def label(model_id):
    """Short display name: 'claude-opus-5-5[1m]' -> 'Opus 5.5', 'gpt-6.1-sol' -> 'GPT-6.1 Sol'."""
    model = normalize(model_id)
    match = re.fullmatch(r"claude-(?:(\d+(?:-\d+)?)-)?(opus|sonnet|haiku|fable|mythos)(?:-(\d+(?:-\d+)?))?", model)
    if match:
        version = (match.group(3) or match.group(1) or "").replace("-", ".")
        return f"{match.group(2).title()} {version}".strip()
    words = model.split("-")
    if words[0] == "gpt":
        head, rest = "GPT", words[1:]
        if rest and re.fullmatch(r"\d+(?:\.\d+)?[a-z]?", rest[0]):
            head += "-" + rest.pop(0)
        return " ".join([head] + [word.title() for word in rest if word and word != "latest"])
    return model or "Unknown"
