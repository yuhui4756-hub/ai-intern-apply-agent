from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import ROOT_DIR, TASK_TYPES, looks_masked, mask_secret, set_env_value, suggest_api_key_env
from .db import connect, dumps, get_setting, init_db, loads, set_setting, utc_now
from .services.analyzer import (
    build_interview_review,
    clean_extracted,
    generate_message,
    rule_extract_jd,
    score_job,
)
from .services.job_fetcher import fetch_job_from_url
from .services.job_searcher import capture_current_search_page, open_manual_search_in_edge, search_jobs_with_browser
from .services.llm import OpenAICompatibleClient, client_for_task
from .services.research import search_company
from .services.resume import read_resume_text


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="简历投递 Agent", lifespan=lifespan)
templates = Jinja2Templates(directory=str(ROOT_DIR / "app" / "templates"))
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "app" / "static")), name="static")

BULK_IMPORT_LIMIT = 20
BATCH_SEPARATOR_RE = re.compile(r"(?m)^\s*(?:-{3,}|={3,}|#{3,}|岗位\s*\d+[:：]?)\s*$")
BATCH_START_MARKERS = [
    re.compile(r"(?m)^\s*公司名称\s*[:：]"),
    re.compile(r"(?m)^\s*(?:岗位名称|职位名称)\s*[:：]"),
]


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def redirect_with_notice(path: str, message: str, notice_type: str = "info") -> RedirectResponse:
    query = urlencode({"notice": message, "notice_type": notice_type})
    separator = "&" if "?" in path else "?"
    return redirect(f"{path}{separator}{query}")


def task_label(task_type: str) -> str:
    return dict(TASK_TYPES).get(task_type, task_type)


def parse_json_fields(job: dict[str, Any]) -> dict[str, Any]:
    job = dict(job)
    job["extracted"] = loads(job.get("extracted_json"), {})
    return job


def analysis_source_label(value: str) -> str:
    return {
        "llm_plus_rules": "LLM + 本地规则",
        "local_rules": "本地规则",
        "failed": "待分析",
    }.get(value or "", value or "本地规则")


def auto_search_depth(scoring: dict[str, Any], extracted: dict[str, Any]) -> str:
    if scoring.get("risk_signals"):
        return "quick"
    if scoring.get("level") == "高匹配" and not extracted.get("company"):
        return "deep"
    if scoring.get("level") == "高匹配" or scoring.get("caution_signals"):
        return "deep"
    if scoring.get("level") == "中匹配":
        return "standard"
    return "quick"


def get_resume_text(resume_id: int | None) -> tuple[dict[str, Any] | None, str]:
    if not resume_id:
        return None, ""
    with connect() as conn:
        resume = conn.execute("SELECT * FROM resume_versions WHERE id = ?", (resume_id,)).fetchone()
        if not resume:
            return None, ""
        resume_dict = {key: resume[key] for key in resume.keys()}
        parsed = resume_dict.get("parsed_text") or ""
        file_path = resume_dict.get("file_path") or ""
        if not parsed and file_path:
            parsed = read_resume_text(file_path)
            if parsed:
                conn.execute(
                    """
                    UPDATE resume_versions
                    SET parsed_text = ?, file_type = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (parsed, Path(file_path).suffix.lower().lstrip("."), utc_now(), resume_id),
                )
                resume_dict["parsed_text"] = parsed
        return resume_dict, parsed


def try_llm_jd_extract(jd_text: str) -> tuple[dict[str, Any] | None, str]:
    client = client_for_task("jd_extract")
    if not client or not client.configured:
        return None, ""
    prompt = (
        "你是求职投递 Agent 的 JD 结构化抽取器。请只输出 JSON，不要输出 Markdown。"
        "字段：title, company, city, salary_text, internship_days, internship_duration, "
        "responsibilities(array), requirements(array), required_skills(array), bonus_skills(array), "
        "risk_signals(array), caution_signals(array)。如果不知道，填空字符串或空数组。"
    )
    try:
        return client.complete_json(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": jd_text[:12000]},
            ]
        ), ""
    except Exception as exc:
        client.log_error(str(exc))
        return None, str(exc)


def try_llm_message(extracted: dict[str, Any], scoring: dict[str, Any], fallback: dict[str, str]) -> dict[str, str]:
    client = client_for_task("message_draft")
    if not client or not client.configured:
        return fallback
    prompt = (
        "请基于岗位信息和候选人优势生成投递话术，风格礼貌正式但有学生真诚感。"
        "不要编造经历。输出 JSON：message, email。"
    )
    try:
        result = client.complete_json(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": dumps({"job": extracted, "scoring": scoring})},
            ]
        )
        return {
            "message": result.get("message") or fallback["message"],
            "email": result.get("email") or fallback["email"],
        }
    except Exception as exc:
        client.log_error(str(exc))
        return fallback


def apply_blacklists(extracted: dict[str, Any], jd_text: str) -> None:
    keywords = get_setting("blacklist_keywords", []) or []
    companies = get_setting("blacklist_companies", []) or []
    risk_signals = list(extracted.get("risk_signals") or [])
    for keyword in keywords:
        keyword = str(keyword).strip()
        if keyword and keyword in jd_text and keyword not in risk_signals:
            risk_signals.append(keyword)
    company = str(extracted.get("company") or "").strip()
    for blacklisted in companies:
        blacklisted = str(blacklisted).strip()
        if blacklisted and company and (blacklisted in company or company in blacklisted):
            signal = f"黑名单公司：{blacklisted}"
            if signal not in risk_signals:
                risk_signals.append(signal)
    extracted["risk_signals"] = risk_signals


def analyze_job_payload(
    jd_text: str,
    resume_id: int | None,
    title: str = "",
    company: str = "",
    city: str = "",
    salary_text: str = "",
    search_depth: str = "auto",
) -> dict[str, Any]:
    _resume, resume_text = get_resume_text(resume_id)
    fallback_extract = rule_extract_jd(
        jd_text,
        fallback_title=title,
        fallback_company=company,
        fallback_city=city,
        fallback_salary=salary_text,
    )
    llm_extract, analysis_error = try_llm_jd_extract(jd_text)
    extracted = clean_extracted({**fallback_extract, **(llm_extract or {})})
    extracted["required_skills"] = list(dict.fromkeys((llm_extract or {}).get("required_skills") or fallback_extract["required_skills"]))
    extracted["risk_signals"] = list(dict.fromkeys((llm_extract or {}).get("risk_signals") or fallback_extract["risk_signals"]))
    extracted["caution_signals"] = list(dict.fromkeys((llm_extract or {}).get("caution_signals") or fallback_extract["caution_signals"]))
    apply_blacklists(extracted, jd_text)

    scoring = score_job(extracted, jd_text, resume_text)
    messages = try_llm_message(extracted, scoring, generate_message(extracted, scoring))
    final_depth = auto_search_depth(scoring, extracted) if search_depth == "auto" else search_depth
    source = "llm_plus_rules" if llm_extract else "local_rules"
    if not jd_text:
        source = "failed"
        analysis_error = "JD 文本为空"

    return {
        "extracted": extracted,
        "scoring": scoring,
        "messages": messages,
        "search_depth": final_depth,
        "analysis_error": analysis_error,
        "analysis_source": source,
    }


def insert_company_research(conn: Any, job_id: int, company: str, title: str, city: str, search_depth: str) -> None:
    if not company:
        return
    now = utc_now()
    for result in search_company(company, title, city, search_depth):
        conn.execute(
            """
            INSERT INTO company_research (
                job_id, company, query, source_title, source_url,
                summary, risk_signals_json, searched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                company,
                " ".join([company, title, city]).strip(),
                result.title,
                result.url,
                result.summary,
                dumps([]),
                now,
            ),
        )


def split_batch_jds(raw_text: str) -> list[str]:
    text = (raw_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    explicit_parts = [part.strip() for part in BATCH_SEPARATOR_RE.split(text) if part.strip()]
    if len(explicit_parts) > 1:
        return explicit_parts

    for marker in BATCH_START_MARKERS:
        starts = [match.start() for match in marker.finditer(text)]
        if len(starts) > 1:
            boundaries = [0] + starts[1:] + [len(text)]
            return [text[boundaries[index] : boundaries[index + 1]].strip() for index in range(len(boundaries) - 1)]

    return [text]


def infer_platform_from_url(url: str) -> str:
    lowered = url.lower()
    if "zhipin.com" in lowered:
        return "Boss 直聘"
    if "liepin.com" in lowered:
        return "猎聘"
    if "shixiseng.com" in lowered:
        return "实习僧"
    if "zhaopin.com" in lowered:
        return "智联招聘"
    if "51job.com" in lowered or "we.51job.com" in lowered:
        return "前程无忧"
    return "岗位链接"


def fetch_mode_label(value: str) -> str:
    return {
        "auto": "自动",
        "http": "普通网页",
        "browser": "浏览器渲染",
    }.get(value or "", value or "自动")


def browser_channel_label(value: str) -> str:
    return {
        "msedge": "Microsoft Edge",
        "edge": "Microsoft Edge",
        "chromium": "Chromium",
    }.get(value or "", value or "Microsoft Edge")


def initial_job_status(jd_text: str, scoring: dict[str, Any]) -> str:
    if not jd_text:
        return "待分析"
    if scoring["recommendation"] in {"必投", "可冲"}:
        return "待确认"
    return "已归档"


def create_job_record(
    conn: Any,
    *,
    jd_text: str,
    resume_id: int | None,
    platform: str = "",
    source_url: str = "",
    title: str = "",
    company: str = "",
    city: str = "",
    salary_text: str = "",
    search_depth: str = "auto",
) -> tuple[int, dict[str, Any]]:
    analysis = analyze_job_payload(
        jd_text,
        resume_id,
        title=title,
        company=company,
        city=city,
        salary_text=salary_text,
        search_depth=search_depth,
    )
    extracted = analysis["extracted"]
    scoring = analysis["scoring"]
    messages = analysis["messages"]
    final_depth = analysis["search_depth"]
    now = utc_now()
    status = initial_job_status(jd_text, scoring)
    cursor = conn.execute(
        """
        INSERT INTO job_postings (
            platform, source_url, title, company, city, salary_text,
            internship_days, internship_duration, jd_text, extracted_json,
            selected_resume_id, match_score, match_level, risk_level,
            recommendation, status, skip_reason, generated_message,
            generated_email, analysis_error, analysis_source, search_depth, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            platform,
            source_url,
            extracted.get("title") or "",
            extracted.get("company") or company,
            extracted.get("city") or "",
            extracted.get("salary_text") or "",
            extracted.get("internship_days") or "",
            extracted.get("internship_duration") or "",
            jd_text,
            dumps({"extracted": extracted, "scoring": scoring}),
            resume_id,
            scoring["score"] if jd_text else 0,
            scoring["level"] if jd_text else "",
            scoring["risk_level"] if jd_text else "",
            scoring["recommendation"] if jd_text else "",
            status,
            scoring["skip_reason"] if jd_text else "",
            messages["message"] if jd_text else "",
            messages["email"] if jd_text else "",
            analysis["analysis_error"],
            analysis["analysis_source"],
            final_depth,
            now,
            now,
        ),
    )
    job_id = cursor.lastrowid
    company_name = extracted.get("company") or company
    if jd_text and company_name:
        insert_company_research(conn, job_id, company_name, extracted.get("title") or "", extracted.get("city") or "", final_depth)
    return job_id, analysis


def token_stats() -> dict[str, Any]:
    with connect() as conn:
        today = conn.execute(
            """
            SELECT COALESCE(SUM(total_tokens), 0) AS total, COALESCE(SUM(estimated_cost), 0) AS cost
            FROM model_call_logs
            WHERE date(created_at) = date('now')
            """
        ).fetchone()
        all_time = conn.execute(
            """
            SELECT COALESCE(SUM(total_tokens), 0) AS total, COALESCE(SUM(estimated_cost), 0) AS cost
            FROM model_call_logs
            """
        ).fetchone()
    return {
        "today_total": int(today["total"] or 0),
        "today_cost": float(today["cost"] or 0),
        "all_total": int(all_time["total"] or 0),
        "all_cost": float(all_time["cost"] or 0),
    }


def save_search_result(result: Any) -> int:
    now = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO job_search_runs (
                platform, keyword, city, search_url, browser_channel, status, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.platform,
                result.keyword,
                result.city,
                result.search_url,
                result.browser_channel,
                "完成" if result.candidates else "无结果",
                result.note,
                now,
            ),
        )
        run_id = cursor.lastrowid
        for candidate in result.candidates:
            conn.execute(
                """
                INSERT INTO job_candidates (
                    search_run_id, platform, title, company, city, source_url,
                    summary, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    result.platform,
                    candidate.title,
                    candidate.company,
                    candidate.city,
                    candidate.source_url,
                    candidate.summary,
                    "候选",
                    now,
                    now,
                ),
            )
    return int(run_id)


def save_search_failure(platform: str, keyword: str, city: str, browser_channel: str, error_message: str) -> int:
    now = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO job_search_runs (
                platform, keyword, city, search_url, browser_channel, status, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                platform,
                keyword,
                city,
                "",
                browser_channel or "msedge",
                "失败",
                error_message[:500],
                now,
            ),
        )
    return int(cursor.lastrowid)


@app.get("/")
def dashboard(request: Request) -> Any:
    with connect() as conn:
        counts = {
            row["status"]: row["count"]
            for row in conn.execute("SELECT status, COUNT(*) AS count FROM job_postings GROUP BY status").fetchall()
        }
        recent_jobs = [
            parse_json_fields({key: row[key] for key in row.keys()})
            for row in conn.execute("SELECT * FROM job_postings ORDER BY created_at DESC LIMIT 6").fetchall()
        ]
        resumes = conn.execute("SELECT COUNT(*) AS count FROM resume_versions").fetchone()["count"]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"counts": counts, "recent_jobs": recent_jobs, "resumes": resumes, "token_stats": token_stats()},
    )


@app.get("/resumes")
def resumes_page(request: Request) -> Any:
    with connect() as conn:
        profile = conn.execute("SELECT * FROM candidate_profile ORDER BY id LIMIT 1").fetchone()
        resumes = conn.execute("SELECT * FROM resume_versions ORDER BY id").fetchall()
    return templates.TemplateResponse(
        request,
        "resumes.html",
        {
            "profile": {key: profile[key] for key in profile.keys()} if profile else {},
            "resumes": [{key: row[key] for key in row.keys()} for row in resumes],
            "loads": loads,
        },
    )


@app.post("/resumes/profile")
async def update_profile(request: Request) -> RedirectResponse:
    form = await request.form()
    now = utc_now()
    with connect() as conn:
        profile = conn.execute("SELECT id FROM candidate_profile ORDER BY id LIMIT 1").fetchone()
        values = (
            str(form.get("name") or ""),
            str(form.get("education") or ""),
            str(form.get("github_url") or ""),
            str(form.get("demo_url") or ""),
            dumps([item.strip() for item in str(form.get("target_roles") or "").splitlines() if item.strip()]),
            dumps([item.strip() for item in str(form.get("skills") or "").splitlines() if item.strip()]),
            now,
        )
        if profile:
            conn.execute(
                """
                UPDATE candidate_profile
                SET name = ?, education = ?, github_url = ?, demo_url = ?,
                    target_roles = ?, skills_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (*values, profile["id"]),
            )
    return redirect("/resumes")


@app.post("/resumes/{resume_id}")
async def update_resume(resume_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    file_path = str(form.get("file_path") or "").strip()
    parsed_text = str(form.get("parsed_text") or "")
    if file_path and not parsed_text:
        parsed_text = read_resume_text(file_path)
    with connect() as conn:
        conn.execute(
            """
            UPDATE resume_versions
            SET name = ?, target_role = ?, file_path = ?, parsed_text = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                str(form.get("name") or ""),
                str(form.get("target_role") or ""),
                file_path,
                parsed_text,
                utc_now(),
                resume_id,
            ),
        )
    return redirect("/resumes")


@app.get("/jobs")
def jobs_page(request: Request, status: str = "") -> Any:
    params: tuple[Any, ...] = ()
    query = "SELECT * FROM job_postings"
    if status:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY created_at DESC"
    with connect() as conn:
        jobs = [parse_json_fields({key: row[key] for key in row.keys()}) for row in conn.execute(query, params).fetchall()]
        recommendation_counts = {
            row["recommendation"] or "未分析": row["count"]
            for row in conn.execute(
                """
                SELECT recommendation, COUNT(*) AS count
                FROM job_postings
                GROUP BY recommendation
                """
            ).fetchall()
        }
        status_counts = {
            row["status"]: row["count"]
            for row in conn.execute("SELECT status, COUNT(*) AS count FROM job_postings GROUP BY status").fetchall()
        }
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "jobs": jobs,
            "status_filter": status,
            "recommendation_counts": recommendation_counts,
            "status_counts": status_counts,
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
        },
    )


@app.get("/jobs/new")
def new_job_page(request: Request) -> Any:
    with connect() as conn:
        resumes = conn.execute("SELECT * FROM resume_versions ORDER BY is_default DESC, id").fetchall()
    jd_client = client_for_task("jd_extract")
    return templates.TemplateResponse(
        request,
        "job_form.html",
        {
            "resumes": [{key: row[key] for key in row.keys()} for row in resumes],
            "model_configured": bool(jd_client and jd_client.configured),
            "bulk_import_limit": BULK_IMPORT_LIMIT,
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
        },
    )


@app.get("/searches")
def searches_page(request: Request) -> Any:
    with connect() as conn:
        runs = conn.execute(
            """
            SELECT r.*,
                   COUNT(c.id) AS candidate_count,
                   SUM(CASE WHEN c.status = '已导入' THEN 1 ELSE 0 END) AS imported_count
            FROM job_search_runs r
            LEFT JOIN job_candidates c ON c.search_run_id = r.id
            GROUP BY r.id
            ORDER BY r.created_at DESC
            LIMIT 30
            """
        ).fetchall()
    return templates.TemplateResponse(
        request,
        "searches.html",
        {
            "runs": [{key: row[key] for key in row.keys()} for row in runs],
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
            "browser_channel_label": browser_channel_label,
        },
    )


@app.post("/searches")
async def create_search_run(request: Request) -> RedirectResponse:
    form = await request.form()
    platform = str(form.get("platform") or "Boss 直聘").strip()
    keyword = str(form.get("keyword") or "").strip()
    city = str(form.get("city") or "").strip()
    browser_channel = str(form.get("browser_channel") or "msedge").strip()
    if not keyword:
        return redirect_with_notice("/searches", "请填写搜索关键词。", "error")

    try:
        result = search_jobs_with_browser(platform, keyword, city, browser_channel=browser_channel)
    except Exception as exc:
        run_id = save_search_failure(platform, keyword, city, browser_channel, f"自动采集失败：{str(exc)}")
        return redirect_with_notice(f"/searches/{run_id}", f"搜索采集失败：{str(exc)[:160]}", "error")

    run_id = save_search_result(result)
    return redirect_with_notice(f"/searches/{run_id}", f"已采集 {len(result.candidates)} 个候选岗位。", "success")


@app.post("/searches/open-manual")
async def open_manual_search(request: Request) -> RedirectResponse:
    form = await request.form()
    platform = str(form.get("platform") or "Boss 直聘").strip()
    keyword = str(form.get("keyword") or "").strip()
    city = str(form.get("city") or "").strip()
    if not keyword:
        return redirect_with_notice("/searches", "请填写搜索关键词。", "error")
    try:
        search_url = open_manual_search_in_edge(platform, keyword, city)
    except Exception as exc:
        run_id = save_search_failure(platform, keyword, city, "msedge", f"打开 Edge 失败：{str(exc)}")
        return redirect_with_notice(f"/searches/{run_id}", f"打开 Edge 失败：{str(exc)[:160]}", "error")
    return redirect_with_notice("/searches", f"已打开 Edge 搜索页：{search_url}。完成登录或筛选后，点击“采集当前 Edge 页面”。", "success")


@app.post("/searches/capture-current")
async def capture_current_search(request: Request) -> RedirectResponse:
    form = await request.form()
    platform = str(form.get("platform") or "Boss 直聘").strip()
    keyword = str(form.get("keyword") or "").strip()
    city = str(form.get("city") or "").strip()
    browser_channel = str(form.get("browser_channel") or "msedge").strip()
    if not keyword:
        return redirect_with_notice("/searches", "请填写搜索关键词。", "error")
    try:
        result = capture_current_search_page(platform, keyword, city, browser_channel=browser_channel)
    except Exception as exc:
        run_id = save_search_failure(platform, keyword, city, browser_channel, f"当前页面采集失败：{str(exc)}")
        return redirect_with_notice(f"/searches/{run_id}", f"当前页面采集失败：{str(exc)[:160]}", "error")
    run_id = save_search_result(result)
    return redirect_with_notice(f"/searches/{run_id}", f"已从当前 Edge 页面采集 {len(result.candidates)} 个候选岗位。", "success")


@app.get("/searches/{run_id}")
def search_detail(run_id: int, request: Request) -> Any:
    with connect() as conn:
        run = conn.execute("SELECT * FROM job_search_runs WHERE id = ?", (run_id,)).fetchone()
        candidates = conn.execute(
            "SELECT * FROM job_candidates WHERE search_run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        resumes = conn.execute("SELECT * FROM resume_versions ORDER BY is_default DESC, id").fetchall()
    if not run:
        return redirect("/searches")
    return templates.TemplateResponse(
        request,
        "search_detail.html",
        {
            "run": {key: run[key] for key in run.keys()},
            "candidates": [{key: row[key] for key in row.keys()} for row in candidates],
            "resumes": [{key: row[key] for key in row.keys()} for row in resumes],
            "browser_channel_label": browser_channel_label,
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
        },
    )


@app.post("/candidates/{candidate_id}/import")
async def import_candidate(candidate_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    resume_id_raw = str(form.get("selected_resume_id") or "")
    resume_id = int(resume_id_raw) if resume_id_raw.isdigit() else None
    requested_depth = str(form.get("search_depth") or "auto")
    fetch_mode = str(form.get("fetch_mode") or "auto")
    browser_channel = str(form.get("browser_channel") or "msedge")
    with connect() as conn:
        row = conn.execute(
            """
            SELECT c.*, r.browser_channel AS run_browser_channel
            FROM job_candidates c
            LEFT JOIN job_search_runs r ON r.id = c.search_run_id
            WHERE c.id = ?
            """,
            (candidate_id,),
        ).fetchone()
    if not row:
        return redirect_with_notice("/searches", "没有找到候选岗位。", "error")

    candidate = {key: row[key] for key in row.keys()}
    run_id = int(candidate["search_run_id"])
    channel = browser_channel or candidate.get("run_browser_channel") or "msedge"
    try:
        fetched = fetch_job_from_url(candidate["source_url"], fetch_mode=fetch_mode, browser_channel=channel)
        with connect() as conn:
            job_id, _analysis = create_job_record(
                conn,
                jd_text=fetched.text,
                resume_id=resume_id,
                platform=candidate.get("platform") or infer_platform_from_url(fetched.final_url),
                source_url=fetched.final_url,
                title=fetched.title or candidate.get("title") or "",
                company=candidate.get("company") or "",
                city=candidate.get("city") or "",
                search_depth=requested_depth,
            )
            now = utc_now()
            conn.execute(
                "UPDATE job_candidates SET job_id = ?, status = ?, error_message = '', updated_at = ? WHERE id = ?",
                (job_id, "已导入", now, candidate_id),
            )
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
                (job_id, "搜索候选导入", f"从搜索候选 {candidate.get('source_url')} 导入岗位详情。", now),
            )
        return redirect_with_notice(f"/jobs/{job_id}", f"已从候选岗位导入并通过{fetch_mode_label(fetched.fetch_mode)}完成分析。", "success")
    except Exception as exc:
        with connect() as conn:
            conn.execute(
                "UPDATE job_candidates SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
                ("导入失败", str(exc)[:500], utc_now(), candidate_id),
            )
        return redirect_with_notice(f"/searches/{run_id}", f"候选岗位导入失败：{str(exc)[:160]}", "error")


@app.post("/jobs/analyze")
async def analyze_job(request: Request) -> RedirectResponse:
    form = await request.form()
    jd_text = str(form.get("jd_text") or "").strip()
    resume_id_raw = str(form.get("selected_resume_id") or "")
    resume_id = int(resume_id_raw) if resume_id_raw.isdigit() else None
    requested_depth = str(form.get("search_depth") or "auto")
    with connect() as conn:
        job_id, _analysis = create_job_record(
            conn,
            jd_text=jd_text,
            resume_id=resume_id,
            platform=str(form.get("platform") or ""),
            source_url=str(form.get("source_url") or ""),
            title=str(form.get("title") or ""),
            company=str(form.get("company") or ""),
            city=str(form.get("city") or ""),
            salary_text=str(form.get("salary_text") or ""),
            search_depth=requested_depth,
        )
    return redirect(f"/jobs/{job_id}")


@app.post("/jobs/bulk-analyze")
async def bulk_analyze_jobs(request: Request) -> RedirectResponse:
    form = await request.form()
    jd_items = split_batch_jds(str(form.get("batch_jd_text") or ""))
    if not jd_items:
        return redirect_with_notice("/jobs/new", "没有找到可导入的 JD。", "error")

    truncated = len(jd_items) > BULK_IMPORT_LIMIT
    jd_items = jd_items[:BULK_IMPORT_LIMIT]
    resume_id_raw = str(form.get("selected_resume_id") or "")
    resume_id = int(resume_id_raw) if resume_id_raw.isdigit() else None
    requested_depth = str(form.get("search_depth") or "auto")
    platform = str(form.get("platform") or "").strip()
    created: list[dict[str, Any]] = []

    with connect() as conn:
        for jd_text in jd_items:
            job_id, analysis = create_job_record(
                conn,
                jd_text=jd_text,
                resume_id=resume_id,
                platform=platform,
                search_depth=requested_depth,
            )
            created.append(
                {
                    "id": job_id,
                    "recommendation": analysis["scoring"].get("recommendation") or "未分析",
                }
            )

    counts = {label: sum(1 for item in created if item["recommendation"] == label) for label in ["必投", "可冲", "跳过"]}
    message = f"已导入 {len(created)} 条岗位：必投 {counts['必投']}，可冲 {counts['可冲']}，跳过 {counts['跳过']}。"
    if truncated:
        message += f" 单次最多处理 {BULK_IMPORT_LIMIT} 条，其余内容未导入。"
    return redirect_with_notice("/jobs", message, "success")


@app.post("/jobs/import-url")
async def import_job_url(request: Request) -> RedirectResponse:
    form = await request.form()
    source_url = str(form.get("source_url") or "").strip()
    resume_id_raw = str(form.get("selected_resume_id") or "")
    resume_id = int(resume_id_raw) if resume_id_raw.isdigit() else None
    requested_depth = str(form.get("search_depth") or "auto")
    fetch_mode = str(form.get("fetch_mode") or "auto")
    browser_channel = str(form.get("browser_channel") or "msedge")
    platform = str(form.get("platform") or "").strip()
    try:
        fetched = fetch_job_from_url(source_url, fetch_mode=fetch_mode, browser_channel=browser_channel)
    except Exception as exc:
        return redirect_with_notice("/jobs/new", f"链接导入失败：{str(exc)[:160]}", "error")

    with connect() as conn:
        job_id, _analysis = create_job_record(
            conn,
            jd_text=fetched.text,
            resume_id=resume_id,
            platform=platform or infer_platform_from_url(fetched.final_url),
            source_url=fetched.final_url,
            title=fetched.title,
            search_depth=requested_depth,
        )
        conn.execute(
            "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
            (
                job_id,
                "链接导入",
                f"使用{fetch_mode_label(fetched.fetch_mode)}从 {fetched.final_url} 抓取页面文本并生成岗位记录。{fetched.note}",
                utc_now(),
            ),
        )
    return redirect_with_notice(f"/jobs/{job_id}", f"已通过{fetch_mode_label(fetched.fetch_mode)}导入并完成分析。", "success")


@app.post("/jobs/bulk-status")
async def bulk_update_jobs(request: Request) -> RedirectResponse:
    form = await request.form()
    job_ids = [int(value) for value in form.getlist("job_ids") if str(value).isdigit()]
    status = str(form.get("status") or "").strip()
    allowed_statuses = {"待确认", "待投递", "已投递", "已沟通", "待面试", "面试准备中", "已归档"}
    if not job_ids:
        return redirect_with_notice("/jobs", "请先选择岗位。", "error")
    if status not in allowed_statuses:
        return redirect_with_notice("/jobs", "状态无效，未更新。", "error")

    now = utc_now()
    placeholders = ",".join("?" for _ in job_ids)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT id, title, company FROM job_postings WHERE id IN ({placeholders})",
            tuple(job_ids),
        ).fetchall()
        if not rows:
            return redirect_with_notice("/jobs", "没有找到可更新的岗位。", "error")
        valid_ids = [int(row["id"]) for row in rows]
        valid_placeholders = ",".join("?" for _ in valid_ids)
        conn.execute(
            f"UPDATE job_postings SET status = ?, updated_at = ? WHERE id IN ({valid_placeholders})",
            (status, now, *valid_ids),
        )
        for row in rows:
            title = " - ".join(item for item in [row["company"], row["title"]] if item) or f"岗位 {row['id']}"
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
                (row["id"], "批量状态更新", f"{title} 已标记为 {status}。", now),
            )
    return redirect_with_notice("/jobs", f"已更新 {len(valid_ids)} 条岗位为「{status}」。", "success")


@app.get("/jobs/{job_id}")
def job_detail(job_id: int, request: Request) -> Any:
    with connect() as conn:
        job = conn.execute("SELECT * FROM job_postings WHERE id = ?", (job_id,)).fetchone()
        research = conn.execute("SELECT * FROM company_research WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
        events = conn.execute("SELECT * FROM application_events WHERE job_id = ? ORDER BY id DESC", (job_id,)).fetchall()
    if not job:
        return redirect("/jobs")
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "job": parse_json_fields({key: job[key] for key in job.keys()}),
            "research": [{key: row[key] for key in row.keys()} for row in research],
            "events": [{key: row[key] for key in row.keys()} for row in events],
            "analysis_source_label": analysis_source_label,
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
        },
    )


@app.post("/jobs/{job_id}/status")
async def update_job_status(job_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    now = utc_now()
    status = str(form.get("status") or "待确认")
    note = str(form.get("note") or "").strip()
    skip_reason = str(form.get("skip_reason") or "").strip()
    with connect() as conn:
        conn.execute(
            "UPDATE job_postings SET status = ?, skip_reason = ?, updated_at = ? WHERE id = ?",
            (status, skip_reason, now, job_id),
        )
        if note:
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
                (job_id, "状态更新", note, now),
            )
    return redirect(f"/jobs/{job_id}")


@app.post("/jobs/{job_id}/reanalyze")
async def reanalyze_job(job_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    with connect() as conn:
        row = conn.execute("SELECT * FROM job_postings WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return redirect("/jobs")

    job = {key: row[key] for key in row.keys()}
    resume_id = job.get("selected_resume_id")
    requested_depth = str(form.get("search_depth") or job.get("search_depth") or "auto")
    analysis = analyze_job_payload(
        job.get("jd_text") or "",
        int(resume_id) if resume_id else None,
        title=str(form.get("title") or job.get("title") or ""),
        company=str(form.get("company") or job.get("company") or ""),
        city=str(form.get("city") or job.get("city") or ""),
        salary_text=str(form.get("salary_text") or job.get("salary_text") or ""),
        search_depth=requested_depth,
    )
    extracted = analysis["extracted"]
    scoring = analysis["scoring"]
    messages = analysis["messages"]
    status = job.get("status") or "待确认"
    if status in {"待分析", "待确认", "已归档"}:
        status = "待确认" if scoring["recommendation"] in {"必投", "可冲"} else "已归档"

    now = utc_now()
    with connect() as conn:
        conn.execute("DELETE FROM company_research WHERE job_id = ?", (job_id,))
        conn.execute(
            """
            UPDATE job_postings
            SET title = ?, company = ?, city = ?, salary_text = ?,
                internship_days = ?, internship_duration = ?, extracted_json = ?,
                match_score = ?, match_level = ?, risk_level = ?, recommendation = ?,
                status = ?, skip_reason = ?, generated_message = ?, generated_email = ?,
                analysis_error = ?, analysis_source = ?, search_depth = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                extracted.get("title") or "",
                extracted.get("company") or "",
                extracted.get("city") or "",
                extracted.get("salary_text") or "",
                extracted.get("internship_days") or "",
                extracted.get("internship_duration") or "",
                dumps({"extracted": extracted, "scoring": scoring}),
                scoring["score"],
                scoring["level"],
                scoring["risk_level"],
                scoring["recommendation"],
                status,
                scoring["skip_reason"],
                messages["message"],
                messages["email"],
                analysis["analysis_error"],
                analysis["analysis_source"],
                analysis["search_depth"],
                now,
                job_id,
            ),
        )
        conn.execute(
            "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
            (job_id, "重新分析", f"使用{analysis_source_label(analysis['analysis_source'])}刷新岗位分析。", now),
        )
        insert_company_research(
            conn,
            job_id,
            extracted.get("company") or "",
            extracted.get("title") or "",
            extracted.get("city") or "",
            analysis["search_depth"],
        )
    return redirect_with_notice(f"/jobs/{job_id}", "已重新分析岗位。", "success")


@app.get("/interviews")
def interviews_page(request: Request) -> Any:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT i.*, j.title AS job_title, j.company AS company
            FROM interview_preparations i
            LEFT JOIN job_postings j ON j.id = i.job_id
            ORDER BY i.created_at DESC
            """
        ).fetchall()
        jobs = conn.execute("SELECT id, title, company FROM job_postings ORDER BY created_at DESC").fetchall()
    return templates.TemplateResponse(
        request,
        "interviews.html",
        {
            "interviews": [{key: row[key] for key in row.keys()} for row in rows],
            "jobs": [{key: row[key] for key in row.keys()} for row in jobs],
        },
    )


@app.post("/interviews")
async def create_interview_review(request: Request) -> RedirectResponse:
    form = await request.form()
    job_id_raw = str(form.get("job_id") or "")
    job_id = int(job_id_raw) if job_id_raw.isdigit() else None
    source_text = str(form.get("source_text") or "")
    job: dict[str, Any] | None = None
    if job_id:
        with connect() as conn:
            row = conn.execute("SELECT * FROM job_postings WHERE id = ?", (job_id,)).fetchone()
            if row:
                job = parse_json_fields({key: row[key] for key in row.keys()})

    review = build_interview_review(job, source_text)
    client = client_for_task("interview_review")
    if client and client.configured and source_text:
        try:
            llm_markdown = client.complete_text(
                [
                    {"role": "system", "content": "请生成中文面试复盘 Markdown，聚焦没答好问题、补强建议和下一轮模拟题。"},
                    {"role": "user", "content": dumps({"job": job or {}, "transcript": source_text[:12000]})},
                ]
            )
            if llm_markdown.strip():
                review["markdown"] = llm_markdown.strip()
        except Exception as exc:
            client.log_error(str(exc))

    now = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO interview_preparations (
                job_id, source_text, prep_plan_json, question_bank_json,
                review_markdown, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, source_text, dumps(review["plan"]), dumps(review["questions"]), review["markdown"], now, now),
        )
        review_id = cursor.lastrowid
    return redirect(f"/interviews/{review_id}")


@app.get("/interviews/{review_id}")
def interview_detail(review_id: int, request: Request) -> Any:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT i.*, j.title AS job_title, j.company AS company
            FROM interview_preparations i
            LEFT JOIN job_postings j ON j.id = i.job_id
            WHERE i.id = ?
            """,
            (review_id,),
        ).fetchone()
    if not row:
        return redirect("/interviews")
    review = {key: row[key] for key in row.keys()}
    review["plan"] = loads(review.get("prep_plan_json"), {})
    review["questions"] = loads(review.get("question_bank_json"), [])
    return templates.TemplateResponse(request, "interview_detail.html", {"review": review})


@app.get("/interviews/{review_id}/download")
def download_interview_review(review_id: int) -> Response:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT i.review_markdown, COALESCE(j.title, 'interview-review') AS title
            FROM interview_preparations i
            LEFT JOIN job_postings j ON j.id = i.job_id
            WHERE i.id = ?
            """,
            (review_id,),
        ).fetchone()
    if not row:
        return Response("Not found", status_code=404)
    filename = safe_filename(row["title"] or "interview-review") + ".md"
    return Response(
        row["review_markdown"] or "",
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def safe_filename(value: str) -> str:
    keep = []
    for char in value:
        if char.isalnum() or char in {"-", "_"}:
            keep.append(char)
        elif char.isspace():
            keep.append("-")
    return "".join(keep).strip("-")[:80] or "interview-review"


@app.get("/settings")
def settings_page(request: Request) -> Any:
    with connect() as conn:
        profile_rows = conn.execute("SELECT * FROM model_profiles ORDER BY is_default DESC, id").fetchall()
        routes = conn.execute(
            """
            SELECT r.*, p.name AS profile_name
            FROM model_routes r
            LEFT JOIN model_profiles p ON p.id = r.profile_id
            ORDER BY r.id
            """
        ).fetchall()
        logs = conn.execute("SELECT * FROM model_call_logs ORDER BY created_at DESC LIMIT 20").fetchall()
    profiles = []
    env_usage: dict[str, list[str]] = {}
    for row in profile_rows:
        key_name = row["api_key_env"] or suggest_api_key_env(row["name"], row["base_url"])
        env_usage.setdefault(key_name, []).append(row["name"])
    for row in profile_rows:
        profile = {key: row[key] for key in row.keys()}
        key_name = profile.get("api_key_env") or suggest_api_key_env(profile.get("name") or "", profile.get("base_url") or "")
        profile["api_key_env"] = key_name
        profile["api_key_mask"] = mask_secret(os.environ.get(key_name, ""))
        profile["api_key_env_shared_with"] = [name for name in env_usage.get(key_name, []) if name != profile.get("name")]
        profile["api_key_env_suggestion"] = suggest_api_key_env(profile.get("name") or "", profile.get("base_url") or "")
        profiles.append(profile)
    env_path = ROOT_DIR / ".env"
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "profiles": profiles,
            "routes": [{key: row[key] for key in row.keys()} for row in routes],
            "logs": [{key: row[key] for key in row.keys()} for row in logs],
            "task_label": task_label,
            "token_stats": token_stats(),
            "env_exists": env_path.exists(),
            "env_example": (ROOT_DIR / ".env.example").read_text(encoding="utf-8"),
            "blacklist_companies": "\n".join(get_setting("blacklist_companies", []) or []),
            "blacklist_keywords": "\n".join(get_setting("blacklist_keywords", []) or []),
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
            "default_api_key_env": "OPENAI_COMPATIBLE_API_KEY",
        },
    )


@app.post("/settings/model-profiles")
async def upsert_model_profile(request: Request) -> RedirectResponse:
    form = await request.form()
    profile_id = str(form.get("profile_id") or "")
    action = str(form.get("action") or "save")
    name = str(form.get("name") or "未命名配置")
    base_url = str(form.get("base_url") or "")
    api_key_env = str(form.get("api_key_env") or "").strip() or suggest_api_key_env(name, base_url)
    api_key_value = str(form.get("api_key") or "").strip()
    if api_key_value and not looks_masked(api_key_value):
        set_env_value(api_key_env, api_key_value)

    values = (
        name,
        base_url,
        api_key_env,
        str(form.get("model") or ""),
        float(form.get("temperature") or 0.2),
        float(form.get("input_cost_per_million") or 0),
        float(form.get("output_cost_per_million") or 0),
        1 if form.get("is_default") == "on" else 0,
        utc_now(),
    )
    with connect() as conn:
        if values[7]:
            conn.execute("UPDATE model_profiles SET is_default = 0")
        if profile_id.isdigit():
            conn.execute(
                """
                UPDATE model_profiles
                SET name = ?, base_url = ?, api_key_env = ?, model = ?, temperature = ?,
                    input_cost_per_million = ?, output_cost_per_million = ?,
                    is_default = ?, updated_at = ?
                WHERE id = ?
                """,
                (*values, int(profile_id)),
            )
        else:
            conn.execute(
                """
                INSERT INTO model_profiles (
                    name, base_url, api_key_env, model, temperature,
                    input_cost_per_million, output_cost_per_million, is_default,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*values, values[-1]),
            )
        shared_rows = conn.execute(
            "SELECT name FROM model_profiles WHERE api_key_env = ? AND name != ? ORDER BY id",
            (api_key_env, name),
        ).fetchall()
    shared_names = [row["name"] for row in shared_rows]
    save_message = f"模型配置已保存。API Key 变量名：{api_key_env}"
    if shared_names:
        save_message += f"；该变量也被 {', '.join(shared_names)} 使用，会共用同一个 Key。"

    if action == "test":
        profile = {
            "name": values[0],
            "base_url": values[1],
            "api_key_env": values[2],
            "model": values[3],
            "temperature": values[4],
            "input_cost_per_million": values[5],
            "output_cost_per_million": values[6],
        }
        client = OpenAICompatibleClient(profile, "settings_test")
        try:
            client.complete_text(
                [
                    {"role": "system", "content": "你是模型连通性测试助手。"},
                    {"role": "user", "content": "请只回复 OK。"},
                ]
            )
            return redirect_with_notice("/settings", f"模型连接成功：{values[0]} / {values[3]}", "success")
        except Exception as exc:
            client.log_error(str(exc))
            return redirect_with_notice("/settings", f"模型连接失败：{str(exc)[:160]}", "error")

    return redirect_with_notice("/settings", save_message, "success")


@app.post("/settings/blacklists")
async def update_blacklists(request: Request) -> RedirectResponse:
    form = await request.form()
    companies = [item.strip() for item in str(form.get("blacklist_companies") or "").splitlines() if item.strip()]
    keywords = [item.strip() for item in str(form.get("blacklist_keywords") or "").splitlines() if item.strip()]
    set_setting("blacklist_companies", companies)
    set_setting("blacklist_keywords", keywords)
    return redirect("/settings")


@app.post("/settings/routes")
async def update_routes(request: Request) -> RedirectResponse:
    form = await request.form()
    now = utc_now()
    with connect() as conn:
        for task_type, _label in TASK_TYPES:
            raw = str(form.get(task_type) or "")
            profile_id = int(raw) if raw.isdigit() else None
            conn.execute(
                "UPDATE model_routes SET profile_id = ?, updated_at = ? WHERE task_type = ?",
                (profile_id, now, task_type),
            )
    return redirect("/settings")
