import pytest

from app.services.ai_code_edits import CodeEditError, apply_model_code_edits


def test_structured_code_edits_apply_only_exact_unique_blocks():
    source = "alpha = 1\nbeta = 2\ngamma = 3\n"
    candidate, plan = apply_model_code_edits(
        source,
        {
            "operations": [
                {"old_text": "beta = 2", "new_text": "beta = 20"},
                {"old_text": "gamma = 3", "new_text": "gamma = 30"},
            ]
        },
    )

    assert candidate == "alpha = 1\nbeta = 20\ngamma = 30\n"
    assert plan["executor"] == "model_patch"
    assert plan["operation_count"] == 2
    assert plan["operations"][0]["startLine"] == 2


def test_structured_code_edits_reject_missing_ambiguous_and_overlapping_anchors():
    with pytest.raises(CodeEditError, match="edit_anchor_not_found"):
        apply_model_code_edits("alpha\n", {"operations": [{"old_text": "beta", "new_text": "gamma"}]})

    with pytest.raises(CodeEditError, match="edit_anchor_ambiguous"):
        apply_model_code_edits("alpha\nalpha\n", {"operations": [{"old_text": "alpha", "new_text": "beta"}]})

    with pytest.raises(CodeEditError, match="overlapping_edit_operations"):
        apply_model_code_edits(
            "alpha beta gamma",
            {"operations": [
                {"old_text": "alpha beta", "new_text": "one"},
                {"old_text": "beta gamma", "new_text": "two"},
            ]},
        )


def test_structured_code_edits_accept_json_fences_but_not_no_ops():
    candidate, _plan = apply_model_code_edits(
        "value = 1\n",
        '```json\n{"operations":[{"old_text":"value = 1","new_text":"value = 2"}]}\n```',
    )
    assert candidate == "value = 2\n"

    with pytest.raises(CodeEditError, match="invalid_edit_operations"):
        apply_model_code_edits("value = 1\n", '{"operations":[]}')
