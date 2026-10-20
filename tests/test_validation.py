from conftest import criteria

from hookbot.core import validation


def test_forecast_validity_uses_only_honest_pairs(store):
    for i, (pred, actual) in enumerate([(None, 50), (55, 52), (70, 68), (40, 45), (60, 61), (30, 35)]):
        hook = store.add_hook("w", f"h{i}", criteria=criteria(5), predicted_hold=pred)
        store.add_result(hook, "w", hold_3s=actual)
    v = validation.forecast_validity(store, "w")
    assert v["n"] == 5  # the first result has no history for the baseline
    assert v["mae"] < v["baseline_mae"] and v["skill"] > 0.5
    assert v["spearman"] > 0.9


def test_forecast_validity_needs_data(store):
    assert validation.forecast_validity(store, "w")["mae"] is None


def test_reliability_bands():
    consistent = [[criteria(v), criteria(v), criteria(v)] for v in (2, 5, 8, 4)]
    noisy = [[criteria(2), criteria(9), criteria(5)], [criteria(8), criteria(1), criteria(5)],
             [criteria(5), criteria(5), criteria(1)]]
    good = validation.reliability(consistent)
    bad = validation.reliability(noisy)
    assert good["score_icc"] > 0.95 and good["score_band"] == "отличная"
    assert bad["score_icc"] < 0.5 and bad["criteria"]["curiosity"]["band"] == "плохая"


def test_report(store):
    validation.save_reliability(store, validation.reliability([[criteria(3), criteria(3)], [criteria(7), criteria(7)]]))
    r = validation.report(store, "w")
    assert r["reliability"]["score_icc"] == 1.0 and r["corpus"] is None
