# -*- coding: utf-8 -*-
"""输出安全：表格单元格净化、异常文案脱敏。"""

# Excel / WPS 会把以这些字符开头的文本当公式（CWE-1236）
_INJECT_PREFIX = ("=", "+", "-", "@", "\t", "\r")

# 回显给用户时不带路径与内部地址的异常
_SENSITIVE_HINTS = ("\\", "/", "token", "access_token")


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
    """把异常压成一句可回显的话，避免把本机路径塞进群聊。"""
    msg = "%s: %s" % (type(exc).__name__, exc)
    msg = msg.replace("\\", "/")
    return msg[:limit]
