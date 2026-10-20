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


def test_every_cited_reference_exists():
    ids = {r["id"] for r in knowledge.references()}
    assert len(ids) == len(knowledge.references())
    for item in (*knowledge.triggers(), *knowledge.anti_patterns()):
        assert item["evidence"]["level"] in knowledge.EVIDENCE_LEVELS, item["id"]
        assert set(item["evidence"]["refs"]) <= ids, item["id"]
    for c in knowledge.rubric():
        assert set(c["refs"]) <= ids
    assert {r["topic"] for r in knowledge.references()} <= set(knowledge.REFERENCE_TOPICS)


def test_zeigarnik_is_not_presented_as_established():
    curiosity = next(t for t in knowledge.triggers() if t["id"] == "curiosity_gap")
    assert "ghibellini_meier2025" in curiosity["evidence"]["refs"]
    assert "не подтвержд" in curiosity["mechanism"]


def test_short_citations():
    assert knowledge.short_cite("itti_koch2001") == "Itti & Koch (2001)"
    assert knowledge.short_cite("robertson2023") == "Robertson et al. (2023)"
    assert knowledge.short_cite("loewenstein1994") == "Loewenstein (1994)"


def test_literature_rendered_but_not_in_prompt():
    lit = knowledge.render_literature()
    assert "10.1038/s41562-023-01538-4" in lit
    assert "# Научная база" not in prompts.SYSTEM_PROMPT
    assert "Доказательность A" in prompts.SYSTEM_PROMPT


def test_action_references_exist():
    ids = {r["id"] for r in knowledge.references()}
    for a in knowledge.actions():
        assert set(a.get("refs", [])) <= ids, a["id"]
