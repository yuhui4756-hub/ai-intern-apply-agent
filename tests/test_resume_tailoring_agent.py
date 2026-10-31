from __future__ import annotations

import json
import asyncio

from app.db import connect, dumps, init_db, utc_now
from app.services import desktop_agent


RESUME_MARKER = "RESUME_FACT_PRIVATE_4729"
JOB_MARKER = "JD_REQUIREMENT_PRIVATE_8351"
EMAIL = "private.resume@example.com"
PHONE = "13800138000"
ID_NUMBER = "11010519900101123X"
ADDRESS = "滨江路 42 号"


def _seed_job_and_resume():
    now = utc_now()
    with connect() as conn:
        profile = conn.execute("SELECT * FROM candidate_profile ORDER BY id LIMIT 1").fetchone()
        resume = conn.execute("SELECT * FROM resume_versions ORDER BY id LIMIT 1").fetchone()
        resume_id = int(resume["id"])
        conn.execute(
            """
            UPDATE candidate_profile
            SET education = ?, skills_json = ?, projects_json = ?
            WHERE id = ?
            """,
            (
                "智能科学与技术本科在读",
                dumps(["Python", "FastAPI", "RAG"]),
                dumps(
                    [
                        {
                            "name": "本地知识库",
                            "highlights": ["构建混合检索流程", "加入检索评测与来源 trace"],
                        }
                    ]
                ),
                int(profile["id"]),
            ),
        )
        resume_text = (
            f"项目经历：{RESUME_MARKER}，使用 Python 和 FastAPI 构建 RAG 知识库，加入检索评测。"
            f"\n姓名：测试候选人\n邮箱：{EMAIL}\n电话：{PHONE}\n"
            f"住址：{ADDRESS}\n身份证号：{ID_NUMBER}\n"
        ) + ("项目实现了分块、混合检索、评测和来源追踪。" * 12)
        conn.execute(
            "UPDATE resume_versions SET parsed_text = ?, profile_id = ?, name = ? WHERE id = ?",
            (resume_text, int(profile["id"]), f"候选人简历-{PHONE}", resume_id),
        )
        job_id = int(
            conn.execute(
                """
                INSERT INTO job_postings (
                    platform, title, company, city, jd_text, extracted_json,
                    selected_resume_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Boss 直聘",
                    "RAG 应用开发实习生",
                    "证据测试科技",
                    "杭州",
                    f"参与 RAG 应用开发；要求 Python、FastAPI。{JOB_MARKER}",
                    json.dumps(
                        {
                            "extracted": {
                                "required_skills": ["Python", "FastAPI", "RAG"],
                                "responsibilities": ["参与检索应用开发"],
                                "requirements": ["熟悉 Python 服务开发"],
                            },
                            "scoring": {
                                "matched_skills": ["Python", "RAG"],
                                "missing_skills": ["向量数据库实战"],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    resume_id,
                    now,
                    now,
                ),
            ).lastrowid
        )
    return job_id, resume_id, resume_text


def _patch_agent_environment(monkeypatch, main, fake_client):
    monkeypatch.setattr(
        desktop_agent,
        "load_client",
        lambda _profile_id: (
            fake_client,
            {"id": 1, "name": "简历定制测试", "model": "tailoring-test-model"},
        ),
    )
    monkeypatch.setattr(main, "controlled_edge_status", lambda: {"status": "未连接"})
    monkeypatch.setattr(main, "communication_policy", lambda: {"mode": "draft"})
    monkeypatch.setattr(main, "automation_control", lambda: {"status_label": "运行中"})


def test_explicit_resume_tailoring_uses_evidence_but_keeps_raw_content_out_of_tool_audit(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "resume-tailoring-agent.sqlite3"))
    from app import main

    init_db()
    job_id, resume_id, resume_text = _seed_job_and_resume()

    class FakeClient:
        configured = True
        profile = {"id": 1, "name": "简历定制测试"}
        model = "tailoring-test-model"

        def __init__(self):
            self.calls = []

        def complete_json(self, messages):
            self.calls.append(messages)
            if len(self.calls) == 1:
                return {
                    "plan": "读取当前岗位的简历和 JD 证据。",
                    "tool_calls": [{"name": "tailor_resume", "arguments": {"job_id": job_id}}],
                    "response": "",
                    "task_summary": "准备岗位定制建议。",
                }
            return {
                "plan": f"证据已核对，勿在回复中复述联系方式 {EMAIL} {PHONE}。",
                "tool_calls": [],
                "response": (
                    "简历定制建议（草稿）\n"
                    "原文：参与 RAG 知识库开发\n"
                    "建议稿：使用 Python 和 FastAPI 构建 RAG 知识库，并加入检索评测。\n"
                    "简历依据：现有项目经历明确写有 Python、FastAPI 和检索评测。\n"
                    "JD 对应点：岗位要求 Python 服务开发和 RAG 应用。\n"
                    f"误输出的个人信息：姓名：测试候选人；邮箱：{EMAIL}；电话：{PHONE}；"
                    f"地址：{ADDRESS}；身份证号：{ID_NUMBER}"
                ),
                "task_summary": (
                    f"已形成简历修改草稿，姓名：测试候选人；联系方式 {EMAIL} {PHONE}；"
                    f"地址：{ADDRESS}；身份证号：{ID_NUMBER}"
                ),
            }

    fake_client = FakeClient()
    _patch_agent_environment(monkeypatch, main, fake_client)
    session = desktop_agent.create_session("岗位简历建议")
    with connect() as conn:
        conn.execute("UPDATE agent_sessions SET active_job_id = ? WHERE id = ?", (job_id, session["id"]))

    conversation = asyncio.run(
        desktop_agent.process_turn(
            int(session["id"]),
            "针对当前岗位优化我的简历",
        )
    )

    assert len(fake_client.calls) == 2
    model_context = str(fake_client.calls[1][1]["content"])
    assert RESUME_MARKER in model_context
    assert JOB_MARKER in model_context
    assert "[个人信息已省略]" in model_context
    assert EMAIL not in model_context
    assert PHONE not in model_context
    assert ID_NUMBER not in model_context
    assert ADDRESS not in model_context
    assert "测试候选人" not in model_context
    assert "候选人简历" not in model_context

    assistant = conversation["messages"][-1]
    assert "建议稿：" in assistant["content"]
    assert EMAIL not in assistant["content"]
    assert PHONE not in assistant["content"]
    assert ID_NUMBER not in assistant["content"]
    assert ADDRESS not in assistant["content"]
    assert "测试候选人" not in assistant["content"]
    assert "\n建议稿：" in assistant["content"]
    assert PHONE not in conversation["session"]["summary"]

    with connect() as conn:
        tool = conn.execute(
            "SELECT tool_name, permission_level, result_json FROM agent_tool_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        stored_resume = conn.execute(
            "SELECT parsed_text FROM resume_versions WHERE id = ?", (resume_id,)
        ).fetchone()["parsed_text"]
        stored_job = conn.execute(
            "SELECT selected_resume_id FROM job_postings WHERE id = ?", (job_id,)
        ).fetchone()
        preparation_count = conn.execute(
            "SELECT COUNT(*) FROM application_preparations WHERE job_id = ?", (job_id,)
        ).fetchone()[0]

    audit = json.loads(tool["result_json"])
    assert tool["tool_name"] == "tailor_resume"
    assert tool["permission_level"] == "简历敏感只读"
    audit_text = json.dumps(audit, ensure_ascii=False)
    assert RESUME_MARKER not in audit_text
    assert JOB_MARKER not in audit_text
    assert EMAIL not in audit_text
    assert PHONE not in audit_text
    assert ID_NUMBER not in audit_text
    assert "候选人简历" not in audit_text
    assert audit["source_text_stored_in_tool_audit"] is False
    assert audit["file_written"] is False
    assert stored_resume == resume_text
    assert int(stored_job["selected_resume_id"]) == resume_id
    assert preparation_count == 0


def test_resume_tailoring_tool_refuses_implicit_request_and_wrong_job(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "resume-tailoring-guard.sqlite3"))
    from app import main

    init_db()
    active_job_id, _resume_id, _resume_text = _seed_job_and_resume()
    now = utc_now()
    with connect() as conn:
        other_job_id = int(
            conn.execute(
                "INSERT INTO job_postings (title, company, jd_text, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("另一岗位", "其他公司", JOB_MARKER, now, now),
            ).lastrowid
        )

    class FakeClient:
        configured = True
        profile = {"id": 1, "name": "简历定制测试"}
        model = "tailoring-test-model"

        def __init__(self, requested_job_id):
            self.requested_job_id = requested_job_id
            self.calls = []

        def complete_json(self, messages):
            self.calls.append(messages)
            if len(self.calls) == 1:
                return {
                    "plan": "测试工具误触发保护。",
                    "tool_calls": [
                        {"name": "tailor_resume", "arguments": {"job_id": self.requested_job_id}}
                    ],
                    "response": "",
                    "task_summary": "",
                }
            return {
                "plan": "读取被拒绝。",
                "tool_calls": [],
                "response": "没有读取简历内容。",
                "task_summary": "",
            }

    for request, requested_job_id in (
        ("检查一下简历准备情况", active_job_id),
        ("针对当前岗位优化我的简历", other_job_id),
    ):
        fake_client = FakeClient(requested_job_id)
        _patch_agent_environment(monkeypatch, main, fake_client)
        session = desktop_agent.create_session("简历定制保护")
        with connect() as conn:
            conn.execute(
                "UPDATE agent_sessions SET active_job_id = ? WHERE id = ?",
                (active_job_id, session["id"]),
            )

        conversation = asyncio.run(
            desktop_agent.process_turn(int(session["id"]), request)
        )

        assert len(fake_client.calls) == 2
        assert RESUME_MARKER not in str(fake_client.calls[1][1]["content"])
        assert JOB_MARKER not in str(fake_client.calls[1][1]["content"])
        assert "没有读取简历内容" in conversation["messages"][-1]["content"]

    with connect() as conn:
        logs = conn.execute(
            "SELECT result_json FROM agent_tool_runs WHERE tool_name = 'tailor_resume'"
        ).fetchall()
    assert len(logs) == 2
    assert all(RESUME_MARKER not in row["result_json"] for row in logs)
    assert all(JOB_MARKER not in row["result_json"] for row in logs)


def test_resume_tailoring_requires_a_positive_request_not_a_negated_keyword():
    assert desktop_agent.explicit_resume_tailoring_request("针对当前岗位优化我的简历")
    assert desktop_agent.explicit_resume_tailoring_request("不要只润色，帮我重写简历")
    assert not desktop_agent.explicit_resume_tailoring_request("不要优化简历")
    assert not desktop_agent.explicit_resume_tailoring_request("不做简历优化")
    assert not desktop_agent.explicit_resume_tailoring_request("我不想修改简历")
    assert not desktop_agent.explicit_resume_tailoring_request("简历不需要优化")
    assert not desktop_agent.explicit_resume_tailoring_request("检查一下简历准备情况")
