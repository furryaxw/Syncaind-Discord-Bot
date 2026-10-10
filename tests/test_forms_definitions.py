"""表单定义（数据文件）与界面渲染：装载、校验、切步、组件、卡片与 CSV。

表单是数据，所以这里第一组测试是**往临时目录写 JSON 再读回来**；第二组是纯渲染。
两处都不需要机器人、也不需要数据库。
"""

from __future__ import annotations

import json
from pathlib import Path

import discord
import pytest

from bot.modules.forms import rendering, schema
from bot.modules.forms.store import APPROVED, PENDING, RECEIVED, FormPublication, FormSubmission

LOCALES = ("en-US", "zh-CN")


@pytest.fixture
def tr(i18n):
    """模块自己的文案（中文）。"""

    def translate(key: str, **kwargs: object) -> str:
        return i18n.t("zh-CN", key, **kwargs)

    return translate


@pytest.fixture
def text_en():
    return lambda value: schema.pick(value, "en-US")


@pytest.fixture
def text_zh():
    return lambda value: schema.pick(value, "zh-CN")


def text(english: str, chinese: str) -> dict[str, str]:
    return {"en-US": english, "zh-CN": chinese}


def payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "sample",
        "mode": "collect",
        "title": text("Sample", "样例"),
        "description": text("A sample form.", "一个样例表单。"),
        "fields": [{"key": "note", "kind": "short", "label": text("Note", "备注")}],
    }
    base.update(overrides)
    return base


def write_form(directory: Path, data: object, name: str = "sample.json") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def load(directory: Path) -> schema.FormCatalog:
    return schema.load_catalog(directory, locales=LOCALES)


def make_publication(**overrides: object) -> FormPublication:
    base: dict[str, object] = {
        "publication_id": 1,
        "guild_id": 100,
        "form_id": "sample",
        "channel_id": 6103,
        "message_id": 900,
        "status": "open",
        "capacity": None,
        "taken": 0,
        "closes_at": None,
        "created_by": 400,
        "created_at": "2026-10-07T00:00:00+00:00",
    }
    base.update(overrides)
    return FormPublication(**base)  # type: ignore[arg-type]


def make_submission(**overrides: object) -> FormSubmission:
    base: dict[str, object] = {
        "submission_id": 11,
        "guild_id": 100,
        "form_id": "sample",
        "publication_id": 1,
        "discord_user_id": 300,
        "answers": {},
        "status": PENDING,
        "submitted_at": "2026-10-07T01:02:03+00:00",
        "reviewed_by": None,
        "reviewed_at": None,
        "decision_reason": None,
        "attachment_dir": "a" * 32,
        "review_channel_id": None,
        "review_message_id": None,
    }
    base.update(overrides)
    return FormSubmission(**base)  # type: ignore[arg-type]


def field_payload(**overrides: object) -> dict[str, object]:
    """一个最小字段；写用例时只覆盖关心的那几项。"""
    base: dict[str, object] = {"key": "a", "kind": "short", "label": text("A", "甲")}
    base.update(overrides)
    return base


def over_long_option() -> dict[str, object]:
    return {"value": "x", "label": {"en-US": "z" * 101}}


def rich_form(**overrides: object) -> schema.FormDefinition:
    """一个把各种字段类型都用上的表单，供渲染测试使用。"""
    data = payload(
        title=text("Rich form", "丰富的表单"),
        fields=[
            field_payload(
                key="work",
                kind="choice",
                label=text("Pick one", "选一个"),
                options=[
                    {"value": "a", "label": text("Alpha", "甲")},
                    {"value": "b", "label": text("Beta", "乙")},
                ],
            ),
            field_payload(
                key="topics",
                kind="multi",
                label=text("Pick many", "可多选"),
                options=[
                    {"value": "x", "label": text("X ray", "X 光")},
                    {"value": "y", "label": text("Yankee", "Y 字")},
                ],
            ),
            field_payload(key="note", label=text("Note", "备注")),
            field_payload(key="detail", kind="long", label=text("Detail", "详情")),
            field_payload(key="samples", kind="file", label=text("Files", "文件"), max_files=3, required=False),
            field_payload(key="extra", label=text("Extra", "补充"), required=False),
        ],
    )
    data.update(overrides)
    return schema.parse_definition(data, source="sample.json", locales=LOCALES)


# ---------------------------------------------------------------- 装载（数据文件）


def test_loader_reads_every_json_file_sorted_by_name(tmp_path) -> None:
    write_form(tmp_path, payload(), name="sample.json")
    write_form(tmp_path, payload(id="other"), name="other.json")

    catalog = load(tmp_path)

    assert catalog.ids() == ("other", "sample")
    assert catalog.get("sample") is not None
    assert catalog.get("missing") is None


def test_loader_reads_fields_and_options(tmp_path) -> None:
    write_form(
        tmp_path,
        payload(
            fields=[
                {
                    "key": "topics",
                    "kind": "multi",
                    "label": text("About what", "关于什么"),
                    "options": [
                        {"value": "a", "label": text("Alpha", "甲")},
                        {"value": "b", "label": text("Beta", "乙")},
                    ],
                },
                {"key": "shots", "kind": "file", "label": text("Files", "文件"), "max_files": 3, "required": False},
            ]
        ),
    )

    definition = load(tmp_path).get("sample")
    assert definition is not None

    assert [(field.key, field.kind, field.required, field.max_files) for field in definition.fields] == [
        ("topics", schema.FIELD_MULTI, True, 1),
        ("shots", schema.FIELD_FILE, False, 3),
    ]
    assert [option.value for option in definition.fields[0].options] == ["a", "b"]
    assert definition.fields[0].option("a") is not None
    assert definition.fields[0].option("nope") is None


def test_a_missing_or_empty_directory_is_not_an_error(tmp_path) -> None:
    """「还没配表单」是正常状态：模块要能起来，只是没有任何表单。"""
    assert load(tmp_path / "no_such_dir").ids() == ()
    assert load(tmp_path).ids() == ()


def test_loader_requires_the_file_name_to_match_the_id(tmp_path) -> None:
    write_form(tmp_path, payload(id="mismatch"), name="sample.json")

    with pytest.raises(schema.DefinitionError, match="文件名"):
        load(tmp_path)


def test_loader_names_the_file_when_it_is_not_valid_json(tmp_path) -> None:
    (tmp_path / "sample.json").write_text("{ not json", encoding="utf-8")

    with pytest.raises(schema.DefinitionError, match=r"sample\.json"):
        load(tmp_path)


@pytest.mark.parametrize(
    ("label", "data"),
    [
        ("顶层不是对象", ["not", "an", "object"]),
        ("未知的顶层键", {**payload(), "titel": "typo"}),
        ("缺少键", {"id": "sample", "mode": "collect"}),
        ("用途非法", payload(mode="teleport")),
        ("字段里未知键", payload(fields=[field_payload(typo=1)])),
        ("字段类型非法", payload(fields=[field_payload(kind="slider")])),
        ("选项不是数组", payload(fields=[field_payload(kind="choice", options="yes")])),
        ("选项缺少 label", payload(fields=[field_payload(kind="choice", options=[{"value": "x"}])])),
        (
            "非选项字段带 options",
            payload(fields=[field_payload(options=[{"value": "x", "label": text("X", "X")}])]),
        ),
        ("required 不是布尔", payload(fields=[field_payload(required="yes")])),
        ("max_files 不是整数", payload(fields=[field_payload(kind="file", max_files="3")])),
        ("字段数组为空", payload(fields=[])),
        ("文案为空", payload(fields=[field_payload(label={"en-US": "   "})])),
        ("没有文案", payload(fields=[field_payload(label={})])),
        ("陌生的语言标签", payload(title={"fr-FR": "Bonjour"})),
        ("标题超长", payload(title={"en-US": "x" * 46})),
        ("副标题超长", payload(fields=[field_payload(description={"en-US": "y" * 101})])),
        ("选项名超长", payload(fields=[field_payload(kind="choice", options=[over_long_option()])])),
    ],
)
def test_loader_rejects_broken_definitions(tmp_path, label: str, data: object) -> None:
    write_form(tmp_path, data)

    with pytest.raises(schema.DefinitionError):
        load(tmp_path)


def sample_definition(**overrides: object) -> schema.FormDefinition:
    """一个最小的合法定义；用例只覆盖要破坏的那一项。"""
    base: dict[str, object] = {
        "id": "sample",
        "mode": schema.MODE_COLLECT,
        "title": text("Sample", "样例"),
        "description": text("A sample form.", "一个样例表单。"),
        "fields": (schema.Field(key="note", label=text("Note", "备注"), kind=schema.FIELD_SHORT),),
    }
    base.update(overrides)
    return schema.FormDefinition(**base)  # type: ignore[arg-type]


def duplicate_note_fields() -> tuple[schema.Field, ...]:
    field = schema.Field(key="note", label=text("Note", "备注"), kind=schema.FIELD_SHORT)
    return (field, field)


def only_one_option_field() -> schema.Field:
    return schema.Field(
        key="a",
        label=text("A", "甲"),
        kind=schema.FIELD_CHOICE,
        options=(schema.Option(value="only", label=text("A", "甲")),),
    )


@pytest.mark.parametrize(
    ("label", "definition"),
    [
        ("用途非法", sample_definition(mode="unknown")),
        ("没有字段", sample_definition(fields=())),
        ("字段 key 重复", sample_definition(fields=duplicate_note_fields())),
        ("选项太少", sample_definition(fields=(only_one_option_field(),))),
        ("未知动作", sample_definition(actions=("teleport",))),
        ("收集类不该有通过后动作", sample_definition(actions=(schema.ACTION_GRANT_ROLE,))),
        ("申请类必须一人一份", sample_definition(mode=schema.MODE_APPLY, allow_multiple=True)),
    ],
)
def test_validation_rejects_broken_definitions(label: str, definition: schema.FormDefinition) -> None:
    """解析挡住了形状问题，这一层管语义 —— 两者都要能被单独验证。"""
    with pytest.raises(schema.DefinitionError):
        schema._validate((definition,))


def test_duplicate_ids_are_rejected() -> None:
    with pytest.raises(schema.DefinitionError):
        schema._validate((sample_definition(id="same"), sample_definition(id="same")))


# ---------------------------------------------------------------- 文案


def test_pick_falls_back_to_the_first_language() -> None:
    value = {"en-US": "Hello", "zh-CN": "你好"}

    assert schema.pick(value, "zh-CN") == "你好"
    assert schema.pick(value, "en-US") == "Hello"
    assert schema.pick(value, "ja-JP") == "Hello", "没有的语言回落到第一条"
    assert schema.pick({"zh-CN": "只有中文"}, "en-US") == "只有中文"


def test_supported_locales_come_from_the_core_catalog() -> None:
    assert "en-US" in schema.supported_locales()
    assert "zh-CN" in schema.supported_locales()


# ---------------------------------------------------------------- 切步与组件


def test_fields_are_split_into_steps_of_five() -> None:
    definition = rich_form()

    assert definition.step_count == 2
    assert [len(step) for step in definition.steps] == [5, 1]


def test_build_step_items_picks_the_right_component_per_kind(text_en, tr) -> None:
    items = rendering.build_step_items(rich_form(), 0, text=text_en, tr=tr)

    assert [type(item.component) for item in items] == [
        discord.ui.Select,
        discord.ui.Select,
        discord.ui.TextInput,
        discord.ui.TextInput,
        discord.ui.FileUpload,
    ]
    assert items[3].component.style is discord.TextStyle.paragraph
    assert items[2].component.style is discord.TextStyle.short
    assert items[4].component.max_values == 3


def test_select_options_use_the_form_text(text_zh, tr) -> None:
    select = rendering.build_step_items(rich_form(), 0, text=text_zh, tr=tr)[0].component

    assert [option.value for option in select.options] == ["a", "b"]
    assert [option.label for option in select.options] == ["甲", "乙"]


def test_labels_and_descriptions_land_on_the_label(text_zh, tr) -> None:
    data = payload(
        fields=[
            {"key": "a", "kind": "short", "label": text("Short", "短")},
            {
                "key": "b",
                "kind": "short",
                "label": text("Question", "问题"),
                "description": text("Explains", "补充说明"),
            },
        ]
    )
    definition = schema.parse_definition(data, source="sample.json", locales=LOCALES)

    items = rendering.build_step_items(definition, 0, text=text_zh, tr=tr)
    by_id = {item.component.custom_id: item for item in items}

    assert by_id[rendering.field_id("a")].text == "短"
    assert by_id[rendering.field_id("a")].description is None
    assert by_id[rendering.field_id("b")].description == "补充说明"


def test_prefill_marks_the_stored_choice_and_text(text_en, tr) -> None:
    items = rendering.build_step_items(rich_form(), 0, text=text_en, tr=tr, prefill={"work": "b", "note": "hi"})

    assert [option.default for option in items[0].component.options] == [False, True]
    assert items[2].component.value == "hi"


def test_build_step_items_rejects_an_unknown_step(text_en, tr) -> None:
    with pytest.raises(ValueError):
        rendering.build_step_items(rich_form(), 9, text=text_en, tr=tr)


def test_collect_answers_reads_every_component_kind(text_en, tr) -> None:
    from tests.forms_fakes import make_attachment

    modal = discord.ui.Modal(title="t")
    for item in rendering.build_step_items(rich_form(), 0, text=text_en, tr=tr):
        modal.add_item(item)

    attachment = make_attachment()
    components = {item.component.custom_id: item.component for item in modal.children}
    components[rendering.field_id("work")]._values = ["b"]
    components[rendering.field_id("topics")]._values = ["x", "y"]
    components[rendering.field_id("note")]._value = "hello"
    components[rendering.field_id("detail")]._value = "long"
    components[rendering.field_id("samples")]._values = [attachment]

    assert rendering.collect_answers(modal) == {
        "work": ["b"],
        "topics": ["x", "y"],
        "note": "hello",
        "detail": "long",
        "samples": [attachment],
    }


# ---------------------------------------------------------------- 展示


def test_format_value_covers_every_kind(text_zh, tr) -> None:
    definition = rich_form()
    work = definition.field("work")
    topics = definition.field("topics")
    samples = definition.field("samples")
    assert work is not None and topics is not None and samples is not None

    assert rendering.format_value(work, ["a"], text=text_zh, tr=tr) == "甲"
    assert rendering.format_value(topics, ["x", "y"], text=text_zh, tr=tr) == "X 光、Y 字"
    assert rendering.format_value(work, [], text=text_zh, tr=tr) == "（未填）"
    value = [{"name": "a.png", "stored": "x", "size": 2048, "type": ""}]
    assert rendering.format_value(samples, value, text=text_zh, tr=tr) == "1 个附件：a.png（2.0 KiB）"


def test_format_value_keeps_an_option_value_that_no_longer_exists(text_zh, tr) -> None:
    """定义改过之后，旧提交里可能出现已经不存在的选项值 —— 原样显示，别吞掉。"""
    definition = rich_form()
    work = definition.field("work")
    assert work is not None

    assert rendering.format_value(work, ["gone"], text=text_zh, tr=tr) == "gone"


def test_format_size_switches_units() -> None:
    assert rendering.format_size(12) == "12 B"
    assert rendering.format_size(2048) == "2.0 KiB"
    assert rendering.format_size(3 * 1024 * 1024) == "3.0 MiB"


def test_answer_fields_skips_blank_answers(text_zh, tr) -> None:
    pairs = rendering.answer_fields(rich_form(), {"work": ["a"], "note": "  ", "detail": ""}, text=text_zh, tr=tr)

    assert [name for name, _value in pairs] == ["选一个"]


def test_is_blank_treats_whitespace_and_empty_lists_as_empty() -> None:
    assert rendering.is_blank(None) is True
    assert rendering.is_blank("   ") is True
    assert rendering.is_blank([]) is True
    assert rendering.is_blank("x") is False
    assert rendering.is_blank([{"name": "a.png"}]) is False


def test_clip_keeps_short_text_and_marks_long_text() -> None:
    assert rendering.clip("abc", 5) == "abc"
    assert rendering.clip("abcdef", 5) == "abcd…"


def test_open_card_shows_capacity_deadline_and_closed_state(text_en, tr) -> None:
    definition = rich_form()

    publication = make_publication(capacity=10, taken=3, closes_at="2026-10-07T12:00:00+00:00")
    embed = rendering.open_card(definition, publication, text=text_en, tr=tr)
    assert "名额：7 / 10" in embed.description
    assert "截止：2026-10-07T12:00:00+00:00" in embed.description

    closed = rendering.open_card(definition, publication, text=text_en, tr=tr, closed=True)
    assert "已经关闭" in closed.description

    full = rendering.open_card(definition, make_publication(capacity=2, taken=2), text=text_en, tr=tr)
    assert "名额已经满了" in full.description


def test_review_card_shows_the_answers_and_the_decision(text_zh, tr) -> None:
    submission = make_submission(
        answers={"work": ["b"], "detail": "说点什么"},
        status=APPROVED,
        reviewed_by=700,
        decision_reason="looks good",
    )

    embed = rendering.review_card(
        rich_form(), submission, text=text_zh, tr=tr, applicant="<@300>", reviewed_by="<@700>"
    )

    values = {field.name: field.value for field in embed.fields}
    assert values["选一个"] == "乙"
    assert values["详情"] == "说点什么"
    assert "looks good" in embed.description
    assert "<@700>" in embed.description


def test_review_card_truncates_instead_of_blowing_the_embed_limit(text_en, tr) -> None:
    """长文表单一定塞不下一条 embed：宁可截断并说明，也不能让它抛异常导致申请发不出去。"""
    data = payload(
        fields=[
            {"key": f"f{index}", "kind": "long", "label": text(f"Field {index}", f"字段 {index}")}
            for index in range(12)
        ]
    )
    definition = schema.parse_definition(data, source="sample.json", locales=LOCALES)
    answers = {f"f{index}": "长" * 700 for index in range(12)}

    embed = rendering.review_card(definition, make_submission(answers=answers), text=text_en, tr=tr, applicant="<@300>")

    assert embed.footer.text is not None and "export" in embed.footer.text
    assert sum(len(field.value) for field in embed.fields) < rendering.EMBED_TOTAL_LIMIT


def test_results_lines_show_a_preview(text_zh, tr) -> None:
    rows = [make_submission(answers={"work": ["a"]}, status=RECEIVED)]

    lines = rendering.results_lines(rich_form(), rows, text=text_zh, tr=tr)

    assert len(lines) == 1
    assert "甲" in lines[0]
    assert "已收到" in lines[0]


def test_build_csv_is_excel_friendly_and_quotes_values(text_zh, tr) -> None:
    definition = rich_form()
    rows = [
        make_submission(
            answers={
                "work": ["a"],
                "topics": ["x", "y"],
                "detail": 'line one, "quoted"',
                "samples": [{"name": "a b.png", "stored": "s", "size": 100, "type": ""}],
            },
            status=RECEIVED,
        )
    ]

    payload_bytes = rendering.build_csv(
        definition, rows, text=text_zh, tr=tr, name_for=lambda user_id: f"user{user_id}"
    )

    assert payload_bytes.startswith(b"\xef\xbb\xbf"), "不带 BOM 的话 Excel 会把中文读成乱码"
    decoded = payload_bytes.decode("utf-8-sig")
    header, row = decoded.strip().splitlines()
    assert header.split(",")[0] == "提交编号"
    assert "甲" in row and "X 光、Y 字" in row
    assert '"line one, ""quoted"""' in row
    assert "a b.png（100 B）" in row
    assert "user300" in row
