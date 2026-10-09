"""Refused book TOC responses, with synthetic inputs."""
from unittest.mock import Mock

import pytest

from app.services import book_analyzer, gemini_rest
from app.services.finish_policy import GeminiResponseRejected


@pytest.mark.parametrize("source", ["pdf", "photos"])
@pytest.mark.parametrize("reason", [0, 3, 4, 99])
def test_refused_toc_never_reaches_theme_builder(monkeypatch, source, reason):
    monkeypatch.setattr(book_analyzer, "_render_pages", lambda *a: [b"synthetic"])
    monkeypatch.setattr(gemini_rest, "call_book_analysis", lambda *a, **kw: (
        '[{"title":"1. Synthetic lesson","page":1}]', reason))
    builder = Mock(return_value=["theme"])
    monkeypatch.setattr(book_analyzer, "themes_from_toc_entries", builder)
    with pytest.raises(GeminiResponseRejected):
        if source == "pdf":
            book_analyzer.themes_from_gemini(b"synthetic", [1])
        else:
            book_analyzer.themes_from_toc_photos(b"synthetic", [b"photo"])
    builder.assert_not_called()


@pytest.mark.parametrize("source", ["pdf", "photos"])
@pytest.mark.parametrize("reason", [1, 2])
def test_usable_toc_still_builds_themes(monkeypatch, source, reason):
    monkeypatch.setattr(book_analyzer, "_render_pages", lambda *a: [b"synthetic"])
    monkeypatch.setattr(gemini_rest, "call_book_analysis", lambda *a, **kw: (
        '[{"title":"1. Synthetic lesson","page":1}]', reason))
    monkeypatch.setattr(book_analyzer, "themes_from_toc_entries", lambda *a, **kw: ["theme"])
    result = (book_analyzer.themes_from_gemini(b"synthetic", [1]) if source == "pdf"
              else book_analyzer.themes_from_toc_photos(b"synthetic", [b"photo"]))
    assert result == ["theme"]
