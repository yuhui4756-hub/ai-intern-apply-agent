from fastapi.testclient import TestClient


def create_match_review_job(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "job-match-review.sqlite3"))

    from app.db import connect, dumps, init_db, utc_now

    init_db()
    now = utc_now()
    with connect() as conn:
        resume_id = conn.execute("SELECT id FROM resume_versions ORDER BY id LIMIT 1").fetchone()["id"]
        conn.execute(
            "UPDATE resume_versions SET parsed_text = ? WHERE id = ?",
            ("RAG 项目实践，邮箱 student@example.com，手机 13900139000。", resume_id),
        )
        job_id = int(
            conn.execute(
                """
                INSERT INTO job_postings (
                    platform, title, company, jd_text, extracted_json, selected_resume_id,
                    match_score, match_level, risk_level, recommendation, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Boss 直聘",
                    "AI Agent 开发实习生",
                    "杭州复核智能科技有限公司",
                    "要求 Python、FastAPI、RAG，使用 LangGraph 编排 Agent 工作流。联系电话 13700137000。",
                    dumps(
                        {
                            "extracted": {
                                "required_skills": ["Python", "FastAPI", "RAG"],
                                "bonus_skills": ["LangGraph"],
                                "responsibilities": ["开发 Agent 工作流"],
                                "requirements": ["有 RAG 项目实践"],
                            },
                            "scoring": {
                                "matching_evidence": "简历正文",
                                "matched_skills": ["Python", "RAG"],
                                "missing_skills": ["FastAPI"],
                                "fit_notes": ["项目方向匹配"],
                                "preference_notes": [],
                                "risk_signals": [],
                                "caution_signals": [],
                            },
                        }
                    ),
                    resume_id,
                    76,
                    "高匹配",
                    "低",
                    "必投",
                    "待确认",
                    now,
                    now,
                ),
            ).lastrowid
        )
    return job_id


def test_job_match_review_keeps_local_decision_and_redacts_model_input(tmp_path, monkeypatch):
    job_id = create_match_review_job(tmp_path, monkeypatch)

    from app import main
    from app.db import connect, loads

    class FakeClient:
        configured = True
        profile = {"name": "复核模型"}
        model = "review-model"

        def __init__(self):
            self.messages = []

        def complete_json(self, messages):
            self.messages = messages
            return {
                "conclusion": "本地规则显示岗位匹配度较高，仍应确认实际职责。",
                "matched_evidence": ["候选人明确填写过 RAG 项目实践。"],
                "gaps": ["FastAPI 需要按候选人实际经历确认。"],
                "questions_to_confirm": ["团队当前 Agent 工作流主要解决什么业务问题？"],
                "caution_points": ["LangGraph 经验未在候选人资料中明确出现，应如实说明正在学习。"],
                "resume_focus": ["突出 RAG 检索评测和工程验证。"],
            }

        def log_error(self, _error):
            raise AssertionError("正常模型输出不应记录错误")

    fake_client = FakeClient()
    monkeypatch.setattr(main, "client_for_task", lambda task_type: fake_client if task_type == "job_match" else None)
    client = TestClient(main.app)

    response = client.post(f"/jobs/{job_id}/match-review", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/jobs/{job_id}?notice=")
    payload = fake_client.messages[1]["content"]
    assert "student@example.com" not in payload
    assert "13900139000" not in payload
    assert "13700137000" not in payload
    assert "[邮箱已省略]" in payload
    assert "[手机号已省略]" in payload
    with connect() as conn:
        job = conn.execute(
            "SELECT match_score, match_level, risk_level, recommendation, status FROM job_postings WHERE id = ?", (job_id,)
        ).fetchone()
        review = conn.execute(
            "SELECT status, review_json, model_profile, model_name FROM job_match_reviews WHERE job_id = ?", (job_id,)
        ).fetchone()
        action = conn.execute(
            "SELECT action_type, status, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert tuple(job) == (76, "高匹配", "低", "必投", "待确认")
    assert review["status"] == "已完成"
    assert review["model_profile"] == "复核模型"
    assert review["model_name"] == "review-model"
    assert loads(review["review_json"], {})["gaps"] == ["FastAPI 需要按候选人实际经历确认。"]
    assert action["action_type"] == "job_match_review"
    decision = loads(action["decision_json"], {})
    assert decision["local_score_changed"] is False
    assert decision["job_status_changed"] is False
    assert decision["input_redacted"] is True

    page = client.get(f"/jobs/{job_id}")
    assert page.status_code == 200
    assert "深度匹配复核" in page.text
    assert "团队当前 Agent 工作流主要解决什么业务问题" in page.text


def test_job_match_review_without_model_does_not_store_a_review(tmp_path, monkeypatch):
    job_id = create_match_review_job(tmp_path, monkeypatch)

    from app import main
    from app.db import connect, loads

    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    client = TestClient(main.app)

    response = client.post(f"/jobs/{job_id}/match-review", follow_redirects=False)

    assert response.status_code == 303
    with connect() as conn:
        review_count = conn.execute("SELECT COUNT(*) AS count FROM job_match_reviews WHERE job_id = ?", (job_id,)).fetchone()["count"]
        job = conn.execute("SELECT match_score, recommendation, status FROM job_postings WHERE id = ?", (job_id,)).fetchone()
        action = conn.execute("SELECT status, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert review_count == 0
    assert tuple(job) == (76, "必投", "待确认")
    assert action["status"] == "未配置"
    assert loads(action["decision_json"], {})["model_called"] is False
