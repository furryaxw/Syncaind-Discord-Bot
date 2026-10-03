"""release 正文的清理：机器标记不该出现在 Discord 里。

作者的发布说明里嵌着 `<!-- sp-compat {...} -->` 这种给管理器读的标记。
GitHub 页面不渲染 HTML 注释，所以那边看不见；**而 Discord 不渲染 HTML**，于是它变成明文，
下面是兼容矩阵 JSON —— 用户看到的就是这个。
"""

from __future__ import annotations

from bot.modules.github_feed.format import clean_notes

SP_COMPAT = '<!-- sp-compat {"hamish.sprocket": ["0.2.53.x", "0.2.55.5"], "lavagang.melonloader": ["0.7.3"]} -->'


def test_the_real_marker_is_removed() -> None:
    body = f"## 变更\n- 修了 A\n\n{SP_COMPAT}\n"

    cleaned = clean_notes(body)

    assert "sp-compat" not in cleaned
    assert cleaned == "## 变更\n- 修了 A"


def test_an_inline_marker_is_removed_without_eating_the_text() -> None:
    cleaned = clean_notes(f"修了 A {SP_COMPAT} 顺便修了 B")

    assert "sp-compat" not in cleaned
    assert cleaned.startswith("修了 A") and cleaned.endswith("顺便修了 B")


def test_multiple_markers_are_all_removed() -> None:
    cleaned = clean_notes(f"{SP_COMPAT}\n正文\n<!-- sp-deps abc -->\n结尾")

    assert cleaned == "正文\n\n结尾"


def test_a_multiline_marker_is_removed() -> None:
    body = '开头\n<!-- sp-compat {\n  "a.b": ["1.0"]\n} -->\n结尾'

    assert clean_notes(body) == "开头\n\n结尾"


def test_removing_a_marker_never_merges_two_lines() -> None:
    """标记独占一行时，删掉它只留下一个空行，**绝不把上下两行并成一行**。

    这是刻意的：真实发布说明里标记常夹在列表项之间，
    一旦并成一行，markdown 的列表结构就断了 —— 少一个空行远不如破掉排版严重。
    """
    body = "- 第一项\n<!-- sp-compat {} -->\n- 第二项"

    assert clean_notes(body) == "- 第一项\n\n- 第二项"


def test_blank_lines_left_by_a_marker_are_collapsed() -> None:
    body = f"段落一\n\n{SP_COMPAT}\n\n段落二"

    assert clean_notes(body) == "段落一\n\n段落二"


def test_windows_line_endings_are_normalised() -> None:
    assert clean_notes(f"第一行\r\n{SP_COMPAT}\r\n第二行") == "第一行\n\n第二行"


def test_a_body_that_is_only_a_marker_becomes_empty() -> None:
    """清完就没内容了 —— 上层会走「这个版本没有填写说明」，而不是显示一坨 JSON。"""
    assert clean_notes(SP_COMPAT) == ""


def test_an_unclosed_marker_is_left_alone() -> None:
    """没闭合的 `<!--` 宁可留着明文：少隐藏一点，比悄悄吞掉半篇说明好。"""
    body = "正文\n<!-- sp-compat { 没闭合"

    assert clean_notes(body) == body


def test_a_body_without_markers_is_untouched() -> None:
    body = "## 变更\n\n- 修了 A\n- 加了 B"

    assert clean_notes(body) == body
