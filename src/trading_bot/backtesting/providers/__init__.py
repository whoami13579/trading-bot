from .dukascopy import DukascopyProvider
from .yahoo import YahooProvider


def provider(name, cache, **kwargs):
    if name == "dukascopy":
        return DukascopyProvider(cache, **kwargs)
    if name == "yfinance":
        return YahooProvider(cache, **kwargs)
    raise ValueError("Unsupported historical data source")
