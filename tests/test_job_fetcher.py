import pytest

from app.services.job_fetcher import ensure_public_http_url, extract_visible_text


def test_extract_visible_text_removes_scripts_and_navigation():
    html = """
    <html>
      <head><title>AI 应用开发实习生 - 测试公司</title></head>
      <body>
        <nav>登录</nav>
        <script>alert("x")</script>
        <h1>AI 应用开发实习生</h1>
        <p>公司名称：杭州链接智能科技有限公司</p>
        <p>要求 Python、RAG、FastAPI，每周 5 天。</p>
      </body>
    </html>
    """

    title, text = extract_visible_text(html)

    assert title == "AI 应用开发实习生 - 测试公司"
    assert "alert" not in text
    assert "登录" not in text
    assert "杭州链接智能科技有限公司" in text


def test_public_url_guard_rejects_local_addresses():
    with pytest.raises(ValueError):
        ensure_public_http_url("http://127.0.0.1:8000/job")

    with pytest.raises(ValueError):
        ensure_public_http_url("file:///tmp/job.html")
