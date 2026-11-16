# -*- coding: utf-8 -*-
"""输出安全：表格单元格净化、异常文案脱敏。"""
import re

# Excel / WPS 会把以这些字符开头的文本当公式（CWE-1236）
_INJECT_PREFIX = ("=", "+", "-", "@", "\t", "\r")

# 绝对路径：E:/a/b/c、/home/x/y、core/out/musiclib.json 之类
_PATH_RE = re.compile(r"(?:[A-Za-z]:)?[/\\](?:[\w.\-]+[/\\])+[\w.\-]+")
_TOKEN_RE = re.compile(r"(?i)(access_)?token")


def safe_cell(value):
    """xlsx / CSV 写入前的净化；数字原样返回，文本前缀 ' 破坏公式。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return value
    s = str(value)
    if s[:1] in _INJECT_PREFIX:
        return "'" + s
    return s


def safe_error(exc, limit=160):
    """把异常压成一句可回显的话：路径与凭证关键字替换掉，日志里保留原文。"""
    msg = "%s: %s" % (type(exc).__name__, exc)
    msg = _PATH_RE.sub("[路径]", msg)  # 先吃路径，避免 token.json 被拆成 token.[路径]
    msg = _TOKEN_RE.sub("[凭证]", msg)
    return msg[:limit]
