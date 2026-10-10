"""表单定义：从数据目录里的 ``*.json`` 读出来的结构、解析与校验。

表单是**数据**，而且**不进 git**：定义放在数据目录（默认 ``data/forms/``，与数据库、附件同一个卷），
一个文件一个表单，文件名必须等于 ``id``。**文字也写在同一个文件里**（不是 locales 里的键），
所以加一种表单 = 把一个 json 拷过去 + ``/module reload forms``，仓库里不会出现你的表单内容。

```json
{
  "id": "player_survey",
  "mode": "collect",
  "title": {"en-US": "Player survey", "zh-CN": "玩家问卷"},
  "description": {"en-US": "A few questions.", "zh-CN": "几个问题。"},
  "allow_multiple": false,
  "actions": ["grant_role", "notify_channel"],
  "fields": [
    {"key": "github", "kind": "choice",
     "label": {"en-US": "Do you have a GitHub account?", "zh-CN": "你有 GitHub 账号吗？"},
     "options": [
       {"value": "yes", "label": {"en-US": "Yes", "zh-CN": "有"}},
       {"value": "no", "label": {"en-US": "No", "zh-CN": "没有"}}
     ]},
    {"key": "samples", "kind": "file", "label": {"en-US": "Samples"}, "max_files": 3, "required": false}
  ]
}
```

- ``mode``：``apply`` 申请（走审核）／``collect`` 收集／``signup`` 报名（占名额）。
- ``actions`` 只有申请类能用；``allow_multiple`` 只有收集与报名能用。
- 字段键：``key`` / ``kind`` / ``label`` / ``description``（可选）/ ``required``（默认 true）；
  选项类多一个 ``options``（``value`` + ``label``），附件类多一个 ``max_files``。
- **未知键一律报错**：拼错一个键被静默忽略，比直接报错难查得多。
- 文字是按语言标签的对象，至少写一种；请求的语言没有就回落到**文件里第一个**。
  长度超限也在这里报错（Discord 的限制是硬的）：标题与字段名 45、副标题 100、选项名 100。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("bot.forms")

# 字段类型
FIELD_SHORT = "short"
FIELD_LONG = "long"
FIELD_CHOICE = "choice"
FIELD_MULTI = "multi"
FIELD_FILE = "file"
FIELD_KINDS = (FIELD_SHORT, FIELD_LONG, FIELD_CHOICE, FIELD_MULTI, FIELD_FILE)

# 用途：申请要走审核；收集与报名提交即完成，报名还会占名额。
MODE_APPLY = "apply"
MODE_COLLECT = "collect"
MODE_SIGNUP = "signup"
MODES = (MODE_APPLY, MODE_COLLECT, MODE_SIGNUP)

# 通过后动作。空数组表示只留档，什么都不做。
ACTION_GRANT_ROLE = "grant_role"
ACTION_NOTIFY_CHANNEL = "notify_channel"
ACTIONS = (ACTION_GRANT_ROLE, ACTION_NOTIFY_CHANNEL)

STEP_SIZE = 5
MAX_OPTIONS = 10
MAX_FILES = 10
LONG_TEXT_MAX = 4000
SHORT_TEXT_MAX = 200
# 附件上限：Discord 自己的上限随服务器等级变化，这里只挡住「把机器人磁盘塞满」这种情况。
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024

# Discord 的硬限制：弹窗里字段名最多 45 字符、副标题 100，下拉选项名 100。
LABEL_LIMIT = 45
DESCRIPTION_LIMIT = 100
OPTION_LIMIT = 100
FORM_DESCRIPTION_LIMIT = 500

FORM_KEYS = frozenset({"id", "mode", "title", "description", "fields", "actions", "allow_multiple"})
FORM_REQUIRED_KEYS = frozenset({"id", "mode", "title", "description", "fields"})
FIELD_KEYS = frozenset({"key", "kind", "label", "description", "required", "options", "max_files"})
FIELD_REQUIRED_KEYS = frozenset({"key", "kind", "label"})
OPTION_KEYS = frozenset({"value", "label"})

# ``语言标签 -> 文案``。对象保持插入顺序，所以第一条就是回落项。
Localized = dict[str, str]


class DefinitionError(ValueError):
    """定义文件有问题。消息里带文件名与位置，照着改就行。"""


def supported_locales(core_locales_dir: Path | str | None = None) -> tuple[str, ...]:
    """机器人支持的界面语言标签：核心 ``locales/*.json`` 的文件名。

    表单里的语言标签必须在这份清单里，否则「zh」这种写法会被当成另一种语言静默回落。
    """
    directory = Path(core_locales_dir) if core_locales_dir else Path(__file__).resolve().parents[2] / "locales"
    if not directory.is_dir():
        return ()
    return tuple(sorted(path.stem for path in directory.glob("*.json")))


def pick(value: Localized, locale: str) -> str:
    """取某个语言的文案；没有就回落到文件里写的第一条。"""
    if locale in value:
        return value[locale]
    return next(iter(value.values()))


@dataclass(frozen=True)
class Option:
    value: str
    label: Localized


@dataclass(frozen=True)
class Field:
    key: str
    kind: str
    label: Localized
    required: bool = True
    options: tuple[Option, ...] = ()
    max_files: int = 1
    description: Localized | None = None

    @property
    def has_options(self) -> bool:
        return self.kind in (FIELD_CHOICE, FIELD_MULTI)

    def option(self, value: str) -> Option | None:
        return next((item for item in self.options if item.value == value), None)


@dataclass(frozen=True)
class FormDefinition:
    id: str
    mode: str
    title: Localized
    description: Localized
    fields: tuple[Field, ...]
    actions: tuple[str, ...] = ()
    # 收集类默认一人一份；允许重复提交的表单（例如多条建议）把它设成 true。
    allow_multiple: bool = False

    @property
    def is_apply(self) -> bool:
        return self.mode == MODE_APPLY

    @property
    def is_signup(self) -> bool:
        return self.mode == MODE_SIGNUP

    @property
    def steps(self) -> tuple[tuple[Field, ...], ...]:
        """按 5 个一步切分 —— 一个弹窗放不下第 6 个组件。"""
        chunks = [self.fields[index : index + STEP_SIZE] for index in range(0, len(self.fields), STEP_SIZE)]
        return tuple(chunks)

    @property
    def step_count(self) -> int:
        return len(self.steps)

    def field(self, key: str) -> Field | None:
        return next((item for item in self.fields if item.key == key), None)

    def grants_role(self) -> bool:
        return ACTION_GRANT_ROLE in self.actions

    def notifies_channel(self) -> bool:
        return ACTION_NOTIFY_CHANNEL in self.actions


@dataclass(frozen=True)
class FormCatalog:
    """一次装载的结果。目录为空就是空目录 —— 「还没配表单」不是错误。"""

    definitions: tuple[FormDefinition, ...] = ()

    def all(self) -> tuple[FormDefinition, ...]:
        return self.definitions

    def ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.definitions)

    def get(self, form_id: str) -> FormDefinition | None:
        return next((item for item in self.definitions if item.id == form_id), None)


# ---------------------------------------------------------------------- 解析


def parse_definition(raw: Any, *, source: str, locales: tuple[str, ...] = ()) -> FormDefinition:
    """把一份 JSON 变成 :class:`FormDefinition`。形状不对就抛 :class:`DefinitionError`。"""
    if not isinstance(raw, dict):
        raise DefinitionError(f"{source}：顶层必须是一个对象")
    unknown = sorted(set(raw) - FORM_KEYS)
    if unknown:
        raise DefinitionError(f"{source}：未知键 {unknown}（可用：{sorted(FORM_KEYS)}）")
    missing = sorted(FORM_REQUIRED_KEYS - set(raw))
    if missing:
        raise DefinitionError(f"{source}：缺少键 {missing}")

    form_id = _require_str(raw, "id", source)
    if Path(source).stem != form_id:
        raise DefinitionError(f"{source}：文件名必须等于 id（现在是 {form_id}）")

    raw_fields = raw["fields"]
    if not isinstance(raw_fields, list) or not raw_fields:
        raise DefinitionError(f"{source}：fields 必须是非空数组")
    fields = tuple(parse_field(item, source=source, locales=locales) for item in raw_fields)

    actions = raw.get("actions", [])
    if not isinstance(actions, list) or not all(isinstance(action, str) for action in actions):
        raise DefinitionError(f"{source}：actions 必须是字符串数组")
    allow_multiple = raw.get("allow_multiple", False)
    if not isinstance(allow_multiple, bool):
        raise DefinitionError(f"{source}：allow_multiple 必须是 true 或 false")

    return FormDefinition(
        id=form_id,
        mode=_require_str(raw, "mode", source),
        title=_text(raw, "title", source, limit=LABEL_LIMIT, locales=locales),
        description=_text(raw, "description", source, limit=FORM_DESCRIPTION_LIMIT, locales=locales),
        fields=fields,
        actions=tuple(actions),
        allow_multiple=allow_multiple,
    )


def parse_field(raw: Any, *, source: str, locales: tuple[str, ...] = ()) -> Field:
    if not isinstance(raw, dict):
        raise DefinitionError(f"{source}：fields 里每一项都必须是对象")
    unknown = sorted(set(raw) - FIELD_KEYS)
    if unknown:
        raise DefinitionError(f"{source}：字段里有未知键 {unknown}（可用：{sorted(FIELD_KEYS)}）")
    missing = sorted(FIELD_REQUIRED_KEYS - set(raw))
    if missing:
        raise DefinitionError(f"{source}：字段缺少键 {missing}")

    key = _require_str(raw, "key", source)
    kind = _require_str(raw, "kind", source)
    if kind not in FIELD_KINDS:
        raise DefinitionError(f"{source}：字段 {key} 的类型非法 {kind!r}（可用：{list(FIELD_KINDS)}）")

    raw_options = raw.get("options", [])
    if not isinstance(raw_options, list):
        raise DefinitionError(f"{source}：字段 {key} 的 options 必须是数组")
    if kind in (FIELD_CHOICE, FIELD_MULTI):
        options = tuple(parse_option(item, source=source, field_key=key, locales=locales) for item in raw_options)
    elif raw_options:
        raise DefinitionError(f"{source}：字段 {key} 是 {kind}，不该有 options")
    else:
        options = ()

    required = raw.get("required", True)
    if not isinstance(required, bool):
        raise DefinitionError(f"{source}：字段 {key} 的 required 必须是 true 或 false")
    max_files = raw.get("max_files", 1)
    if not isinstance(max_files, int) or isinstance(max_files, bool):
        raise DefinitionError(f"{source}：字段 {key} 的 max_files 必须是整数")
    description = None
    if "description" in raw:
        description = _text(raw, "description", source, limit=DESCRIPTION_LIMIT, locales=locales, where=key)

    return Field(
        key=key,
        kind=kind,
        label=_text(raw, "label", source, limit=LABEL_LIMIT, locales=locales, where=key),
        required=required,
        options=options,
        max_files=max_files,
        description=description,
    )


def parse_option(raw: Any, *, source: str, field_key: str, locales: tuple[str, ...] = ()) -> Option:
    if not isinstance(raw, dict):
        raise DefinitionError(f"{source}：字段 {field_key} 的选项必须是对象")
    unknown = sorted(set(raw) - OPTION_KEYS)
    if unknown:
        raise DefinitionError(f"{source}：字段 {field_key} 的选项里有未知键 {unknown}（可用：{sorted(OPTION_KEYS)}）")
    missing = sorted(OPTION_KEYS - set(raw))
    if missing:
        raise DefinitionError(f"{source}：字段 {field_key} 的选项缺少键 {missing}")
    return Option(
        value=_require_str(raw, "value", source),
        label=_text(raw, "label", source, limit=OPTION_LIMIT, locales=locales, where=f"{field_key}/{raw.get('value')}"),
    )


def _text(
    raw: dict[str, Any],
    key: str,
    source: str,
    *,
    limit: int,
    locales: tuple[str, ...],
    where: str = "",
) -> Localized:
    """解析 ``{"en-US": "…", "zh-CN": "…"}``：至少一种语言、标签要在支持清单里、长度不超限。"""
    prefix = f"{source}：{where} 的 {key}" if where else f"{source}：{key}"
    value = raw.get(key)
    if not isinstance(value, dict) or not value:
        raise DefinitionError(f"{prefix} 必须是「语言标签 -> 文案」的对象，且至少写一种语言")
    parsed: Localized = {}
    for tag, text in value.items():
        if locales and tag not in locales:
            raise DefinitionError(f"{prefix} 的语言标签 {tag!r} 不在支持的语言里（{list(locales)}）")
        if not isinstance(text, str) or not text.strip():
            raise DefinitionError(f"{prefix}（{tag}）必须是非空字符串")
        if len(text) > limit:
            raise DefinitionError(f"{prefix}（{tag}）有 {len(text)} 个字符，超过 Discord 的 {limit} 上限")
        parsed[tag] = text.strip()
    return parsed


def _require_str(raw: dict[str, Any], key: str, source: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DefinitionError(f"{source}：{key} 必须是非空字符串")
    return value.strip()


# ---------------------------------------------------------------------- 装载


def load_catalog(
    directory: Path | str,
    *,
    locales: tuple[str, ...] | None = None,
    logger: logging.Logger | None = None,
) -> FormCatalog:
    """读一个目录下所有 ``*.json``。

    目录不存在、或者一个文件都没有 = 空目录（「还没配表单」是正常状态，不是错误）。
    但只要目录里有文件，**任何一个坏了都整体报错**，不静默少一个表单。
    """
    log = logger or LOGGER
    root = Path(directory)
    tags = supported_locales() if locales is None else locales
    if not root.is_dir():
        log.info("表单定义目录还不存在（%s）：目前没有任何表单", root)
        return FormCatalog()
    paths = sorted(root.glob("*.json"))
    if not paths:
        log.info("表单定义目录里没有 *.json（%s）：目前没有任何表单", root)
        return FormCatalog()

    definitions: list[FormDefinition] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DefinitionError(f"{path.name}：读不出来（{exc}）") from exc
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DefinitionError(f"{path.name}：不是合法 JSON（第 {exc.lineno} 行：{exc.msg}）") from exc
        definitions.append(parse_definition(raw, source=path.name, locales=tags))

    result = FormCatalog(tuple(definitions))
    _validate(result.definitions)
    log.info("已加载 %d 个表单定义：%s（目录 %s）", len(result.definitions), list(result.ids()), root)
    return result


def _validate(definitions: tuple[FormDefinition, ...]) -> None:
    """跨表单与语义上的规则。形状（类型、未知键、长度）在解析时就挡住了。"""
    seen: set[str] = set()
    for definition in definitions:
        if not definition.id or not definition.id.replace("_", "").isalnum():
            raise DefinitionError(f"表单 id 非法：{definition.id!r}（只允许字母、数字与下划线）")
        if definition.id in seen:
            raise DefinitionError(f"表单 id 重复：{definition.id}")
        seen.add(definition.id)
        if definition.mode not in MODES:
            raise DefinitionError(f"{definition.id}：用途非法 {definition.mode!r}，只能是 {MODES}")
        if not definition.fields:
            raise DefinitionError(f"{definition.id}：至少要有一个字段")
        keys: set[str] = set()
        for item in definition.fields:
            if not item.key or item.key in keys:
                raise DefinitionError(f"{definition.id}：字段 key 缺失或重复：{item.key!r}")
            keys.add(item.key)
            if item.has_options and not 2 <= len({option.value for option in item.options}) <= MAX_OPTIONS:
                raise DefinitionError(f"{definition.id}.{item.key}：选项要有 2–{MAX_OPTIONS} 个且 value 不重复")
            if item.kind == FIELD_FILE and not 1 <= item.max_files <= MAX_FILES:
                raise DefinitionError(f"{definition.id}.{item.key}：附件数量要在 1–{MAX_FILES} 之间")
        unknown = [action for action in definition.actions if action not in ACTIONS]
        if unknown:
            raise DefinitionError(f"{definition.id}：未知的通过后动作 {unknown}")
        if definition.actions and not definition.is_apply:
            raise DefinitionError(f"{definition.id}：只有申请类才有通过后动作")
        if definition.allow_multiple and definition.is_apply:
            raise DefinitionError(f"{definition.id}：申请类必须一人一份")
