# -*- coding: utf-8 -*-
"""曲库全表导出：官谱全表 / 自制谱全表（xlsx）。

`/曲目 官谱全表` 与 `/曲目 自制谱全表` 都走这里。表结构照旧版离线表格：
  官谱：音乐ID / 曲名 / 时长 / 难度 / 定数 / 物量
  自制谱：音乐ID / 曲名 / 时长 / 作者ID / 作者 / 标签 / 价格 / 点赞 / 异常 /
          难度1-3 / 定数1-3 / 物量1-3 / show+难度 / show+定数

生成结果由调用方按 TTL 缓存，短时间内重复请求直接复用文件。
"""
import os

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .music import level_label

HEADER_FILL = PatternFill("solid", fgColor="1F3B4D")
HEADER_FONT = Font(color="FFFFFF", bold=True)
CENTER = Alignment(horizontal="center", vertical="center")
LEFT = Alignment(horizontal="left", vertical="center")
RIGHT = Alignment(horizontal="right", vertical="center")

OFFICIAL_HEADERS = ["音乐ID", "曲名", "曲风", "时长", "难度", "定数", "物量"]

# 曲风：MusicCategoryNew 1 国语 / 3 韩文 / 4 欧美 / 6 其他
GENRE_NAMES = {1: "国语", 3: "韩文", 4: "欧美", 6: "其他"}

# 官谱排序辅键：定数相同按 高 → 中 → 初 → 秀 → S+
TAG_ORDER = {"[高]": 0, "[中]": 1, "[初]": 2, "[秀]": 3, "[S+]": 4}
USER_HEADERS = [
    "音乐ID", "曲名", "时长", "作者ID", "作者", "标签", "价格(金币)", "点赞", "异常",
    "难度1", "定数1", "物量1", "难度2", "定数2", "物量2", "难度3", "定数3", "物量3",
    "show+难度", "show+定数",
]

_NUMERIC = {
    "音乐ID", "定数", "物量", "作者ID", "价格(金币)", "点赞",
    "定数1", "物量1", "定数2", "物量2", "定数3", "物量3", "show+定数",
}


def _disp_width(value):
    """显示宽度：中日韩等宽字符按 2 计。"""
    w = 0
    for ch in str(value):
        w += 2 if ord(ch) > 0x2E7F else 1
    return w


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def mmss(seconds):
    """秒 -> M:SS；没有时长返回空串。"""
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    return "%d:%02d" % (s // 60, s % 60)


def _finish(ws, headers, path):
    """统一样式：表头、数字右对齐、自适应列宽、冻结首行、筛选。"""
    for cell in ws[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = CENTER
    for col_idx, header in enumerate(headers, 1):
        letter = get_column_letter(col_idx)
        align = RIGHT if header in _NUMERIC else LEFT
        for cell in ws[letter][1:]:
            cell.alignment = align

    widths = []
    for col_idx, header in enumerate(headers, 1):
        w_data = 0
        for cell in ws[get_column_letter(col_idx)][1:]:
            w_data = max(w_data, _disp_width(cell.value if cell.value is not None else ""))
        widths.append(min(max(_disp_width(header) + 5, w_data + 3), 60))
    for col_idx, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = w

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    ws.row_dimensions[1].height = 22

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    ws.parent.save(path)


def build_official(lib, path):
    """生成官谱全表，每个谱面一行。

    排序：定数倒序为主，同定数按 高 → 中 → 初 → 秀 → S+ 为辅，再按音乐 ID。
    返回 (路径, 谱面行数, 曲目数)。
    """
    songs = lib._attach_duration(list(lib.load_official()))
    rows = []
    for r in songs:
        mid = r.get("musicId")
        name = r.get("name") or ""
        genre = GENRE_NAMES.get(_int(r.get("categoryNew")), "其他")
        dur = mmss(r.get("duration"))
        levels = r.get("levels") or []
        if not levels:
            rows.append([mid, name, genre, dur, "未填", "", "", 0, 9, _int(mid)])
            continue
        for lv in levels:
            if lv.get("level") is not None and lv["level"] < 0 and not lv.get("isShow"):
                continue
            label = level_label(lv)
            if lv.get("isBad"):
                label = "⚠️ " + label
            clean = label.replace("⚠️ ", "")
            tag_rank = next((v for k, v in TAG_ORDER.items() if clean.startswith(k)), 9)
            level = lv.get("level")
            level_num = level if isinstance(level, int) and level > 0 else 0
            rows.append([
                mid,
                name,
                genre,
                dur,
                label,
                level if level is not None else "",
                lv.get("combo") or "",
                level_num,
                tag_rank,
                _int(mid),
            ])
    rows.sort(key=lambda x: (-x[7], x[8], x[9]))

    wb = Workbook()
    ws = wb.active
    ws.title = "官谱全表"
    ws.append(OFFICIAL_HEADERS)
    for row in rows:
        ws.append(row[:7])
    _finish(ws, OFFICIAL_HEADERS, path)
    return path, len(rows), len(songs)


def build_user(lib, path):
    """生成自制谱全表，每个商品一行，按点赞倒序。返回 (路径, 行数)。"""
    items = lib._attach_duration(list(lib.load_user()))
    # 点赞倒序；同赞按音乐 ID 升序，保证每次生成顺序稳定
    items.sort(key=lambda r: (-int(r.get("like") or 0), int(r.get("musicId") or 0)))
    wb = Workbook()
    ws = wb.active
    ws.title = "自制谱商店"
    ws.append(USER_HEADERS)
    for r in items:
        levels = r.get("levels") or []
        classic = [lv for lv in levels if not lv.get("isShow")]
        classic.sort(key=lambda x: (x.get("order", 9), x.get("levNew") or 0))
        while len(classic) < 3:
            classic.append(None)
        row = [
            r.get("musicId"),
            r.get("name") or "",
            mmss(r.get("duration")),
            r.get("ownerId") or "",
            r.get("ownerName") or "",
            " / ".join(r.get("tags") or []),
            r.get("price") if r.get("price") is not None else "",
            r.get("like") if r.get("like") is not None else "",
            "⚠️" if any(lv.get("isBad") for lv in levels) else "",
        ]
        for lv in classic[:3]:
            if lv is None:
                row.extend(["", "", ""])
            else:
                row.extend([
                    level_label(lv),
                    lv.get("level") if lv.get("level") is not None else "",
                    lv.get("combo") or "",
                ])
        show = next((lv for lv in levels if lv.get("isShow")), None)
        if show is None:
            row.extend(["", ""])
        else:
            row.extend([level_label(show), show.get("level") if show.get("level") is not None else ""])
        ws.append(row)
    _finish(ws, USER_HEADERS, path)
    return path, max(0, ws.max_row - 1)
