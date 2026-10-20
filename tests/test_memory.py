import pytest

from conftest import criteria


def test_hook_roundtrip_and_workspace_isolation(store):
    hook_id = store.add_hook("a", "  Хук  ", criteria=criteria(7), triggers=["specificity", "specificity"])
    hook = store.get_hook(hook_id)
    assert hook["text"] == "Хук"
    assert hook["triggers"] == ["specificity"]
    assert hook["criteria"]["curiosity"] == 7
    assert store.get_hook(hook_id, "b") is None


def test_criteria_are_clamped(store):
    hook_id = store.add_hook("a", "x", criteria={"curiosity": 14, "clarity": -3, "emotion": "7.6", "bad": "n/a"})
    assert store.get_hook(hook_id)["criteria"] == {"curiosity": 10, "clarity": 0, "emotion": 8}


def test_result_normalizes_skip_and_hold(store):
    hook_id = store.add_hook("a", "x")
    assert store.add_result(hook_id, "a", skip_rate=38)["hold_3s"] == 62
    assert store.add_result(hook_id, "a", hold_3s=70, views=1200.4)["skip_rate"] == 30
    assert store.add_result(hook_id, "a", views=10).get("hold_3s") is None


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"skip_rate": 120}, ValueError),
        ({"views": -1}, ValueError),
        ({}, ValueError),
        ({"bogus": 1}, ValueError),
    ],
)
def test_result_validation(store, kwargs, error):
    hook_id = store.add_hook("a", "x")
    with pytest.raises(error):
        store.add_result(hook_id, "a", **kwargs)


def test_result_requires_own_hook(store):
    hook_id = store.add_hook("a", "x")
    with pytest.raises(LookupError):
        store.add_result(hook_id, "b", skip_rate=10)


def test_training_rows_use_latest_result_with_hold(store):
    hook_id = store.add_hook("a", "x", criteria=criteria())
    store.add_result(hook_id, "a", skip_rate=50)
    store.add_result(hook_id, "a", skip_rate=20)
    store.add_result(hook_id, "a", views=999)  # no hold: ignored for training
    rows = store.training_rows("a")
    assert len(rows) == 1 and rows[0]["hold_3s"] == 80


def test_shared_rows_require_opt_in(store):
    for ws in ("a", "b"):
        store.add_result(store.add_hook(ws, "x"), ws, skip_rate=40)
    store.update_profile("b", share_data=True)
    assert {r["workspace"] for r in store.training_rows(None, shared_only=True)} == {"b"}


def test_hooks_without_results_filters_sources(store):
    evaluated = store.add_hook("a", "x", source="evaluated")
    store.add_hook("a", "y", source="generated")
    done = store.add_hook("a", "z", source="evaluated")
    store.add_result(done, "a", skip_rate=10)
    assert [h["id"] for h in store.hooks_without_results("a", sources=["evaluated"])] == [evaluated]
    assert len(store.hooks_without_results("a")) == 2


def test_reflection_due_counts_new_results(store):
    for i in range(5):
        store.add_result(store.add_hook("a", f"h{i}"), "a", skip_rate=40)
    assert store.reflection_due("a", every=5)
    store.save_insight("a", {"summary": "s"}, n_results=5)
    assert not store.reflection_due("a", every=5)
    assert store.latest_insight("a")["content"] == {"summary": "s"}


def test_delete_workspace(store):
    hook_id = store.add_hook("a", "x")
    child = store.add_hook("a", "y", parent_id=hook_id)
    store.add_result(child, "a", skip_rate=10)
    store.save_insight("a", {}, 1)
    keep = store.add_hook("b", "x")
    store.delete_workspace("a")
    assert store.recent_hooks("a") == [] and store.latest_insight("a") is None
    assert store.get_hook(keep) is not None
