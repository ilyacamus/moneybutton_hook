import pytest

from telegram_bot.parsing import parse_hook_message, parse_metrics, parse_number, parse_result_command


@pytest.mark.parametrize(
    "text, expected",
    [
        ("skip 38%, views 12k, avg 6.5", {"skip_rate": 38, "views": 12_000, "avg_watch_sec": 6.5}),
        ("Скип рейт 42%  просмотры: 15 000", {"skip_rate": 42, "views": 15_000}),
        ("12к просмотров, 340 лайков, 25 репостов, сохранения 80",
         {"views": 12_000, "likes": 340, "shares": 25, "saves": 80}),
        ("удержание 61, ср. время 7,2 с, длина 32", {"hold_3s": 61, "avg_watch_sec": 7.2, "video_length_sec": 32}),
        ("пролистали 55% просмотров 3.4к", {"skip_rate": 55, "views": 3_400}),
        ("просмотры 1.2млн", {"views": 1_200_000}),
        ("просто текст", {}),
    ],
)
def test_parse_metrics(text, expected):
    assert parse_metrics(text) == pytest.approx(expected)


def test_parse_result_command():
    assert parse_result_command("#17 skip 38 views 12k") == (17, {"skip_rate": 38, "views": 12_000})
    assert parse_result_command("17") == (17, {})
    assert parse_result_command("") == (None, {})


def test_parse_number():
    assert parse_number("около 41,5%") == 41.5
    assert parse_number("нет") is None


def test_parse_hook_message_labelled():
    hook = parse_hook_message("Хук: Я потратил 47к\nТекст на экране: −47 000 ₽\nВизуал: бросаю чеки\nЗвук: удар")
    assert (hook.text, hook.on_screen_text, hook.visual, hook.audio) == ("Я потратил 47к", "−47 000 ₽", "бросаю чеки", "удар")


def test_parse_hook_message_plain_multiline():
    hook = parse_hook_message("Хотите больше клиентов?\nСейчас расскажу")
    assert hook.text == "Хотите больше клиентов? Сейчас расскажу" and not hook.visual
