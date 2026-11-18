"""个人信息卡片图（积分 / 全连率 / 全国排名 / 金币 / 游玩次数 / 天梯）。

复用 dc_image 的配色与绘制工具，输出与战力分析图同一风格。
"""
from PIL import Image, ImageDraw

from .image import (
    W,
    draw_footer,
    draw_header,
    draw_history,
    draw_stats,
    fmt_int,
    gradient_bg,
)

LADDER_NAMES = ["未定级", "一星", "二星", "三星", "星钻", "表神", "里神"]


def _ladder_name(grade):
    try:
        g = int(grade)
    except (TypeError, ValueError):
        return "-"
    if 0 <= g < len(LADDER_NAMES):
        return LADDER_NAMES[g]
    return "-"


def build_info(player):
    """生成个人信息卡片（PIL.Image）。"""
    info = player.info()
    acc = player.account() or {}
    items = {
        i.get("ReplyTextItemID"): i.get("Content")
        for i in (player.stats_items() or [])
        if isinstance(i, dict)
    }
    hist = player.history() or []
    try:
        grade, point = player.ladder()
    except Exception:  # noqa: BLE001
        grade, point = None, None

    stat_h = 84
    gap = 16
    y1 = 196
    y2 = y1 + stat_h + gap
    hist_y = y2 + stat_h + 12
    hist_h = 286
    H = hist_y + hist_h + 86

    img = gradient_bg(W, H)
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)

    draw_header(img, d, info)

    draw_stats(
        d,
        y1,
        [
            ("积分", fmt_int(info.get("MusicScore"))),
            ("全连率", "%.2f%%" % (float(info.get("ComboPercent") or 0) / 100)),
            ("全国排名", fmt_int(info.get("RankNation"))),
            ("金币", fmt_int(acc.get("Gold"))),
            # ReplyTextItemID=5 是官方「游玩次数」的固定项，接口只给 ID + 数值
            ("游玩次数", items.get(5) or "-"),
        ],
    )
    draw_stats(
        d,
        y2,
        [
            ("天梯段位", _ladder_name(grade)),
            ("天梯分", fmt_int(point) if point is not None else "-"),
            ("战力", fmt_int(info.get("LvRatio"))),
            ("城市", info.get("CityName") or "-"),
            ("战队", info.get("TeamName") or "无"),
        ],
    )

    draw_history(d, hist_y, hist_h, hist)

    draw_footer(d, H)
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
