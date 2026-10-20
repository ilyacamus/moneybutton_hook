import random

from conftest import criteria

from hookbot.core import knowledge, learning


def _seed(store, workspace="w", n=40, seed=1, noise=3.0):
    rng = random.Random(seed)
    for i in range(n):
        crit = {c: rng.randint(2, 9) for c in knowledge.criterion_ids()}
        hold = 20 + 4 * crit["curiosity"] + 3 * crit["emotion"] + rng.gauss(0, noise)
        triggers = ["curiosity_gap"] if crit["curiosity"] > 6 else ["question_hook"]
        hook_id = store.add_hook(workspace, f"hook {i}", criteria=crit, triggers=triggers)
        store.add_result(hook_id, workspace, hold_3s=max(0, min(100, hold)))


def test_prior_until_enough_results(store):
    _seed(store, n=learning.MIN_RESULTS - 1)
    cal = learning.calibrate(store, "w")
    assert cal.scope == "prior" and not cal.can_predict
    assert cal.weights == knowledge.prior_weights()
    assert cal.predict_hold(criteria(9)) is None


def test_calibration_learns_what_drives_hold(store):
    _seed(store)
    cal = learning.calibrate(store, "w")
    assert cal.scope == "workspace" and cal.n == 40
    top_two = sorted(cal.weights, key=cal.weights.get, reverse=True)[:2]
    assert set(top_two) == {"curiosity", "emotion"}
    assert abs(sum(cal.weights.values()) - 1) < 1e-3
    assert cal.mae is not None and cal.mae < 6
    strong = cal.predict_hold(criteria(5, curiosity=9, emotion=9))
    weak = cal.predict_hold(criteria(5, curiosity=2, emotion=2))
    assert strong > weak + 20


def test_score_is_0_to_100_and_ignores_missing(store):
    cal = learning.prior_calibration()
    assert cal.score(criteria(10)) == 100
    assert cal.score(criteria(0)) == 0
    assert cal.score({"curiosity": 8}) == 80
    assert cal.score({}) is None


def test_global_model_for_new_users(store):
    _seed(store, workspace="veteran")
    store.update_profile("veteran", share_data=True)
    assert learning.calibrate(store, "newbie").scope == "global"
    store.update_profile("veteran", share_data=False)
    assert learning.calibrate(store, "newbie").scope == "prior"


def test_trigger_lift(store):
    _seed(store)
    lifts = {item["trigger_id"]: item["lift"] for item in learning.trigger_lift(store.training_rows("w"))}
    assert lifts["curiosity_gap"] > 5 > lifts["question_hook"]


def test_examples_do_not_overlap():
    rows = [{"id": i, "text": str(i), "hold_3s": float(i)} for i in range(5)]
    best, worst = learning.select_examples(rows)
    assert {b["id"] for b in best}.isdisjoint({w["id"] for w in worst})
    assert best[0]["id"] == 4 and worst[0]["id"] == 0


def test_context_prompt_mentions_learning(store):
    _seed(store)
    store.save_insight("w", {"summary": "Цифры работают", "do_more": [{"rule": "Ставь сумму"}], "do_less": []}, 40)
    text = learning.build_context(store, "w").to_prompt()
    assert "n=40" in text and "Цифры работают" in text and "Ставь сумму" in text
    assert "`curiosity_gap`" in text
