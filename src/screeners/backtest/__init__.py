"""Measure what the rules would actually have done."""
from .engine import BacktestResult, run_backtest
from .stats import Outcome, summarise

__all__ = ["run_backtest", "BacktestResult", "summarise", "Outcome"]
