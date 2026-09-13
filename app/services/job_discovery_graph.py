from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph


class DiscoveryGraphState(TypedDict, total=False):
    task_id: int
    route: str
    result: dict[str, Any]
    current_detail_step: dict[str, Any] | None


@dataclass(frozen=True)
class DiscoveryTaskOperations:
    """Existing side-effecting services exposed as explicit LangGraph node operations."""

    check_control: Callable[[int], dict[str, Any] | None]
    next_search_step: Callable[[int], dict[str, Any] | None]
    run_search_step: Callable[[int, dict[str, Any]], None]
    ensure_detail_steps: Callable[[int], int]
    next_detail_step: Callable[[int], dict[str, Any] | None]
    run_detail_step: Callable[[int, dict[str, Any]], None]
    finish_task: Callable[[int], dict[str, Any]]


def run_job_discovery_graph(task_id: int, operations: DiscoveryTaskOperations) -> dict[str, Any]:
    """Run the read-only discovery workflow while SQLite remains its durable source of truth.

    LangGraph selects the next bounded workflow phase. Browser reads, state changes,
    pause/cancel handling, and all auditing remain in the existing local services.
    """

    def control(state: DiscoveryGraphState) -> DiscoveryGraphState:
        stopped = operations.check_control(int(state["task_id"]))
        if stopped:
            return {"route": "stop", "result": stopped}
        return {"route": "search"}

    def search(state: DiscoveryGraphState) -> DiscoveryGraphState:
        current_task_id = int(state["task_id"])
        step = operations.next_search_step(current_task_id)
        if not step:
            return {"route": "prepare_details"}
        operations.run_search_step(current_task_id, step)
        return {"route": "control"}

    def prepare_details(state: DiscoveryGraphState) -> DiscoveryGraphState:
        operations.ensure_detail_steps(int(state["task_id"]))
        return {"route": "detail_control"}

    def detail_control(state: DiscoveryGraphState) -> DiscoveryGraphState:
        current_task_id = int(state["task_id"])
        stopped = operations.check_control(current_task_id)
        if stopped:
            return {"route": "stop", "result": stopped}
        step = operations.next_detail_step(current_task_id)
        if step:
            return {"route": "detail", "current_detail_step": step}
        return {"route": "finish"}

    def detail(state: DiscoveryGraphState) -> DiscoveryGraphState:
        current_task_id = int(state["task_id"])
        step = state.get("current_detail_step")
        if not step:
            return {"route": "detail_control"}
        operations.run_detail_step(current_task_id, step)
        return {"route": "detail_control", "current_detail_step": None}

    def finish(state: DiscoveryGraphState) -> DiscoveryGraphState:
        return {"route": "end", "result": operations.finish_task(int(state["task_id"]))}

    graph = StateGraph(DiscoveryGraphState)
    graph.add_node("control", control)
    graph.add_node("search", search)
    graph.add_node("prepare_details", prepare_details)
    graph.add_node("detail_control", detail_control)
    graph.add_node("detail", detail)
    graph.add_node("finish", finish)
    graph.add_edge(START, "control")
    graph.add_conditional_edges("control", lambda state: state["route"], {"search": "search", "stop": END})
    graph.add_conditional_edges(
        "search",
        lambda state: state["route"],
        {"control": "control", "prepare_details": "prepare_details"},
    )
    graph.add_edge("prepare_details", "detail_control")
    graph.add_conditional_edges(
        "detail_control",
        lambda state: state["route"],
        {"detail": "detail", "finish": "finish", "stop": END},
    )
    graph.add_edge("detail", "detail_control")
    graph.add_edge("finish", END)
    return graph.compile().invoke({"task_id": task_id}, {"recursion_limit": 64})
