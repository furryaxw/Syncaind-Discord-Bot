"""HTML 表单：文件模板渲染与服务端解析。

模板放在 ``templates/`` 下，与 Python 代码分离；``data/forms/<id>.html`` 存在时优先用它。
占位符是 ``{{name}}``，只做字符串替换，不执行模板逻辑。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any

from .schema import (
    FIELD_CHOICE,
    FIELD_FILE,
    FIELD_LONG,
    FIELD_MULTI,
    FIELD_SHORT,
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    FormDefinition,
    pick,
)

SHORT_MAX = 200
LONG_MAX = 4000
TEMPLATES_DIR = Path(__file__).with_name("templates")
DEFAULT_CSS_URL = "/form.css"


@dataclass(frozen=True)
class UploadedFile:
    name: str
    data: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True)
class ParsedAnswers:
    answers: dict[str, Any]
    errors: dict[str, str]
    files: dict[str, list[UploadedFile]]

    @property
    def ok(self) -> bool:
        return not self.errors


def render_form(
    definition: FormDefinition,
    *,
    action: str,
    csrf_field: str,
    locale: str,
    submit_label: str = "Submit",
    values: Mapping[str, Any] | None = None,
    errors: Mapping[str, str] | None = None,
    custom_template: str | None = None,
) -> str:
    values = values or {}
    errors = errors or {}
    title = pick(definition.title, locale)
    description = pick(definition.description, locale)
    fields = "\n".join(_render_field(field, locale, values, errors) for field in definition.fields)
    replacements = {
        "lang": escape(locale),
        "title": escape(title),
        "description": escape(description),
        "description_block": f"<p>{escape(description)}</p>" if description else "",
        "action": escape(action),
        "csrf_field": csrf_field,
        "fields": fields,
        "submit_label": escape(submit_label),
        "css_url": DEFAULT_CSS_URL,
    }
    template = custom_template if custom_template is not None else load_template("form.html")
    return apply_template(template, replacements)


def render_message(*, title: str, message: str, locale: str = "en-US") -> str:
    return apply_template(
        load_template("message.html"),
        {
            "lang": escape(locale),
            "title": escape(title),
            "message": escape(message),
            "css_url": DEFAULT_CSS_URL,
        },
    )


def render_stylesheet() -> str:
    return load_asset("form.css")


def load_template(name: str) -> str:
    return load_asset(name)


def load_custom_template(definitions_dir: Path, form_id: str) -> str | None:
    path = definitions_dir / f"{form_id}.html"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def validate_custom_template(text: str) -> list[str]:
    """返回缺失的必需占位符。自定义 HTML 至少要让机器人能提交。"""
    required = ("{{action}}", "{{csrf_field}}")
    return [placeholder for placeholder in required if placeholder not in text]


def apply_template(template: str, values: Mapping[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


@lru_cache(maxsize=8)
def load_asset(name: str) -> str:
    path = TEMPLATES_DIR / name
    return path.read_text(encoding="utf-8")


def _render_field(field: Any, locale: str, values: Mapping[str, Any], errors: Mapping[str, str]) -> str:
    label = pick(field.label, locale)
    hint = pick(field.description, locale) if field.description else ""
    error = errors.get(field.key, "")
    css = "field-error" if error else ""
    parts = [f'<div class="{css}">' if css else "<div>"]
    parts.append(f'<label for="f-{escape(field.key)}">{escape(label)}</label>')
    if hint:
        parts.append(f'<div class="hint">{escape(hint)}</div>')
    value = values.get(field.key)

    if field.kind == FIELD_SHORT:
        parts.append(
            f'<input id="f-{escape(field.key)}" type="text" name="{escape(field.key)}"'
            f' maxlength="{SHORT_MAX}" value="{escape(str(value or ""))}"'
            f'{" required" if field.required else ""}>'
        )
    elif field.kind == FIELD_LONG:
        parts.append(
            f'<textarea id="f-{escape(field.key)}" name="{escape(field.key)}" maxlength="{LONG_MAX}"'
            f'{" required" if field.required else ""}>{escape(str(value or ""))}</textarea>'
        )
    elif field.kind == FIELD_CHOICE:
        selected = str(value or "")
        options = []
        for option in field.options:
            option_label = pick(option.label, locale)
            checked = " checked" if option.value == selected else ""
            options.append(
                f'<label><input type="radio" name="{escape(field.key)}" value="{escape(option.value)}"'
                f'{checked}{" required" if field.required else ""}> {escape(option_label)}</label>'
            )
        parts.append("<fieldset>" + "".join(options) + "</fieldset>")
    elif field.kind == FIELD_MULTI:
        selected_values = {str(item) for item in value} if isinstance(value, list) else set()
        options = []
        for option in field.options:
            option_label = pick(option.label, locale)
            checked = " checked" if option.value in selected_values else ""
            options.append(
                f'<label><input type="checkbox" name="{escape(field.key)}" value="{escape(option.value)}"'
                f"{checked}> {escape(option_label)}</label>"
            )
        parts.append("<fieldset>" + "".join(options) + "</fieldset>")
    elif field.kind == FIELD_FILE:
        multiple = " multiple" if field.max_files > 1 else ""
        parts.append(
            f'<input id="f-{escape(field.key)}" type="file" name="{escape(field.key)}"{multiple}'
            f'{" required" if field.required else ""}>'
        )
        parts.append(
            f'<div class="hint">Max {field.max_files} file(s), 8 MB each, 24 MB total.</div>'
        )
        if isinstance(value, list) and value:
            names = ", ".join(str(item.get("name", "")) for item in value if isinstance(item, dict))
            if names:
                parts.append(f'<div class="hint">Uploaded: {escape(names)}</div>')

    if error:
        parts.append(f'<div class="error">{escape(error)}</div>')
    parts.append("</div>")
    return "\n".join(parts)


def parse_answers(
    definition: FormDefinition,
    *,
    text: Mapping[str, str],
    multi: Mapping[str, Sequence[str]],
    files: Mapping[str, Sequence[UploadedFile]],
) -> ParsedAnswers:
    answers: dict[str, Any] = {}
    errors: dict[str, str] = {}
    kept_files: dict[str, list[UploadedFile]] = {}
    total_bytes = 0

    for field in definition.fields:
        if field.kind == FIELD_SHORT:
            value = (text.get(field.key) or "").strip()
            _check_required(field, value, errors)
            if len(value) > SHORT_MAX:
                errors[field.key] = f"At most {SHORT_MAX} characters."
            answers[field.key] = value
        elif field.kind == FIELD_LONG:
            value = (text.get(field.key) or "").strip()
            _check_required(field, value, errors)
            if len(value) > LONG_MAX:
                errors[field.key] = f"At most {LONG_MAX} characters."
            answers[field.key] = value
        elif field.kind == FIELD_CHOICE:
            value = (text.get(field.key) or "").strip()
            _check_required(field, value, errors)
            if value and field.option(value) is None:
                errors[field.key] = "Invalid option."
            answers[field.key] = value
        elif field.kind == FIELD_MULTI:
            selected = [value for value in multi.get(field.key, ()) if value]
            if field.required and not selected:
                errors[field.key] = "Pick at least one option."
            valid = {option.value for option in field.options}
            if any(value not in valid for value in selected):
                errors[field.key] = "Invalid option."
            answers[field.key] = selected
        elif field.kind == FIELD_FILE:
            uploads = list(files.get(field.key, ()))
            if field.required and not uploads:
                errors[field.key] = "Attach at least one file."
            if len(uploads) > field.max_files:
                errors[field.key] = f"At most {field.max_files} file(s)."
            for upload in uploads:
                if len(upload.data) > MAX_FILE_BYTES:
                    errors[field.key] = f"{upload.name} is too large (8 MB each)."
                    break
                total_bytes += len(upload.data)
            if total_bytes > MAX_TOTAL_BYTES:
                errors[field.key] = "Attachments are too large in total (24 MB)."
            kept_files[field.key] = uploads
            answers[field.key] = [
                {"name": upload.name, "size": len(upload.data), "type": upload.content_type}
                for upload in uploads
            ]

    return ParsedAnswers(answers=answers, errors=errors, files=kept_files)


def _check_required(field: Any, value: str, errors: dict[str, str]) -> None:
    if field.required and not value:
        errors[field.key] = "This field is required."
