"""Tests for the seed_kb CLI.

We import the module under test directly and patch ``requests`` so no
real HTTP traffic flows.  The CLI must:

* iterate every file in ``config/kb_seed/placeholders/`` and POST it
* tolerate 409 (idempotency) as a "skip" not a failure
* delete only placeholder-tagged documents on ``--wipe-placeholders``
* report status with a placeholder/real split
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_SCRIPT = REPO_ROOT / "scripts" / "seed_kb.py"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_kb_under_test", SEED_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["seed_kb_under_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seed_module():
    return _load_seed_module()


def _fake_response(status_code: int, json_body: dict | None = None, text: str = ""):
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_body or {}
    mock.text = text
    return mock


def test_parse_frontmatter_extracts_title_and_tags(seed_module) -> None:
    text = (
        "---\n"
        'title: "Stunt-SOP (Platzhalter)"\n'
        "source: PLACEHOLDER\n"
        'tags: ["placeholder", "stunt"]\n'
        "ttl_hours: 8760\n"
        "---\n"
        "\nBody text here.\n"
    )
    meta, body = seed_module._parse_frontmatter(text)
    assert meta["title"] == "Stunt-SOP (Platzhalter)"
    assert meta["tags"] == ["placeholder", "stunt"]
    assert meta["ttl_hours"] == 8760
    assert body.lstrip().startswith("Body text here.")


def test_parse_frontmatter_noop_without_block(seed_module) -> None:
    meta, body = seed_module._parse_frontmatter("No front matter.\n")
    assert meta == {}
    assert body == "No front matter.\n"


def test_seed_placeholders_uploads_all_files_and_counts_skips(seed_module) -> None:
    responses = [
        _fake_response(201, {"doc_id": "11111111-1111-1111-1111-111111111111"}),
        _fake_response(409),  # duplicate, must be treated as skip not fail
        _fake_response(201, {"doc_id": "22222222-2222-2222-2222-222222222222"}),
        _fake_response(201, {"doc_id": "33333333-3333-3333-3333-333333333333"}),
        _fake_response(201, {"doc_id": "44444444-4444-4444-4444-444444444444"}),
        _fake_response(201, {"doc_id": "55555555-5555-5555-5555-555555555555"}),
    ]
    post_mock = MagicMock(side_effect=responses)

    with patch.object(seed_module.requests, "post", post_mock):
        rc = seed_module.cmd_seed_placeholders("http://test", "eki_test")

    assert rc == 0
    # We have six placeholder files in the repo; CLI must POST each
    assert post_mock.call_count == 6
    # The first call's data must include placeholder tag and PLACEHOLDER source
    sent = post_mock.call_args_list[0]
    sent_data = sent.kwargs.get("data") or sent[1]["data"]
    assert "placeholder" in sent_data["tags"]
    assert sent_data["source"] == "PLACEHOLDER"


def test_wipe_placeholders_calls_tag_delete(seed_module) -> None:
    delete_mock = MagicMock(
        return_value=_fake_response(200, {"deleted": True, "tag": "placeholder", "count": 6})
    )
    with patch.object(seed_module.requests, "delete", delete_mock):
        rc = seed_module.cmd_wipe_placeholders("http://test", "eki_test")

    assert rc == 0
    delete_mock.assert_called_once()
    params = delete_mock.call_args.kwargs.get("params") or {}
    assert params.get("tag") == "placeholder"


def test_status_groups_placeholders_and_real(seed_module, capsys) -> None:
    get_mock = MagicMock(
        return_value=_fake_response(
            200,
            {
                "total_returned": 3,
                "documents": [
                    {
                        "doc_id": "a",
                        "title": "Stunt-SOP",
                        "tags": ["placeholder", "stunt"],
                        "chunk_count": 4,
                    },
                    {
                        "doc_id": "b",
                        "title": "Fire",
                        "tags": ["placeholder", "fire"],
                        "chunk_count": 3,
                    },
                    {"doc_id": "c", "title": "Real Doc", "tags": ["official"], "chunk_count": 7},
                ],
            },
        )
    )
    with patch.object(seed_module.requests, "get", get_mock):
        rc = seed_module.cmd_status("http://test", "eki_test")

    assert rc == 0
    out = capsys.readouterr().out
    assert "Placeholder docs: 2" in out
    assert "Real docs:        1" in out
    assert "Total chunks:     14" in out


def test_reseed_wipes_then_ingests_real(seed_module, tmp_path) -> None:
    delete_mock = MagicMock(
        return_value=_fake_response(200, {"deleted": True, "tag": "placeholder", "count": 6})
    )
    post_mock = MagicMock(return_value=_fake_response(201, {"doc_id": "abc"}))

    fake_real_dir = tmp_path / "real"
    fake_real_dir.mkdir()
    (fake_real_dir / "official.md").write_text("# real safety doc\n", encoding="utf-8")

    with (
        patch.object(seed_module, "REAL_DIR", fake_real_dir),
        patch.object(seed_module.requests, "delete", delete_mock),
        patch.object(seed_module.requests, "post", post_mock),
    ):
        rc = seed_module.cmd_reseed("http://test", "eki_test")

    assert rc == 0
    delete_mock.assert_called_once()
    post_mock.assert_called_once()
    sent_data = post_mock.call_args.kwargs["data"]
    assert sent_data["source"] == "UPLOAD"
    assert "placeholder" not in sent_data["tags"]


# ---------------------------------------------------------------------------
# 1.0.1: /v1/kb/* is admin-only -> a non-admin key gets 403 with a clear hint
# ---------------------------------------------------------------------------

_FORBIDDEN_BODY = '{"error": "Forbidden", "message": "Admin API key required for this endpoint"}'


def test_upload_with_non_admin_key_fails_with_admin_hint(seed_module, capsys) -> None:
    post_mock = MagicMock(return_value=_fake_response(403, text=_FORBIDDEN_BODY))
    with patch.object(seed_module.requests, "post", post_mock):
        rc = seed_module.cmd_seed_placeholders("http://test", "eki_not_admin")

    assert rc == 1
    out = capsys.readouterr().out
    assert "HTTP 403" in out
    assert "requires an admin API key" in out
    assert "create_api_key.py --insert --admin" in out
    assert "Summary: 0 created, 0 skipped, 6 failed" in out


@pytest.mark.parametrize(
    ("command", "method"), [("cmd_wipe_placeholders", "delete"), ("cmd_status", "get")]
)
def test_wipe_and_status_with_non_admin_key_explain_403(
    seed_module, capsys, command, method
) -> None:
    mock = MagicMock(return_value=_fake_response(403, text=_FORBIDDEN_BODY))
    with patch.object(seed_module.requests, method, mock):
        rc = getattr(seed_module, command)("http://test", "eki_not_admin")

    assert rc == 1
    out = capsys.readouterr().out
    assert out.startswith("Error: HTTP 403")
    assert "requires an admin API key" in out


def test_other_http_errors_have_no_admin_hint(seed_module, capsys) -> None:
    delete_mock = MagicMock(return_value=_fake_response(503, text="down"))
    with patch.object(seed_module.requests, "delete", delete_mock):
        assert seed_module.cmd_wipe_placeholders("http://test", "eki_admin") == 1
    out = capsys.readouterr().out
    assert out.strip() == "Error: HTTP 503: down"


def test_missing_key_mentions_the_admin_requirement(seed_module, capsys, monkeypatch) -> None:
    monkeypatch.delenv("EKI_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["seed_kb.py", "--status"])
    assert seed_module.main() == 2
    assert "admin API key" in capsys.readouterr().out
