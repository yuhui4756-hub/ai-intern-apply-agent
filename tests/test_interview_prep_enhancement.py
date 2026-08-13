from fastapi.testclient import TestClient


def create_review(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "interview-prep-enhancement.sqlite3"))

    from app.db import connect, dumps, init_db, utc_now

    init_db()
    now = utc_now()
    with connect() as conn:
        profile = conn.execute("SELECT id FROM candidate_profile ORDER BY id LIMIT 1").fetchone()
        conn.execute(
            """
            UPDATE candidate_profile
            SET skills_json = ?, projects_json = ?
            WHERE id = ?
            """,
            (
                dumps(["Python", "FastAPI", "RAG"]),
                dumps(
                    [
                        {
                            "name": "本地 RAG 应用",
                            "highlights": ["混合检索", "检索评测", "来源追溯"],
                        }
                    ]
                ),
                profile["id"],
            ),
        )
        resume_id = conn.execute("SELECT id FROM resume_versions ORDER BY id LIMIT 1").fetchone()["id"]
        conn.execute(
            "UPDATE resume_versions SET parsed_text = ? WHERE id = ?",
            ("做过 RAG 检索评测。邮箱 test@example.com，电话 13800138000。", resume_id),
        )
        job_id = int(
            conn.execute(
                """
                INSERT INTO job_postings (
                    platform, title, company, jd_text, extracted_json, selected_resume_id,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Boss 直聘",
                    "AI Agent 开发实习生",
                    "杭州测试智能科技有限公司",
                    "要求 Python、FastAPI、RAG，负责 Agent 工作流与检索服务开发。",
                    dumps(
                        {
                            "extracted": {
                                "required_skills": ["Python", "FastAPI", "RAG"],
                                "bonus_skills": ["LangGraph"],
                                "responsibilities": ["开发 Agent 工作流"],
                                "requirements": ["有 RAG 项目实践"],
                            }
                        }
                    ),
                    resume_id,
                    "待面试",
                    now,
                    now,
                ),
            ).lastrowid
        )
        review_id = int(
            conn.execute(
                """
                INSERT INTO interview_preparations (
                    job_id, source_text, prep_plan_json, question_bank_json,
                    review_markdown, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    "面试邀请：请准备 RAG 项目。联系方式 13800138000。",
                    dumps({"title": "AI Agent 开发实习生", "three_day_plan": ["旧 3 天计划"], "seven_day_plan": ["旧 7 天计划"]}),
                    dumps(["旧问题：RAG 如何评测？"]),
                    "# 旧面试准备",
                    now,
                    now,
                ),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO interview_feedback (
                job_id, interview_preparation_id, feedback_type, question,
                issue_summary, improvement_plan, status, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, review_id, "技术问题", "RAG 召回不准如何排查？", "没有讲重排", "补齐评测链路", "待练习", "manual", now, now),
        )
    return review_id


def test_interview_prep_enhancement_updates_local_materials_and_redacts_context(tmp_path, monkeypatch):
    review_id = create_review(tmp_path, monkeypatch)

    from app import main
    from app.db import connect, loads

    class FakeClient:
        configured = True

        def __init__(self):
            self.messages = []
            self.errors = []

        def complete_json(self, messages):
            self.messages = messages
            return {
                "three_day_plan": ["补齐 RAG 评测表达", "练习 Agent 工具调用", "完成模拟面试"],
                "seven_day_plan": ["梳理 JD", "复习检索", "复习 FastAPI", "项目深挖", "行为题", "模拟", "复盘"],
                "questions": ["请说明 RAG 召回不准时如何按链路排查？", "如何设计 Agent 工具调用的错误处理？"],
                "markdown": "# 强化准备\n\n只基于已有项目事实。",
            }

        def log_error(self, error):
            self.errors.append(error)

    fake_client = FakeClient()
    monkeypatch.setattr(main, "client_for_task", lambda task_type: fake_client if task_type == "interview_prep" else None)
    client = TestClient(main.app)

    response = client.post(f"/interviews/{review_id}/enhance", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/interviews/{review_id}?notice=")
    prompt_payload = fake_client.messages[1]["content"]
    assert "test@example.com" not in prompt_payload
    assert "13800138000" not in prompt_payload
    assert "[邮箱已省略]" in prompt_payload
    assert "[手机号已省略]" in prompt_payload
    assert "RAG 召回不准如何排查" in prompt_payload
    with connect() as conn:
        review = conn.execute(
            "SELECT prep_plan_json, question_bank_json, review_markdown FROM interview_preparations WHERE id = ?", (review_id,)
        ).fetchone()
        log = conn.execute(
            "SELECT action_type, status, summary, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    plan = loads(review["prep_plan_json"], {})
    assert plan["three_day_plan"][0] == "补齐 RAG 评测表达"
    assert "Agent 工具调用" in review["question_bank_json"]
    assert review["review_markdown"] == "# 强化准备\n\n只基于已有项目事实。"
    assert log["action_type"] == "interview_prep_enhance"
    assert log["status"] == "已增强"
    assert loads(log["decision_json"], {})["user_triggered"] is True
    assert "test@example.com" not in log["summary"]
    assert "13800138000" not in log["decision_json"]

    page = client.get(f"/interviews/{review_id}")
    assert page.status_code == 200
    assert "智能强化准备" in page.text


def test_interview_prep_enhancement_without_model_preserves_existing_materials(tmp_path, monkeypatch):
    review_id = create_review(tmp_path, monkeypatch)

    from app import main
    from app.db import connect, loads

    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    client = TestClient(main.app)

    response = client.post(f"/interviews/{review_id}/enhance", follow_redirects=False)

    assert response.status_code == 303
    with connect() as conn:
        review = conn.execute(
            "SELECT prep_plan_json, question_bank_json, review_markdown FROM interview_preparations WHERE id = ?", (review_id,)
        ).fetchone()
        log = conn.execute(
            "SELECT status, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert loads(review["prep_plan_json"], {})["three_day_plan"] == ["旧 3 天计划"]
    assert loads(review["question_bank_json"], []) == ["旧问题：RAG 如何评测？"]
    assert review["review_markdown"] == "# 旧面试准备"
    assert log["status"] == "未配置"
    assert loads(log["decision_json"], {})["model_called"] is False
