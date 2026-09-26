from evals.run_agent_quality_eval import run_evaluation


def test_offline_agent_quality_fixture_passes_without_network_or_model_calls():
    report = run_evaluation()

    assert report["evaluation_version"] == "v0.9"
    assert report["summary"] == {"total": 14, "passed": 14, "failed": 0}
    assert report["suites"]["jd_rule"]["passed"] == 5
    assert report["suites"]["conversation"]["passed"] == 5
    assert report["suites"]["agent_contract"]["passed"] == 4
