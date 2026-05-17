#!/usr/bin/env python3
"""Simplified TradingAgents runner.

Only prompts for the ticker symbol. Everything else comes from DEFAULT_CONFIG
(which picks up TRADINGAGENTS_* env-var overrides from .env). Report is always
saved automatically.
"""

import datetime
import time
from pathlib import Path

import questionary
from rich.console import Console
from rich.live import Live

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.graph.analyst_execution import (
    AnalystWallTimeTracker,
    build_analyst_execution_plan,
    get_initial_analyst_node,
)
from tradingagents.default_config import DEFAULT_CONFIG
from cli.main import (
    message_buffer,
    create_layout,
    update_display,
    update_analyst_statuses,
    update_research_team_status,
    save_report_to_disk,
    display_complete_report,
    classify_message_type,
    ANALYST_ORDER,
)
from cli.utils import detect_asset_type, filter_analysts_for_asset_type
from cli.models import AnalystType
from cli.stats_handler import StatsCallbackHandler

console = Console()

# DEFAULT_CONFIG already applies TRADINGAGENTS_* env-var overrides, so model,
# provider, backend URL, etc. are all controlled via .env.
config = DEFAULT_CONFIG.copy()
config["max_debate_rounds"] = 1
config["max_risk_discuss_rounds"] = 1


def _get_ticker() -> str:
    ticker = questionary.text(
        "Ticker symbol (e.g. AAPL, SPY, BTC-USD):",
        validate=lambda v: bool(v.strip()) or "Please enter a ticker symbol.",
    ).ask()
    if not ticker:
        console.print("[red]No ticker provided. Exiting.[/red]")
        raise SystemExit(1)
    return ticker.strip().upper()


def run() -> None:
    ticker = _get_ticker()
    trade_date = datetime.date.today().strftime("%Y-%m-%d")
    asset_type = detect_asset_type(ticker)
    console.print(
        f"[green]Asset:[/green] {ticker}  "
        f"[green]Type:[/green] {asset_type.value}  "
        f"[green]Date:[/green] {trade_date}"
    )

    all_analysts = [AnalystType.MARKET, AnalystType.SOCIAL, AnalystType.NEWS, AnalystType.FUNDAMENTALS]
    analyst_objects = filter_analysts_for_asset_type(all_analysts, asset_type)
    selected_analyst_keys = [k for k in ANALYST_ORDER if k in {a.value for a in analyst_objects}]

    analyst_execution_plan = build_analyst_execution_plan(
        selected_analyst_keys, concurrency_limit=config["analyst_concurrency_limit"]
    )
    analyst_wall_time_tracker = AnalystWallTimeTracker(analyst_execution_plan)
    stats_handler = StatsCallbackHandler()

    graph = TradingAgentsGraph(selected_analyst_keys, config=config, debug=False, callbacks=[stats_handler])
    message_buffer.init_for_analysis(selected_analyst_keys)

    start_time = time.time()
    layout = create_layout()

    with Live(layout, refresh_per_second=4):
        update_display(layout, stats_handler=stats_handler, start_time=start_time)
        message_buffer.add_message("System", f"Ticker: {ticker}  |  {asset_type.value}  |  {trade_date}")
        message_buffer.add_message("System", f"Analysts: {', '.join(selected_analyst_keys)}")

        first_analyst = get_initial_analyst_node(analyst_execution_plan)
        message_buffer.update_agent_status(first_analyst, "in_progress")
        analyst_wall_time_tracker.mark_started(selected_analyst_keys[0])
        update_display(layout, stats_handler=stats_handler, start_time=start_time)

        init_state = graph.propagator.create_initial_state(ticker, trade_date, asset_type=asset_type.value)
        args = graph.propagator.get_graph_args(callbacks=[stats_handler])

        trace = []
        for chunk in graph.graph.stream(init_state, **args):
            for message in chunk.get("messages", []):
                msg_id = getattr(message, "id", None)
                if msg_id is not None:
                    if msg_id in message_buffer._processed_message_ids:
                        continue
                    message_buffer._processed_message_ids.add(msg_id)
                msg_type, content = classify_message_type(message)
                if content and content.strip():
                    message_buffer.add_message(msg_type, content)
                if hasattr(message, "tool_calls") and message.tool_calls:
                    for tc in message.tool_calls:
                        if isinstance(tc, dict):
                            message_buffer.add_tool_call(tc["name"], tc["args"])
                        else:
                            message_buffer.add_tool_call(tc.name, tc.args)

            update_analyst_statuses(message_buffer, chunk, wall_time_tracker=analyst_wall_time_tracker)

            if chunk.get("investment_debate_state"):
                debate = chunk["investment_debate_state"]
                bull = debate.get("bull_history", "").strip()
                bear = debate.get("bear_history", "").strip()
                judge = debate.get("judge_decision", "").strip()
                if bull or bear:
                    update_research_team_status("in_progress")
                if bull:
                    message_buffer.update_report_section("investment_plan", f"### Bull Researcher\n{bull}")
                if bear:
                    message_buffer.update_report_section("investment_plan", f"### Bear Researcher\n{bear}")
                if judge:
                    message_buffer.update_report_section("investment_plan", f"### Research Manager\n{judge}")
                    update_research_team_status("completed")
                    message_buffer.update_agent_status("Trader", "in_progress")

            if chunk.get("trader_investment_plan"):
                message_buffer.update_report_section("trader_investment_plan", chunk["trader_investment_plan"])
                if message_buffer.agent_status.get("Trader") != "completed":
                    message_buffer.update_agent_status("Trader", "completed")
                    message_buffer.update_agent_status("Aggressive Analyst", "in_progress")

            if chunk.get("risk_debate_state"):
                risk = chunk["risk_debate_state"]
                agg = risk.get("aggressive_history", "").strip()
                con = risk.get("conservative_history", "").strip()
                neu = risk.get("neutral_history", "").strip()
                judge = risk.get("judge_decision", "").strip()
                if agg:
                    if message_buffer.agent_status.get("Aggressive Analyst") != "completed":
                        message_buffer.update_agent_status("Aggressive Analyst", "in_progress")
                    message_buffer.update_report_section("final_trade_decision", f"### Aggressive Analyst\n{agg}")
                if con:
                    if message_buffer.agent_status.get("Conservative Analyst") != "completed":
                        message_buffer.update_agent_status("Conservative Analyst", "in_progress")
                    message_buffer.update_report_section("final_trade_decision", f"### Conservative Analyst\n{con}")
                if neu:
                    if message_buffer.agent_status.get("Neutral Analyst") != "completed":
                        message_buffer.update_agent_status("Neutral Analyst", "in_progress")
                    message_buffer.update_report_section("final_trade_decision", f"### Neutral Analyst\n{neu}")
                if judge:
                    message_buffer.update_report_section("final_trade_decision", f"### Portfolio Manager\n{judge}")
                    message_buffer.update_agent_status("Aggressive Analyst", "completed")
                    message_buffer.update_agent_status("Conservative Analyst", "completed")
                    message_buffer.update_agent_status("Neutral Analyst", "completed")
                    message_buffer.update_agent_status("Portfolio Manager", "completed")

            update_display(layout, stats_handler=stats_handler, start_time=start_time)
            trace.append(chunk)

        final_state: dict = {}
        for chunk in trace:
            final_state.update(chunk)

        graph.process_signal(final_state["final_trade_decision"])

        for agent in message_buffer.agent_status:
            message_buffer.update_agent_status(agent, "completed")
        message_buffer.add_message("System", analyst_wall_time_tracker.format_summary())

        for section in message_buffer.report_sections:
            if section in final_state:
                message_buffer.update_report_section(section, final_state[section])

        update_display(layout, stats_handler=stats_handler, start_time=start_time)

    console.print("\n[bold cyan]Analysis complete![/bold cyan]")
    console.print(f"[dim]{analyst_wall_time_tracker.format_summary()}[/dim]")

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    save_path = Path("reports") / f"{ticker}_{timestamp}"
    report_file = save_report_to_disk(final_state, ticker, save_path)
    console.print(f"\n[green]✓ Report saved:[/green] {save_path.resolve()}")
    console.print(f"  [dim]{report_file.name}[/dim]")

    display_complete_report(final_state)


if __name__ == "__main__":
    run()
