from hookbot import prompts
from hookbot.core import knowledge


def test_ids_are_unique():
    for ids in (
        knowledge.trigger_ids(),
        knowledge.anti_pattern_ids(),
        knowledge.formula_ids(),
        knowledge.action_ids(),
        knowledge.criterion_ids(),
    ):
        assert len(ids) == len(set(ids))


def test_cross_references_point_to_existing_triggers():
    triggers = set(knowledge.trigger_ids())
    for item in (*knowledge.formulas(), *knowledge.actions()):
        assert set(item["trigger_ids"]) <= triggers, item["id"]


def test_rubric_weights_sum_to_one():
    assert abs(sum(knowledge.prior_weights().values()) - 1) < 1e-9


def test_every_trigger_group_and_action_category_is_known():
    assert {t["group"] for t in knowledge.triggers()} <= set(knowledge.GROUP_TITLES)
    assert {a["category"] for a in knowledge.actions()} == set(knowledge.ACTION_CATEGORIES)


def test_system_prompt_contains_whole_knowledge_base():
    for ids in (knowledge.trigger_ids(), knowledge.anti_pattern_ids(), knowledge.action_ids(), knowledge.criterion_ids()):
        for i in ids:
            assert f"`{i}`" in prompts.SYSTEM_PROMPT
    assert "skip rate" in prompts.SYSTEM_PROMPT


def test_system_prompt_is_stable_for_caching():
    # No timestamps or per-request data: identical bytes on every request.
    assert prompts.SYSTEM_PROMPT == prompts.ROLE + "\n\n# БАЗА ЗНАНИЙ\n\n" + knowledge.render_knowledge_base()
