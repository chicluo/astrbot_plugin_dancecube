"""把舞立方全部成绩导出成 xlsx（自适应列宽）。

文件名固定为「<角色ID>全部成绩.xlsx」，保存到管理员本机目录。
列：序号 / 曲名 / MusicID / 定数 / 难度 / 曲风 / ACC / 分数 / Miss / Combo / 战力 / 评级 / 游玩时间 / 来源
"""
import os
import re
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .safe import safe_cell

DEFAULT_EXPORT_DIR = ""

HEADERS = [
    "序号", "曲名", "MusicID", "定数", "难度", "曲风", "ACC",
    "分数", "Miss", "Combo", "战力", "评级", "游玩时间", "来源",
]

HEADER_FILL = PatternFill("solid", fgColor="1F3B4D")
HEADER_FONT = Font(color="FFFFFF", bold=True)
CENTER = Alignment(horizontal="center", vertical="center")
LEFT = Alignment(horizontal="left", vertical="center")
RIGHT = Alignment(horizontal="right", vertical="center")


def safe_filename(name):
    """去掉 Windows 文件名非法字符。"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name or "")).strip(" .")
    return name or "unknown"


def _disp_width(value):
    """显示宽度：中日韩等宽字符按 2 计。"""
    w = 0
    for ch in str(value):
        w += 2 if ord(ch) > 0x2E7F else 1
    return w


def export_scores(player, out_dir=DEFAULT_EXPORT_DIR):
    """生成 xlsx，返回 (路径, 行数, 玩家名, 角色ID)。文件名「<角色ID>全部成绩<时间>.xlsx」。"""
    if not out_dir or not str(out_dir).strip():
        raise RuntimeError("导出目录未指定")
    scores = player.scores() or []
    if not scores:
        raise RuntimeError("没有可导出的成绩")

    info = player.info() or {}
    player_name = info.get("UserName") or "舞立方玩家"
    role_id = str(info.get("UserID") or player.uid or "unknown")

    rows = sorted(
        scores,
        key=lambda s: (-int(s.get("level") or 0), -int(s.get("rating") or 0), s.get("name") or ""),
    )

    wb = Workbook()
    ws = wb.active
    ws.title = "全部成绩"
    ws.append(HEADERS)

    for idx, s in enumerate(rows, 1):
        ws.append([
            idx,
            safe_cell(s.get("name") or ""),
            s.get("musicId"),
            int(s.get("level") or 0),
            safe_cell(s.get("levelTypeName") or ""),
            safe_cell(s.get("clsName") or ""),
            round(float(s.get("acc") or 0), 2),
            int(s.get("score") or 0),
            int(s.get("miss") or 0),
            int(s.get("combo") or 0),
            int(s.get("rating") or 0),
            safe_cell(s.get("grade") or ""),
            safe_cell(s.get("time") or ""),
            "官谱" if int(s.get("ownerType") or 0) == 1 else "自制谱",
        ])

    # 表头样式
    for cell in ws[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = CENTER

    # 列格式
    numeric_cols = {"序号", "定数", "ACC", "分数", "Miss", "Combo", "战力"}
    for col_idx, header in enumerate(HEADERS, 1):
        letter = get_column_letter(col_idx)
        if header in numeric_cols:
            for cell in ws[letter][1:]:
                cell.alignment = RIGHT
        else:
            for cell in ws[letter][1:]:
                cell.alignment = LEFT

    acc_col = get_column_letter(HEADERS.index("ACC") + 1)
    for cell in ws[acc_col][1:]:
        cell.number_format = '0.00"%"'

    # 自适应列宽：表头预留筛选下拉箭头的空间，数据留少量内边距
    widths = []
    for col_idx, header in enumerate(HEADERS, 1):
        w_data = 0
        for cell in ws[get_column_letter(col_idx)][1:]:
            w_data = max(w_data, _disp_width(cell.value if cell.value is not None else ""))
        w_head = _disp_width(header) + 5
        widths.append(min(max(w_head, w_data + 3), 60))
    for col_idx, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = w

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    ws.row_dimensions[1].height = 22

    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, "%s全部成绩%s.xlsx" % (safe_filename(role_id), stamp))

    wb.save(path)
    return path, len(rows), player_name, role_id
