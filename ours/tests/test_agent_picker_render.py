"""The legacy OMS route stays available when the API team is offered to new cards."""

import pytest

from symphony.workflow import build_service_config, load_workflow
from test_api_board_render import configure, kit as kit, render


@pytest.mark.parametrize("with_picker", [True, False])
def test_render_opts_in_to_api_team_picker_without_restricting_dispatch(kit, with_picker):
    configure(kit)
    path = kit.board / "project.toml"
    text = path.read_text() + "new_cards = true\n"
    if with_picker:
        text = text.replace("[agent]", '[agent]\npicker_kinds = ["api-team"]', 1)
    path.write_text(text)
    result = render(kit)
    assert result.returncode == 0, result.stderr
    cfg = build_service_config(load_workflow(kit.board / "WORKFLOW.md"))
    assert cfg.agent.new_issue_kind == "api-team"
    assert cfg.agent.picker_kinds == (("api-team",) if with_picker else ())
    assert {"api-team", "claude", "codex"} <= cfg.board_agent_kinds()
    assert cfg.agent.kind_for_state("Todo") == "claude"
    assert cfg.agent.kind_for_state("Todo", "codex") == "codex"


def test_render_rejects_unavailable_picker_before_writing_workflow(kit):
    path = kit.board / "project.toml"
    path.write_text(path.read_text().replace("[agent]", '[agent]\npicker_kinds = ["missing"]', 1))
    workflow = kit.board / "WORKFLOW.md"
    original = workflow.read_bytes()
    result = render(kit)
    assert result.returncode == 1 and "picker_kinds" in result.stderr
    assert workflow.read_bytes() == original
