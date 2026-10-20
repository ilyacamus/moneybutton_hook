import random
from datetime import datetime, timedelta, timezone

import pytest

from hookbot.core import corpus as corpus_mod
from hookbot.core import features, knowledge, learning
from hookbot.core.corpus import Corpus, ReelRecord

NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _analysis(viral: bool, rng: random.Random) -> dict:
    criteria = {cid: rng.randint(3, 6) for cid in knowledge.criterion_ids()}
    if viral:
        criteria["curiosity"] = rng.randint(7, 10)
    triggers = ["curiosity_gap", "face_emotion"] if viral else ["question_hook"]
    return {"criteria": criteria, "trigger_ids": triggers, "spoken_hook": "Хук", "on_screen_text": "", "visual": "",
            "face_first_second": viral, "text_first_second": True, "motion_first_second": viral}


def seed_corpus(store, *, creators=12, per_creator=8, seed=1, days_offset=10, spacing=3, niche="фитнес",
                flip_recent=False):
    """Each creator: 2 viral reels (curiosity + face) spread over time, the rest typical."""
    rng = random.Random(seed)
    c = Corpus(store)
    for k in range(creators):
        creator = f"ig:creator{k}_{seed}"
        for i in range(per_creator):
            viral = i in ((1, 6) if k % 2 else (2, 7))
            days = days_offset + i * spacing
            reel_id = c.upsert(ReelRecord(
                source="instagram", external_id=f"{creator}-{i}", creator=creator, niche=niche,
                published_at=_iso(days), views=int((40_000 if viral else 10_000) * rng.uniform(0.85, 1.15)),
            ))
            recent = days <= 30
            makes_viral_look = viral if not (flip_recent and recent) else not viral
            analysis = _analysis(makes_viral_look, rng)
            text = "3 ошибки, из-за которых ты теряешь деньги" if makes_viral_look else "Привет! Сегодня расскажу про бюджет"
            feats = features.extract(text, {"has_audio": True, "first_sound_at_sec": 0.0, "cuts_in_first_3s": [1.0]},
                                     analysis)
            c.save_analysis(reel_id, analysis, feats)
    c.label_all(now=NOW)
    return c


def test_text_features():
    f = features.text_features("Привет! Ты теряешь 400 ₽ каждый день?")
    assert f["greeting_start"] == 1 and f["second_person"] == 1 and f["has_number"] == 1
    assert f["has_question"] == 1 and f["negative_words"] == 1
    v = features.video_features({"has_audio": True, "first_sound_at_sec": None, "cuts_in_first_3s": []})
    assert v == {"cuts_3s": 0.0, "first_sound_at": 3.0}


def test_upsert_keeps_analysis_and_refreshes_metrics(store):
    c = Corpus(store)
    rid = c.upsert(ReelRecord(source="ig", external_id="1", creator="a", views=100))
    c.save_analysis(rid, {"criteria": {}}, {})
    assert c.upsert(ReelRecord(source="ig", external_id="1", creator="a", views=500)) == rid
    reel = c.get(rid)
    assert reel["views"] == 500 and reel["status"] == "studied"


def test_within_creator_labels_and_maturity(store):
    c = Corpus(store)
    for i, views in enumerate([1000, 1100, 900, 1000, 5000, 200]):
        c.upsert(ReelRecord(source="ig", external_id=str(i), creator="a", views=views, published_at=_iso(10)))
    c.upsert(ReelRecord(source="ig", external_id="fresh", creator="a", views=90_000, published_at=_iso(1)))
    c.upsert(ReelRecord(source="ig", external_id="solo", creator="b", views=9_000, baseline_views=3_000))
    c.label_all(now=NOW)
    labels = {r["external_id"]: (r["label"], r["outlier_score"]) for r in map(dict, store.conn.execute("SELECT * FROM reels"))}
    assert labels["4"][0] == "viral" and labels["4"][1] == pytest.approx(5.0)
    assert labels["0"][0] == "typical" and labels["5"][0] == "flop"
    assert labels["fresh"][0] is None  # not mature yet: not judged
    assert labels["solo"] == ("viral", 3.0)  # baseline supplied by the source


def test_queue_pairs_viral_with_same_creator_control(store):
    c = Corpus(store)
    for i, views in enumerate([1000, 1000, 1000, 1000, 6000]):
        c.upsert(ReelRecord(source="ig", external_id=f"a{i}", creator="a", views=views, published_at=_iso(10 + i)))
    c.label_all(now=NOW)
    picked = c.queue(4)
    assert picked[0]["label"] == "viral"
    assert picked[1]["creator"] == "a" and picked[1]["label"] == "typical"
    assert picked[1]["external_id"] == "a3"  # closest publish time


def test_effects_find_planted_signal_and_control_fdr(store):
    seed_corpus(store)
    report = corpus_mod.build_report(Corpus(store), "фитнес", now=NOW)
    effects = {e["variable"]: e for e in report["effects"]}
    assert effects["criterion:curiosity"]["verdict"].startswith("подтверждено")
    assert effects["trigger:curiosity_gap"]["diff"] > 50
    assert effects["feature:greeting_start"]["diff"] < 0 and effects["feature:greeting_start"]["q"] < 0.1
    assert effects["feature:greeting_start"]["pairs"] == 12
    # A variable with no planted signal must not be "confirmed".
    assert effects["criterion:specificity"]["verdict"] == "не подтверждено"
    assert report["validity"]["auc"] > 0.8 and report["validity"]["auc_ci"][0] > 0.6


def test_evidence_synthesis_links_data_to_literature(store):
    seed_corpus(store)
    report = corpus_mod.build_report(Corpus(store), "фитнес", now=NOW)
    by_id = {s["id"]: s for s in report["synthesis"]}
    assert by_id["face_emotion"]["status"] == "доказано: наука + данные"  # literature B + corpus
    assert by_id["slow_intro"]["status"] == "доказано: наука + данные"  # greeting less common in virals
    assert by_id["challenge"]["status"] == "нет данных"


def test_trends_detect_flipped_effect(store):
    seed_corpus(store, days_offset=4, spacing=7, flip_recent=True)
    trends = corpus_mod.trend_report(Corpus(store).studied("фитнес"), now=NOW)
    shifted = {s["variable"] for s in trends["shifts"]}
    assert "criterion:curiosity" in shifted or "trigger:curiosity_gap" in shifted


def test_corpus_weights_become_account_prior(store):
    seed_corpus(store, creators=24)
    report = corpus_mod.build_report(Corpus(store), "фитнес", now=NOW)
    weights = report["weights"]["weights"]
    assert max(weights, key=weights.get) == "curiosity"
    corpus_mod.save_report(store, report)
    store.update_profile("w", niche="фитнес для мам")
    cal = learning.calibrate(store, "w")
    assert cal.prior_source == "corpus" and cal.weights == weights


def test_benchmark_and_exemplars(store):
    seed_corpus(store)
    store.update_profile("w", niche="фитнес")
    feats = features.extract("Привет, сегодня про бюджет")
    bench = corpus_mod.benchmark(store, "фитнес", features=feats, criteria={c: 5 for c in knowledge.criterion_ids()})
    greeting = next(i for i in bench["items"] if i["feature"] == "greeting_start")
    assert greeting["viral_share"] == 0 and greeting["control_share"] == 100
    assert bench["score"]["pct_of_viral"] < 50
    ex = corpus_mod.exemplars(store, "фитнес", ["curiosity_gap"])
    assert ex and "curiosity_gap" in ex[0]["trigger_ids"]


def test_context_mentions_corpus_science(store):
    seed_corpus(store)
    corpus_mod.save_report(store, corpus_mod.build_report(Corpus(store), "фитнес", now=NOW))
    store.update_profile("w", niche="фитнес")
    text = learning.build_context(store, "w").to_prompt()
    assert "корпус залетевших" in text and "доказано: наука + данные" in text


def test_watchlist(store):
    c = Corpus(store)
    c.watch("w", "@Blue.Bottle", niche="кофе")
    assert c.watchlist("w")[0]["username"] == "blue.bottle"
    c.unwatch("w", "blue.bottle")
    assert c.watchlist("w") == []
