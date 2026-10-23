"""HTML 表单渲染与服务端解析。"""

from __future__ import annotations

from bot.modules.forms.html import (
    UploadedFile,
    load_custom_template,
    load_template,
    parse_answers,
    render_form,
    render_message,
    render_stylesheet,
    validate_custom_template,
)
from bot.modules.forms.schema import (
    FIELD_CHOICE,
    FIELD_FILE,
    FIELD_LONG,
    FIELD_MULTI,
    FIELD_SHORT,
    Field,
    FormDefinition,
    Option,
)

LOCALE = "zh-CN"


def definition() -> FormDefinition:
    return FormDefinition(
        id="demo",
        mode="collect",
        title={"zh-CN": "测试表单", "en-US": "Demo form"},
        description={"zh-CN": "请填写", "en-US": "Fill in"},
        fields=(
            Field(key="name", kind=FIELD_SHORT, label={"zh-CN": "名字"}),
            Field(key="about", kind=FIELD_LONG, label={"zh-CN": "介绍"}, required=False),
            Field(
                key="choice",
                kind=FIELD_CHOICE,
                label={"zh-CN": "选择"},
                options=(
                    Option(value="a", label={"zh-CN": "A"}),
                    Option(value="b", label={"zh-CN": "B"}),
                ),
            ),
            Field(
                key="tags",
                kind=FIELD_MULTI,
                label={"zh-CN": "标签"},
                required=False,
                options=(
                    Option(value="x", label={"zh-CN": "X"}),
                    Option(value="y", label={"zh-CN": "Y"}),
                ),
            ),
            Field(
                key="files",
                kind=FIELD_FILE,
                label={"zh-CN": "附件"},
                required=False,
                max_files=2,
            ),
        ),
    )


def test_templates_are_files() -> None:
    assert "<!doctype html>" in load_template("form.html")
    assert "{{fields}}" in load_template("form.html")
    assert "body" in render_stylesheet()


def test_message_template() -> None:
    page = render_message(title="已提交", message="谢谢", locale="zh-CN")

    assert "已提交" in page
    assert "谢谢" in page
    assert "/form.css" in page


def test_custom_template_validation(tmp_path) -> None:
    path = tmp_path / "demo.html"
    path.write_text("<form action='{{action}}'>{{csrf_field}}</form>", encoding="utf-8")

    text = load_custom_template(tmp_path, "demo")

    assert text is not None
    assert validate_custom_template(text) == []
    assert validate_custom_template("<form></form>") == ["{{action}}", "{{csrf_field}}"]


def test_render_contains_fields_and_action() -> None:
    html = render_form(
        definition(),
        action="/f/abc",
        csrf_field='<input type="hidden" name="csrf" value="t">',
        locale=LOCALE,
    )

    assert 'action="/f/abc"' in html
    assert 'name="csrf"' in html
    assert 'name="name"' in html
    assert 'name="about"' in html
    assert 'name="choice"' in html
    assert 'name="tags"' in html
    assert 'name="files"' in html
    assert "测试表单" in html


def test_parse_valid_answers() -> None:
    parsed = parse_answers(
        definition(),
        text={"name": " Alice ", "about": "", "choice": "a"},
        multi={"tags": ["x", "y"]},
        files={"files": [UploadedFile(name="a.txt", data=b"hello")]},
    )

    assert parsed.ok
    assert parsed.answers["name"] == "Alice"
    assert parsed.answers["choice"] == "a"
    assert parsed.answers["tags"] == ["x", "y"]
    assert parsed.answers["files"][0]["name"] == "a.txt"


def test_missing_required_and_invalid_option() -> None:
    parsed = parse_answers(definition(), text={"choice": "z"}, multi={}, files={})

    assert not parsed.ok
    assert "name" in parsed.errors
    assert parsed.errors["choice"] == "Invalid option."


def test_too_many_files_rejected() -> None:
    parsed = parse_answers(
        definition(),
        text={"name": "A", "choice": "a"},
        multi={},
        files={
            "files": [
                UploadedFile(name="1", data=b"1"),
                UploadedFile(name="2", data=b"2"),
                UploadedFile(name="3", data=b"3"),
            ]
        },
    )

    assert not parsed.ok
    assert "files" in parsed.errors
