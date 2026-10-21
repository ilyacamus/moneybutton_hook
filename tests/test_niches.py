"""Knowledge base by niche: the client's niche, reels from an outlier index, the niche's breakout model."""
import json
import random

from hookbot import cli, render
from hookbot.core import breakout, corpus, knowledge, learning, niches, outliers
from hookbot.core.corpus import Corpus, ReelRecord
from hookbot.engine import HookEngine
from hookbot.scout import Scout, ScoutConfig
from test_plugin_channel import call, server  # noqa: F401 - fixture

OUTLIERS = """## Instagram

**@trader_one** — "Не покупай биткоин, пока не посмотришь это"
   134.1K views (134.3x their median of 998.5) · 796 followers
   reel:DcqLI2ovmh0
   **reel_concept**: A trader explains a Bitcoin chart.
   **niche**: Finance
   **hook_0_3s**:
     visual: Creator points at a falling chart.
     text: Биткоин упадёт?
     audio: Immediate Russian speech.
   **format**:
     style: Split screen (creator + screen recording)
     template: chart explainer

**@trader_two** — "#трейдинг"
   1.2M views (22.2x their median of 54K) · 296.1K followers · 8s
   reel:DcS114mOggm
   **niche**: Finance
   **hook_0_3s**:
     visual: Close-up of the creator.
     text: Мне не стоило показывать этот график
     audio: Bass-heavy beat.

## TikTok

**@tok_trader** — "Слил депозит за минуту"
   80K views (10.0x their median of 8K)
   tiktok:7412345678901234567
   **tiktok_concept**: A story about a liquidation.
   **hook_0_3s**:
     visual: Red screen with liquidation notice.
"""


def test_classify_and_resolve():
    result = niches.classify(["Как я слил депозит на фьючерсах", "Рецепт пирога за 5 минут", "Биткоин: где вход"],
                             bio="Трейдер. Учу торговать криптой")
    assert result["best"] == "crypto_trading" and result["confidence"] == "высокая"
    assert result["niches"][0]["share"] == 0.8 and "фьючерс" in result["niches"][0]["keywords"]
    assert niches.resolve("крипто-трейдинг: индикаторы, торговые боты") == "crypto_trading"
    assert niches.resolve("Фитнес") == "fitness" and niches.resolve("fitness") == "fitness"
    assert niches.resolve("что-то совсем другое") is None
    assert niches.canonical("audience:cli") == "audience:cli"  # internal tags stay as they are
    assert niches.canonical("coaching") == "coaching"
    # Short words match whole; stems match word starts.
    assert niches.matches("said air", niches.keywords("ai_tech")) == []
    assert niches.matches("ИИ-ассистент и ChatGPT", niches.keywords("ai_tech")) == ["chatgpt", "ии"]
    assert niches.classify([])["best"] is None and niches.classify([])["confidence"] == "низкая"


def test_every_niche_is_complete():
    ids = niches.ids()
    assert len(ids) == len(set(ids)) >= 15
    for niche in niches.all_niches():
        assert niche["name"] and niche["keywords"] and niche["queries"], niche["id"]
        assert niches.resolve(niche["name"]) == niche["id"]


def test_parse_outlier_search():
    items = outliers.parse(OUTLIERS, "крипта")
    assert [(i["platform"], i["creator"], i["id"]) for i in items] == [
        ("instagram", "trader_one", "DcqLI2ovmh0"), ("instagram", "trader_two", "DcS114mOggm"),
        ("tiktok", "tok_trader", "7412345678901234567")]
    one, two, tok = items
    assert one["views"] == 134_100 and one["x_median"] == 134.3 and one["median"] == 998.5 and one["followers"] == 796
    assert two["views"] == 1_200_000 and two["median"] == 54_000 and two["duration"] == 8.0
    assert one["hook_text"] == "Биткоин упадёт?" and one["style"].startswith("Split screen")
    assert tok["concept"].startswith("A story") and tok["followers"] is None
    text = outliers.description(one)
    assert "Биткоин упадёт?" in text and "134" not in text  # blind: no views or multiples
    assert outliers.url(tok) == "https://www.tiktok.com/@tok_trader/video/7412345678901234567"
    # Tool output saved by Claude Code as a JSON list of text blocks.
    assert outliers.dedupe(items + items) == items


def test_outlier_reels_never_enter_a_baseline(store):
    c = Corpus(store)
    for i, views in enumerate((1000, 1100, 1200, 1300, 1400)):
        c.upsert(ReelRecord(source="instagram", external_id=f"feed{i}", creator="ig:trader_one", niche="крипта",
                            views=views))
    counts = outliers.import_items(c, outliers.parse(OUTLIERS), "crypto_trading")
    assert counts == {"found": 3, "new": 3, "creators": 3}
    feed = [c.find("instagram", f"feed{i}") for i in range(5)]
    assert [r["baseline_views"] for r in feed] == [1250.0, 1250.0, 1200.0, 1150.0, 1150.0]
    viral = c.find("instagram", "DcqLI2ovmh0")
    assert viral["feed"] == 0 and viral["niche"] == "crypto_trading" and viral["label"] == "viral"
    assert viral["outlier_score"] == round(134_100 / 998.5, 3)  # the index's median over the creator's history
    alone = c.find("instagram", "DcS114mOggm")
    assert alone["outlier_score"] == round(1_200_000 / 54_000, 3)  # no feed yet: the index's median
    assert feed[0]["niche"] == "crypto_trading"  # free-text niches are stored as the taxonomy id
    # Seen in the feed later: the reel becomes part of the baseline.
    c.upsert(ReelRecord(source="instagram", external_id="DcqLI2ovmh0", creator="ig:trader_one", views=140_000))
    assert c.find("instagram", "DcqLI2ovmh0")["feed"] == 1


def synthetic_pack(n_creators=15, per_creator=6, signal=0.12, seed=3):
    rng = random.Random(seed)
    ids = knowledge.criterion_ids()
    pack = []
    for c in range(n_creators):
        for i in range(per_creator):
            criteria = {cid: rng.randint(2, 8) for cid in ids}
            multiple = 10 ** (1.2 + signal * (criteria["relevance"] - 5) + rng.gauss(0, 0.1))
            pack.append({"source": "instagram", "external_id": f"c{c}r{i}", "creator": f"ig:creator{c}",
                         "niche": "Крипта и трейдинг", "views": int(1000 * multiple), "baseline_views": 1000.0,
                         "feed": False, "description": f"Описание {c}-{i}",
                         "analysis": {"criteria": criteria, "trigger_ids": [], "on_screen_text": "", "visual": ""}})
    return pack


def test_breakout_model(store):
    c = Corpus(store)
    assert breakout.build(store, "crypto_trading") is None  # nothing studied yet
    assert c.import_pack(synthetic_pack()) == {"items": 90, "new": 90, "analysed": 90}
    # The client's own reels calibrate the client only: they never enter the niche's knowledge.
    c.upsert(ReelRecord(source="instagram_own", external_id="own1", creator="own:w", niche="крипта", views=10,
                        baseline_views=1000, workspace="w"))
    corpus.store_analysis(c, c.find("instagram_own", "own1")["id"], {"criteria": {"relevance": 10}})
    c.label_all()
    coverage = c.niche_coverage()["crypto_trading"]
    assert (coverage["reels"], coverage["creators"], coverage["studied"], coverage["control"]) == (90, 15, 90, 0)
    model = breakout.build(store, "crypto_trading")
    assert model["n"] == 90 and model["n_creators"] == 15 and model["sample"] == "breakouts_only"
    assert model["cv"]["spearman"] > 0.5 and model["quality"] == "сильная"
    assert model["cv"]["top_fifth_median_x"] > model["cv"]["bottom_fifth_median_x"]
    assert breakout.top_drivers(model)[0][0] == "relevance"
    low = breakout.predict(model, {"relevance": 2})
    high = breakout.predict(model, {"relevance": 8})
    assert high["expected_x"] > low["expected_x"] and high["percentile"] > low["percentile"]
    assert breakout.predict(model, {}) is None
    breakout.save(store, model)
    assert breakout.latest(store, "крипто-трейдинг")["n"] == 90
    # No signal → the model says so.
    noisy = breakout.cross_validate([{**r, "y": random.Random(i).random()} for i, r in
                                     enumerate(breakout.training_rows(store, "crypto_trading"))])
    assert breakout.quality(noisy["spearman"]) in ("нет сигнала", "слабая")


def test_bundled_model_scores_hooks_out_of_the_box(store):
    model = breakout.latest(store, "crypto_trading")
    assert model["bundled"] and model["n"] >= 1000 and model["quality"] in ("слабая", "умеренная", "сильная")
    assert len(model["quantiles"]) == 101 and set(model["weights"]) == set(knowledge.criterion_ids())
    assert breakout.latest(store, "fitness") is None
    store.update_profile("w", niche="крипто-трейдинг: индикаторы и боты")
    criteria = {cid: 6 for cid in knowledge.criterion_ids()}
    result = learning.analyze(store, "w", criteria)
    potential = result["niche_potential"]
    assert potential["niche"] == "crypto_trading" and potential["expected_x"] > 1 and 0 <= potential["percentile"] <= 100
    store.update_profile("w", niche="фитнес")
    assert learning.analyze(store, "w", criteria)["niche_potential"] is None
    store.update_profile("w", niche="Крипта и трейдинг")
    prompt = learning.build_context(store, "w").to_prompt()
    assert "Модель ниши «Крипта и трейдинг»" in prompt and "не как обещание" in prompt
    lines = render.render_potential(potential, render.Plain)
    assert "Потенциал в нише «Крипта и трейдинг»" in lines[0] and "качество прогноза" in lines[2]


def test_niche_pack_roundtrip(store, tmp_path):
    c = Corpus(store)
    c.import_pack(synthetic_pack(n_creators=2, per_creator=3))
    c.upsert(ReelRecord(source="instagram_own", external_id="own1", creator="own:w", niche="crypto_trading",
                        views=500, skip_rate=41.0, workspace="w"))
    corpus.store_analysis(c, c.find("instagram_own", "own1")["id"], {"criteria": {"relevance": 5}})
    c.set_label(c.find("instagram_own", "own1")["id"], "typical", 1.0)
    pack = c.export_pack("crypto_trading")
    assert len(pack) == 6 and all("skip_rate" not in p and "workspace" not in p for p in pack)
    assert pack[0]["analysis"]["criteria"] and pack[0]["feed"] is False
    other = corpus.MemoryStore(":memory:")
    assert Corpus(other).import_pack(json.loads(json.dumps(pack))) == {"items": 6, "new": 6, "analysed": 6}
    assert Corpus(other).import_pack(pack)["analysed"] == 0  # analyses are not redone


def test_science_report_skips_reels_picked_by_outcome(store):
    c = Corpus(store)
    c.import_pack(synthetic_pack())
    report = corpus.build_report(c, "crypto_trading")
    assert report["niche"] == "crypto_trading" and report["effects"] == []  # no typical reels to compare with
    assert corpus.match_niche(store, "крипто-трейдинг для новичков") == "crypto_trading"


def test_scout_grows_each_niche_with_its_own_words(store, client):
    scout = Scout(store, HookEngine(store, client=client), ig=None, config=ScoutConfig())
    reels = lambda *captions: [{"media_product_type": "REELS", "caption": c} for c in captions]  # noqa: E731
    crypto = reels("Биткоин пробил уровень", "Мой вход в лонг", "Утро")
    assert scout.on_niche({"biography": ""}, crypto, "крипто-трейдинг")
    assert not scout.on_niche({"biography": ""}, crypto, "fitness")
    assert scout.on_niche({"biography": "Тренер, тренировки дома"}, reels("Утро"), "Фитнес и тренировки")
    assert not scout.on_niche({"biography": "Трейдер"}, crypto, "coaching")  # a niche outside the taxonomy: no words
    custom = Scout(store, HookEngine(store, client=client), ig=None, config=ScoutConfig(niche_keywords=("утро",)))
    assert custom.on_niche({"biography": ""}, crypto, "fitness")


def test_cli_niche_commands(tmp_path, capsys):
    db = str(tmp_path / "h.db")
    texts = tmp_path / "channel.txt"
    texts.write_text("Трейдер, учу торговать криптой\n\nКак я слил депозит на фьючерсах\n\nБиткоин: где вход",
                     encoding="utf-8")
    assert cli.main(["--db", db, "niche", "detect", "--text", str(texts), "--set"]) == 0
    out = capsys.readouterr().out
    assert "Крипта и трейдинг" in out and "модель: 10" in out and "Ниша профиля «cli»: Крипта и трейдинг" in out

    search = tmp_path / "search.json"
    search.write_text(json.dumps([{"type": "text", "text": OUTLIERS}]), encoding="utf-8")
    assert cli.main(["--db", db, "niche", "import-vidiq", str(search), "--niche", "крипта", "--query", "биткоин"]) == 0
    assert "Роликов: 3, новых: 3, авторов: 3" in capsys.readouterr().out

    pack = tmp_path / "pack.json"
    pack.write_text(json.dumps(synthetic_pack()), encoding="utf-8")
    assert cli.main(["--db", db, "niche", "import-pack", str(pack)]) == 0
    assert cli.main(["--db", db, "niche", "build", "крипто-трейдинг"]) == 0
    assert "90 роликов, 15 авторов; качество сильная" in capsys.readouterr().out
    assert cli.main(["--db", db, "niche", "list"]) == 0
    out = capsys.readouterr().out
    assert "Крипта и трейдинг (crypto_trading): собрано 93, изучено 90; модель: 90 роликов" in out
    assert "Фитнес и тренировки (fitness): не собрано; модели нет" in out
    assert cli.main(["--db", db, "niche", "export", "crypto_trading", "--out", str(tmp_path / "out.json")]) == 0
    assert len(json.loads((tmp_path / "out.json").read_text("utf-8"))) == 90
    assert cli.main(["--db", db, "niche", "queries", "фитнес"]) == 0
    assert "тренировка дома" in capsys.readouterr().out


def test_plugin_niche_tools(server, tmp_path):  # noqa: F811
    found = call(server, "niche_detect", bio="Трейдер", captions=["Слил депозит на фьючерсах", "Биткоин"], set=True)
    assert found["best"] == "crypto_trading" and found["profile_niche"] == "Крипта и трейдинг"
    assert found["niches"][0]["model"]["n"] >= 1000  # the model shipped with the package
    listed = call(server, "niche_list")
    assert {n["id"] for n in listed["niches"]} == set(niches.ids())

    saved = tmp_path / "search.txt"
    saved.write_text(OUTLIERS, encoding="utf-8")
    assert call(server, "import_outliers", path=str(saved), niche="крипта")["found"] == 3
    batch = call(server, "next_reels", scope="niche", niche="Крипта и трейдинг", limit=5)
    assert batch["errors"] == [] and len(batch["reels"]) == 3
    reel = batch["reels"][0]
    assert reel["frames"] == [] and "Описание первых 3 секунд" in reel["video_facts"]
    assert "views" not in json.dumps(reel) and "134" not in reel["video_facts"]  # blind
    for r in batch["reels"]:
        criteria = {cid: 5 for cid in knowledge.criterion_ids()}
        call(server, "save_reel_analysis", reel_id=r["reel_id"], visual="график", face_first_second=False,
             text_first_second=True, motion_first_second=False, criteria=criteria, trigger_ids=[])
    rebuilt = call(server, "niche_model", niche="crypto_trading", rebuild=True)
    assert rebuilt["model"]["bundled"] and "мало данных" in rebuilt["reason"]  # 3 reels: keep the shipped model

    hook = call(server, "save_evaluation", text="Показываю один раз", criteria={cid: 6 for cid in knowledge.criterion_ids()})
    assert hook["niche_potential"]["niche"] == "crypto_trading"


def synthetic_feeds(n_creators=15, per_creator=10, seed=5):
    """Creators' whole feeds: big and small accounts; within a feed, curiosity decides which reel takes off."""
    rng = random.Random(seed)
    ids = knowledge.criterion_ids()
    pack = []
    for c in range(n_creators):
        size = rng.choice([300, 3000, 30000])
        for i in range(per_creator):
            criteria = {cid: rng.randint(2, 8) for cid in ids}
            multiple = 10 ** (0.15 * (criteria["curiosity"] - 5) + rng.gauss(0, 0.08))
            pack.append({"source": "instagram", "external_id": f"f{c}r{i}", "creator": f"ig:feed{c}",
                         "niche": "crypto_trading", "views": int(size * multiple), "feed": True,
                         "published_at": "2026-09-01T12:00:00+00:00",
                         "analysis": {"criteria": criteria, "input": "cover", "topic": "crypto"}})
    pack[0]["analysis"]["topic"] = "other"  # a creator's off-topic reel is left out of the niche's knowledge
    return pack


def test_within_creator_model(store):
    c = Corpus(store)
    c.import_pack(synthetic_pack())  # breakouts described in text (the outlier index)
    c.import_pack(synthetic_feeds())  # whole feeds rated from cover frames
    model = breakout.build(store, "crypto_trading")
    assert model["input"] == "description" and model["n"] == 90  # one rating input per model
    within = model["within"]
    # Each creator is compared with themselves, within one rating input: 15 feeds and 15 creators' breakouts.
    assert within["inputs"] == {"description": 90, "cover": 149} and within["n_creators"] == 30
    assert within["quality"] == "сильная" and within["cv"]["auc"] > 0.65
    assert "curiosity" in breakout.top_drivers(model, within=True)[0][:2]
    better = breakout.predict(model, {"curiosity": 9})["vs_typical"]
    worse = breakout.predict(model, {"curiosity": 2})["vs_typical"]
    assert better["x"] > 1 > worse["x"] and better["n_creators"] == 30
    # Only feeds rated from frames: the main model is trained on them.
    other = corpus.MemoryStore(":memory:")
    Corpus(other).import_pack(synthetic_feeds())
    assert breakout.build(other, "crypto_trading")["input"] == "cover"
