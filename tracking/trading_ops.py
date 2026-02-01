"""
Trading Operations for Stock Tracking

Buy/sell decision logic and message formatting.
Extracted from stock_tracking_agent.py for LLM context efficiency.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, Tuple

from tracking.helpers import parse_price_value

logger = logging.getLogger(__name__)


def analyze_sell_decision(stock_data: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Sell decision analysis.

    Args:
        stock_data: Stock information

    Returns:
        Tuple[bool, str]: Whether to sell, sell reason
    """
    try:
        ticker = stock_data.get('ticker', '')
        buy_price = stock_data.get('buy_price', 0)
        buy_date = stock_data.get('buy_date', '')
        current_price = stock_data.get('current_price', 0)
        target_price = stock_data.get('target_price', 0)
        stop_loss = stock_data.get('stop_loss', 0)

        # Calculate profit rate
        profit_rate = ((current_price - buy_price) / buy_price) * 100

        # Days elapsed from buy date
        buy_datetime = datetime.strptime(buy_date, "%Y-%m-%d %H:%M:%S")
        days_passed = (datetime.now() - buy_datetime).days

        # Extract scenario information
        scenario_str = stock_data.get('scenario', '{}')
        investment_period = "Medium-term"

        try:
            if isinstance(scenario_str, str):
                scenario_data = json.loads(scenario_str)
                investment_period = scenario_data.get('investment_period', 'Medium-term')
        except:
            pass

        # Check stop-loss condition
        if stop_loss > 0 and current_price <= stop_loss:
            return True, f"Stop-loss condition reached (Stop-loss: {stop_loss:,.0f} KRW)"

        # Check target price reached
        if target_price > 0 and current_price >= target_price:
            return True, f"Target price achieved (Target: {target_price:,.0f} KRW)"

        # Sell conditions by investment period
        if investment_period == "Short-term":
            if days_passed >= 15 and profit_rate >= 5:
                return True, f"Short-term goal achieved (Held: {days_passed} days, Return: {profit_rate:.2f}%)"
            if days_passed >= 10 and profit_rate <= -3:
                return True, f"Short-term loss protection (Held: {days_passed} days, Return: {profit_rate:.2f}%)"

        # General sell conditions
        if profit_rate >= 10:
            return True, f"Return 10%+ achieved (Current return: {profit_rate:.2f}%)"

        if profit_rate <= -5:
            return True, f"Loss -5%+ incurred (Current return: {profit_rate:.2f}%)"

        if days_passed >= 30 and profit_rate < 0:
            return True, f"Held 30+ days with loss (Held: {days_passed} days, Return: {profit_rate:.2f}%)"

        if days_passed >= 60 and profit_rate >= 3:
            return True, f"Held 60+ days with 3%+ profit (Held: {days_passed} days, Return: {profit_rate:.2f}%)"

        if investment_period == "Long-term" and days_passed >= 90 and profit_rate < 0:
            return True, f"Long-term loss cleanup (Held: {days_passed} days, Return: {profit_rate:.2f}%)"

        return False, "Continue holding"

    except Exception as e:
        logger.error(f"Error analyzing sell: {str(e)}")
        return False, "Analysis error"


def format_buy_message(
    company_name: str,
    ticker: str,
    current_price: float,
    scenario: Dict[str, Any],
    rank_change_msg: str = ""
) -> str:
    """
    Format buy message for Telegram.

    Args:
        company_name: Company name
        ticker: Stock code
        current_price: Current price
        scenario: Trading scenario
        rank_change_msg: Ranking change message

    Returns:
        str: Formatted message
    """
    message = f"📈 New Buy: {company_name}({ticker})\n" \
              f"Buy Price: {current_price:,.0f} KRW\n" \
              f"Target Price: {scenario.get('target_price', 0):,.0f} KRW\n" \
              f"Stop Loss: {scenario.get('stop_loss', 0):,.0f} KRW\n" \
              f"Investment Period: {scenario.get('investment_period', 'Short-term')}\n" \
              f"Sector: {scenario.get('sector', 'Unknown')}\n"

    if scenario.get('valuation_analysis'):
        message += f"Valuation: {scenario.get('valuation_analysis')}\n"

    if scenario.get('sector_outlook'):
        message += f"Sector Outlook: {scenario.get('sector_outlook')}\n"

    if rank_change_msg:
        message += f"Trading Value Analysis: {rank_change_msg}\n"

    message += f"Rationale: {scenario.get('rationale', 'No information')}\n"

    # Format trading scenario section
    trading_scenarios = scenario.get('trading_scenarios', {})
    if trading_scenarios and isinstance(trading_scenarios, dict):
        message += _format_trading_scenarios(trading_scenarios, current_price)

    return message


def _format_trading_scenarios(trading_scenarios: Dict[str, Any], current_price: float) -> str:
    """Format trading scenarios section."""
    message = "\n" + "=" * 40 + "\n"
    message += "📋 Trading Scenarios\n"
    message += "=" * 40 + "\n\n"

    # Key levels
    key_levels = trading_scenarios.get('key_levels', {})
    if key_levels:
        message += "💰 Key Price Levels:\n"

        primary_resistance = parse_price_value(key_levels.get('primary_resistance', 0))
        secondary_resistance = parse_price_value(key_levels.get('secondary_resistance', 0))
        if primary_resistance or secondary_resistance:
            message += "  📈 Resistance:\n"
            if secondary_resistance:
                message += f"    • 2nd: {secondary_resistance:,.0f} KRW\n"
            if primary_resistance:
                message += f"    • 1st: {primary_resistance:,.0f} KRW\n"

        message += f"  ━━ Current Price: {current_price:,.0f} KRW ━━\n"

        primary_support = parse_price_value(key_levels.get('primary_support', 0))
        secondary_support = parse_price_value(key_levels.get('secondary_support', 0))
        if primary_support or secondary_support:
            message += "  📉 Support:\n"
            if primary_support:
                message += f"    • 1st: {primary_support:,.0f} KRW\n"
            if secondary_support:
                message += f"    • 2nd: {secondary_support:,.0f} KRW\n"

        volume_baseline = key_levels.get('volume_baseline', '')
        if volume_baseline:
            message += f"  📊 Volume Baseline: {volume_baseline}\n"

        message += "\n"

    # Sell triggers
    sell_triggers = trading_scenarios.get('sell_triggers', [])
    if sell_triggers:
        message += "🔔 Sell Signals:\n"
        for trigger in sell_triggers:
            if "profit" in trigger.lower() or "target" in trigger.lower() or "resistance" in trigger.lower():
                emoji = "✅"
            elif "loss" in trigger.lower() or "support" in trigger.lower() or "drop" in trigger.lower():
                emoji = "⛔"
            elif "time" in trigger.lower() or "sideways" in trigger.lower():
                emoji = "⏰"
            else:
                emoji = "•"
            message += f"  {emoji} {trigger}\n"
        message += "\n"

    # Hold conditions
    hold_conditions = trading_scenarios.get('hold_conditions', [])
    if hold_conditions:
        message += "✋ Hold Conditions:\n"
        for condition in hold_conditions:
            message += f"  • {condition}\n"
        message += "\n"

    # Portfolio context
    portfolio_context = trading_scenarios.get('portfolio_context', '')
    if portfolio_context:
        message += f"💼 Portfolio Perspective:\n  {portfolio_context}\n"

    return message


def format_sell_message(
    company_name: str,
    ticker: str,
    buy_price: float,
    sell_price: float,
    profit_rate: float,
    holding_days: int,
    sell_reason: str
) -> str:
    """
    Format sell message for Telegram.

    Args:
        company_name: Company name
        ticker: Stock code
        buy_price: Buy price
        sell_price: Sell price
        profit_rate: Profit rate (%)
        holding_days: Holding period (days)
        sell_reason: Sell reason

    Returns:
        str: Formatted message
    """
    arrow = "⬆️" if profit_rate > 0 else "⬇️" if profit_rate < 0 else "➖"
    message = f"📉 Sell: {company_name}({ticker})\n" \
              f"Buy Price: {buy_price:,.0f} KRW\n" \
              f"Sell Price: {sell_price:,.0f} KRW\n" \
              f"Return: {arrow} {abs(profit_rate):.2f}%\n" \
              f"Holding Period: {holding_days} days\n" \
              f"Sell Reason: {sell_reason}"
    return message


def calculate_profit_rate(buy_price: float, current_price: float) -> float:
    """Calculate profit rate percentage."""
    if buy_price <= 0:
        return 0.0
    return ((current_price - buy_price) / buy_price) * 100


def calculate_holding_days(buy_date: str) -> int:
    """Calculate holding period in days."""
    try:
        buy_datetime = datetime.strptime(buy_date, "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - buy_datetime).days
    except:
        return 0
