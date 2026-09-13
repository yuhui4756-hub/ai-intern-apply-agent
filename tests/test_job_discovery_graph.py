from app.services.job_discovery_graph import DiscoveryTaskOperations, run_job_discovery_graph


def test_graph_runs_search_then_detail_steps_before_finishing():
    events = []
    search_steps = [{"id": 1}, {"id": 2}]
    detail_steps = [{"id": 3}]

    def next_search(_task_id):
        return search_steps.pop(0) if search_steps else None

    def next_detail(_task_id):
        return detail_steps.pop(0) if detail_steps else None

    operations = DiscoveryTaskOperations(
        check_control=lambda _task_id: None,
        next_search_step=next_search,
        run_search_step=lambda _task_id, step: events.append(f"search:{step['id']}"),
        ensure_detail_steps=lambda _task_id: events.append("prepare_details") or 1,
        next_detail_step=next_detail,
        run_detail_step=lambda _task_id, step: events.append(f"detail:{step['id']}"),
        finish_task=lambda _task_id: events.append("finish") or {"status": "完成", "note": "图编排完成"},
    )

    state = run_job_discovery_graph(9, operations)

    assert events == ["search:1", "search:2", "prepare_details", "detail:3", "finish"]
    assert state["result"] == {"status": "完成", "note": "图编排完成"}


def test_graph_stops_before_any_step_when_control_requests_pause():
    operations = DiscoveryTaskOperations(
        check_control=lambda _task_id: {"status": "已暂停", "note": "任务已暂停"},
        next_search_step=lambda _task_id: (_ for _ in ()).throw(AssertionError("暂停后不应读取搜索步骤")),
        run_search_step=lambda _task_id, _step: (_ for _ in ()).throw(AssertionError("暂停后不应搜索")),
        ensure_detail_steps=lambda _task_id: (_ for _ in ()).throw(AssertionError("暂停后不应创建 JD 步骤")),
        next_detail_step=lambda _task_id: (_ for _ in ()).throw(AssertionError("暂停后不应读取 JD 步骤")),
        run_detail_step=lambda _task_id, _step: (_ for _ in ()).throw(AssertionError("暂停后不应读取 JD")),
        finish_task=lambda _task_id: (_ for _ in ()).throw(AssertionError("暂停后不应结束任务")),
    )

    state = run_job_discovery_graph(9, operations)

    assert state["result"] == {"status": "已暂停", "note": "任务已暂停"}
