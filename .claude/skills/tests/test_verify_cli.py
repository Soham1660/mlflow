import argparse
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "verify_cli", ROOT / ".claude/skills/verify/scripts/cli.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PR = {"state": "open", "draft": False, "requested_reviewers": [], "requested_teams": []}


@pytest.mark.parametrize(
    ("event", "pr", "expected"),
    [
        ("APPROVE", PR, "true"),
        ("COMMENT", PR, "false"),
        ("APPROVE", PR | {"draft": True}, "false"),
        ("APPROVE", PR | {"requested_reviewers": [{"login": "alice"}]}, "false"),
        ("APPROVE", PR | {"requested_teams": [{"slug": "core"}]}, "false"),
    ],
)
def test_publish_enables_auto_merge_only_for_unblocked_approvals(
    cli: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    pr: dict[str, Any],
    expected: str,
) -> None:
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"event": event, "body": "ok", "comments": []}))
    output = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    args = argparse.Namespace(
        repo="mlflow/mlflow",
        pr_number="1",
        head_sha="a" * 40,
        run_url="https://example.com/run",
        payload_path=payload,
        dry_run=False,
    )
    posted = {"html_url": "https://example.com/review"}
    with mock.patch.object(cli, "api", side_effect=[pr, posted]) as mock_api:
        assert cli.publish(args) == 0

    assert mock_api.call_count == 2
    assert mock_api.call_args.kwargs["data"]["event"] == event
    assert output.read_text() == f"auto_merge={expected}\n"
