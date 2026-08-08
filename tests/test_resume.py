from __future__ import annotations

import sys
from types import ModuleType

from app.services.resume import read_resume_text


def test_read_resume_text_extracts_pdf_with_pypdf(tmp_path, monkeypatch):
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-test")

    class FakePage:
        def extract_text(self):
            return "Python / FastAPI\nRAG project"

    class FakeReader:
        is_encrypted = False
        pages = [FakePage()]

        def __init__(self, path):
            assert path == str(resume_path)

    fake_pypdf = ModuleType("pypdf")
    fake_pypdf.PdfReader = FakeReader
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

    assert read_resume_text(str(resume_path)) == "Python / FastAPI\nRAG project"


def test_read_resume_text_returns_empty_for_locked_pdf(tmp_path, monkeypatch):
    resume_path = tmp_path / "locked.pdf"
    resume_path.write_bytes(b"%PDF-test")

    class FakeReader:
        is_encrypted = True
        pages = []

        def __init__(self, _path):
            pass

        def decrypt(self, _password):
            return 0

    fake_pypdf = ModuleType("pypdf")
    fake_pypdf.PdfReader = FakeReader
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

    assert read_resume_text(str(resume_path)) == ""
