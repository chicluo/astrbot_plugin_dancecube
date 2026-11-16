#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
舞立方官方接口 —— 纯本地客户端（不经过任何第三方站点）

数据源（全部直连 https://dancedemo.shenghuayule.com）：
  GetInfo             个人信息（战力/积分/排名/段位/隐私标记）
  GetLvRatioHistory   历史战力（每次变动一条，可画曲线）
  GetMyRankNew        全量成绩（musicIndex=2..6 是曲风分类，不是分页！）
  GetLastPlay         最近游玩 30 首
  GetAccountInfo      金币/星币/票券
  ReplyTextItem       游玩次数等统计
  GetValidMusicList   曲库与定数

战力公式（社区实现，见 ref_plugin/utils.py::compute_rating）：
  单曲战力 = f(定数, ACC)  —— 100% 时 (定数+2)*100，按 98/95/90/85/80/75/70 分档线性插值
  总战力   = 最佳 15 首单曲战力的平均值（官方口径有细微差异，见 README）

用法：
  python dc_client.py info
  python dc_client.py b15 [--all]
  python dc_client.py b30 [--all]
  python dc_client.py ap30
  python dc_client.py recent
  python dc_client.py history [--csv]
  python dc_client.py export --out out
"""
import argparse
import csv
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

try:
    from . import net
    from .safe import safe_cell
except ImportError:  # 作为独立脚本运行（python core/client.py）
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import net  # noqa: E402
    from safe import safe_cell  # noqa: E402

BASE = os.environ.get("DC_BASE", "https://dancedemo.shenghuayule.com")
HERE = os.path.dirname(os.path.abspath(__file__))
# 缓存写到临时目录，插件目录只读（容器/只读挂载）时也能用
CACHE_DIR = os.environ.get("DC_CACHE_DIR") or os.path.join(
    tempfile.gettempdir(), "dancecube"
)
# 由插件启动时指到 plugin_data 的曲库快照，和 MusicLib 共用同一份，避免两套缓存
MUSIC_LIB_PATH = None


def _atomic_write_json(path, data):
    """原子写 JSON：先写临时文件再 replace，避免别人读到半截。"""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def set_music_library_path(path):
    global MUSIC_LIB_PATH
    MUSIC_LIB_PATH = str(path) if path else None
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# show+ 谱的等级类型（MusicLevNew 101-105）
SHOW_TYPE = {
    101: "show+-基础", 102: "show+-进阶", 103: "show+-专家",
    104: "show+-大师", 105: "show+-传奇",
}
# 经典谱的「难度等级」只有三档：MusicLevOld / MusicItemList.MusicLev = 0/1/2
CLASSIC_TIER = {0: "初级", 1: "中级", 2: "高级"}
# MusicLevNew 11-15 的原始标签，仅作最后兜底
CLASSIC_RAW = {11: "基础", 12: "进阶", 13: "专家", 14: "大师", 15: "传奇"}
# 曲库里 MusicLev=-1（未标注）时的推断档位：按 MusicLevNew 的多数票
TIER_GUESS = {11: 0, 12: 0, 13: 1, 14: 2, 15: 2}


def difficulty_name(lev_new, old_lev):
    """拼出难度显示名。

    lev_new  = MusicLevNew（11-15 经典 / 101-105 show+）
    old_lev  = MusicLevOld 或 MusicItemList.MusicLev（经典谱 0/1/2，show+ 为空/-1）

    经典谱按官方口径分三档：[初] / [中] / [高]，后面跟原始标签（基础/进阶/专家/大师/传奇）。
    show+ 沿用五档名（show+基础…）。
    MusicLev = -1 表示官方没标档位，这种谱面无法游玩，调用方应自行排除。
    """
    try:
        lev_new = int(lev_new or 0)
    except (TypeError, ValueError):
        lev_new = 0
    if lev_new >= 100:
        label = SHOW_TYPE.get(lev_new, "")
        if label.startswith("show+-"):
            label = label[len("show+-") :]
        return "[S+]" + (label or str(lev_new))
    try:
        old = int(old_lev)
    except (TypeError, ValueError):
        old = -1
    if old in CLASSIC_TIER:
        short = {"初级": "初", "中级": "中", "高级": "高"}[CLASSIC_TIER[old]]
        return "[%s]%s" % (short, CLASSIC_RAW.get(lev_new, "经典"))
    # MusicLev = -1：舞立方秀专属谱面，二代机玩不了
    return "[秀]" + CLASSIC_RAW.get(lev_new, "经典")


def level_type_name(lev, old):
    """成绩里的难度名（兜底：没有曲库可查时用）。"""
    return difficulty_name(lev, old)


# 曲风：MusicCategoryNew 1 国语 / 3 韩文 / 4 欧美 / 6 其他。
# musicIndex 2..6 只是 GetMyRankNew 的分类请求参数，语义不同，不再拿来当曲风显示。
GENRE_NAMES = {1: "国语", 3: "韩文", 4: "欧美", 6: "其他"}
LADDER = ["未定级", "一星", "二星", "三星", "星钻", "表神", "里神"]


# ---------------------------------------------------------------- 基础设施
def load_token():
    path = os.path.join(HERE, "token.json")
    if not os.path.exists(path):
        raise ValueError("没有 token.json，请先运行：python dc_login.py qr && python dc_login.py poll")
    tok = json.load(open(path, encoding="utf-8"))
    if not tok.get("access_token"):
        raise ValueError("token.json 里没有 access_token")
    return tok


class Api:
    def __init__(self, token, uid):
        self.token = token
        self.uid = str(uid)

    def get(self, path, params=None, timeout=30):
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Authorization": "Bearer " + self.token,
        })
        try:
            with net.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, json.loads(raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(body)
            except json.JSONDecodeError:
                return e.code, {"_raw": body[:300]}
        except Exception as e:
            return 0, {"_error": "%s: %s" % (type(e).__name__, e)}


# ---------------------------------------------------------------- 战力公式
def compute_rating(level, acc):
    """level=定数(1-20)，acc=达成率(0-100)。返回单曲战力。"""
    acc_int = max(0, min(int(round(acc * 100)), 10000))
    if level < 1 or level > 20:
        return 0
    # 定数上限按 19 归一（30 号之后的曲目有定数 20 的谱面）
    k = max(0.0, 19.0 - level) / 19.0
    b = [
        (level + 2) * 100.0,
        (level + 1) * 100.0,
        level * 100.0,
        (level - 1) * 100.0,
        (level - 2 + k) * 100.0,
        (level - 3 + 2 * k) * 100.0,
        (level - 4 + 3 * k) * 100.0,
        (level - 5 + 4 * k) * 100.0,
    ]
    if acc_int == 10000:
        base, off = b[0], 0.0
    elif acc_int >= 9800:
        base, off = b[1], (acc_int - 9800) / 200.0 * 100
    elif acc_int >= 9500:
        base, off = b[2], (acc_int - 9500) / 300.0 * 100
    elif acc_int >= 9000:
        base, off = b[3], (acc_int - 9000) / 500.0 * 100
    elif acc_int >= 8500:
        base, off = b[4], (acc_int - 8500) / 500.0 * (b[3] - b[4])
    elif acc_int >= 8000:
        base, off = b[5], (acc_int - 8000) / 500.0 * (b[4] - b[5])
    elif acc_int >= 7500:
        base, off = b[6], (acc_int - 7500) / 500.0 * (b[5] - b[6])
    elif acc_int >= 7000:
        base, off = b[7], (acc_int - 7000) / 500.0 * (b[6] - b[7])
    else:
        base, off = 0.0, 0.0
    return int(base + off)


def grade_of(acc, miss):
    if acc >= 100.0 and miss == 0:
        return "AP"
    if miss == 0:
        return "FC"
    return ""


# ---------------------------------------------------------------- 数据拉取
class Player:
    def __init__(self, tok):
        self.tok = tok
        self.uid = str(tok.get("userId") or "")
        if not self.uid:
            raise ValueError("token.json 里没有 userId")
        self.api = Api(tok["access_token"], self.uid)
        self._scores = None
        self._lib_index = None
        self._lib_categories = None
        self._info = None

    def lib_lookup(self, music_id, level):
        """用曲库反查 (musicId, 定数) → MusicLevNew。

        GetLastPlay / GetMyRankNew 的 MusicLev 是 11-15 的标签，没有 MusicLevNew，
        光靠 LevOld = -1 分不清 show+ 和「没标档位的经典谱」，只能拿曲库对定数。
        """
        if self._lib_index is None:
            idx = {}
            try:
                for s in self.music_library():
                    mid = int(s.get("MusicID") or 0)
                    for x in s.get("MusicItemList") or []:
                        lv = int(x.get("MusicLevel") or 0)
                        if lv:
                            idx[(mid, lv)] = int(x.get("MusicLevNew") or 0)
            except Exception:
                pass
            self._lib_index = idx
        return self._lib_index.get((int(music_id or 0), int(level or 0)))

    def _level_name(self, music_id, level, lev, old):
        """成绩难度名：优先按曲库里的 MusicLevNew 判定，回退到记录自带的值。"""
        lev_new = self.lib_lookup(music_id, level)
        if lev_new:
            return difficulty_name(lev_new, old)
        return difficulty_name(lev, old)

    def info_raw(self):
        """(HTTP 状态码, 原始响应)；成功时顺手填缓存，出图阶段不再重复请求。"""
        st, d = self.api.get("/Dance/api/User/GetInfo",
                             {"userId": self.uid, "getNationRank": "true"})
        if isinstance(d, dict) and d.get("UserID"):
            self._info = d
        return st, d

    def info(self, force=False):
        """个人信息；接口异常时返回 dict（可能是 _error），不会崩。"""
        if self._info is not None and not force:
            return self._info
        st, d = self.info_raw()
        if isinstance(d, dict) and d.get("UserID"):
            self._info = d
        return d if isinstance(d, dict) else {}

    def account(self):
        st, d = self.api.get("/Dance/api/User/GetAccountInfo")
        return d if isinstance(d, dict) else {}

    def stats_items(self):
        st, d = self.api.get("/Dance/api/ReplyTextItem/GetAllList", {"machineId": "0"})
        return d if isinstance(d, list) else []

    def history(self):
        st, d = self.api.get("/Dance/api/User/GetLvRatioHistory", {"userId": self.uid})
        return d if isinstance(d, list) else []

    def recent(self):
        st, d = self.api.get("/Dance/api/User/GetLastPlay")
        return d if isinstance(d, list) else []

    def _category_of(self, music_id):
        """官谱曲风：用曲库 MusicCategoryNew 反查，和「官谱全表」同一套映射。"""
        if self._lib_categories is None:
            idx = {}
            try:
                for s in self.music_library():
                    idx[str(s.get("MusicID"))] = s.get("MusicCategoryNew")
            except Exception:  # noqa: BLE001
                pass
            self._lib_categories = idx
        return self._lib_categories.get(str(music_id))

    def recent_songs(self, n=None, official_only=False):
        """最近游玩记录，字段与 scores() 对齐，方便统一渲染。

        official_only=True 时只保留官谱（GetLastPlay 不带 OwnerType，
        得用全量成绩的 musicId → ownerType 映射反查）。
        """
        owner_map = {}
        try:
            for s in self.scores():
                owner_map[s["musicId"]] = s["ownerType"]
        except Exception:
            owner_map = {}

        out = []
        for r in (self.recent() or []):
            mid = r.get("MusicID")
            ot = owner_map.get(mid, 0)
            if official_only and ot != 1:
                continue
            lev = int(r.get("MusicLev", 0))
            lv = int(r.get("MusicLevel", 0))
            pct = int(r.get("PlayerPercent", 0))
            acc = pct / 100.0
            miss = int(r.get("PlayerMiss", 0))
            old = r.get("MusicLevOld")
            out.append({
                "musicId": mid,
                "name": r.get("MusicName"),
                "ownerType": ot,
                "levelType": lev,
                "levelTypeName": self._level_name(mid, lv, lev, old),
                "diff": int(old) if old is not None else -1,
                "level": lv,
                "acc": acc,
                "score": int(r.get("PlayerScore", 0)),
                "combo": int(r.get("ComboCount", 0)),
                "miss": miss,
                "time": r.get("RecordTime"),
                "rating": compute_rating(lv, acc),
                "grade": grade_of(acc, miss),
            })
        return out[:n] if n else out

    def scores(self, force=False):
        """全量成绩：musicIndex 2..6 五类合并（官方唯一的全量入口）。

        3 个并发 + 每个请求后 0.15s 间隔，避开官方约 10 req/s 的限速。
        """
        if self._scores is not None and not force:
            return self._scores
        from concurrent.futures import ThreadPoolExecutor

        def fetch(cls):
            rep = self.api.get(
                "/Dance/api/User/GetMyRankNew", {"musicIndex": str(cls)}
            )[1]
            time.sleep(0.15)
            return cls, rep

        out = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            for cls, rep in pool.map(fetch, (2, 3, 4, 5, 6)):
                if not isinstance(rep, list):
                    continue
                for song in rep:
                    cat = self._category_of(song.get("MusicID"))
                    for r in (song.get("ItemRankList") or []):
                        lev = int(r.get("MusicLev", 0))
                        lv = int(r.get("MusicRank", 0))
                        pct = int(r.get("PlayerPercent", 0))
                        acc = pct / 100.0
                        old = r.get("MusicLevOld")
                        out.append({
                            "musicId": song.get("MusicID"),
                            "name": song.get("Name"),
                            "ownerType": int(song.get("OwnerType", 0)),
                            "cls": cls,
                            "clsName": GENRE_NAMES.get(cat, "-"),
                            "levelType": lev,
                            "levelTypeName": self._level_name(song.get("MusicID"), lv, lev, old),
                            "diff": int(old) if old is not None else -1,
                            "level": lv,
                            "acc": acc,
                            "score": int(r.get("PlayerScore", 0)),
                            "combo": int(r.get("ComboCount", 0)),
                            "miss": int(r.get("PlayerMiss", 0)),
                            "time": r.get("RecordTime"),
                            "rating": compute_rating(lv, acc),
                            "grade": grade_of(acc, int(r.get("PlayerMiss", 0))),
                        })
        self._scores = out
        return out

    def official(self):
        return [s for s in self.scores() if s["ownerType"] == 1]

    def music_library(self, max_age_h=24):
        """曲库（950 首，含每谱面定数与物量）。本地缓存 24h，避免每次拉 480KB。"""
        path = MUSIC_LIB_PATH or os.path.join(CACHE_DIR, "musiclib.json")
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age_h * 3600:
            try:
                return json.load(open(path, encoding="utf-8"))
            except Exception:
                pass
        st, d = self.api.get("/Dance/MusicData/GetValidMusicList")
        if isinstance(d, list):
            _atomic_write_json(path, d)  # 和 MusicLib 共用文件，必须是原子写
            return d
        return []

    def charts_at_level(self, lv):
        """曲库中所有定数 == lv 的谱面。"""
        out = []
        for s in self.music_library():
            for it in (s.get("MusicItemList") or []):
                if int(it.get("MusicLevel", 0)) != lv:
                    continue
                lev = int(it.get("MusicLevNew", 0))
                old = it.get("MusicLev")          # 曲库里三档存在这里：0/1/2，show+ 为 -1
                out.append({
                    "musicId": str(s.get("MusicID")),
                    "name": s.get("Name"),
                    "level": lv,
                    "levelType": lev,
                    "levelTypeName": difficulty_name(lev, old),
                    "diff": int(old) if old is not None else -1,
                    "combo": int(it.get("MusicCombo", 0)),
                    "isNew": bool(s.get("IsNew")),
                })
        return out

    def ladder(self):
        """天梯（全民星）段位与分数：(grade, point)。取当前赛季，否则最近赛季。"""
        st, d = self.api.get("/Dance/api/Match/GetQuanMinSeasons")
        if not isinstance(d, list) or not d:
            return None, None
        pick = None
        for s in d:
            if s.get("IsCurrent"):
                pick = s
                break
        if pick is None:
            for s in d:
                if s.get("IsLast"):
                    pick = s
                    break
        if pick is None:
            pick = d[-1]
        return pick.get("LevelGrade"), pick.get("LevelPoint")

    def best(self, n=15):
        return sorted(self.official(), key=lambda x: -x["rating"])[:n]

    def ap(self, n=30):
        aps = [s for s in self.official() if s["grade"] == "AP"]
        return sorted(aps, key=lambda x: -x["rating"])[:n]


# ---------------------------------------------------------------- 渲染
def fmt_int(n):
    try:
        return "{:,}".format(int(n))
    except Exception:
        return str(n)


def _row(values):
    """CSV 一行，逐格做公式注入净化。"""
    return [safe_cell(v) for v in values]


def _w(s):
    """显示宽度：中日韩字符算 2 列。"""
    return sum(2 if ord(ch) > 127 else 1 for ch in str(s))


def pad(s, width, right=False):
    """按显示宽度补齐/截断，保证等宽字体下对齐。"""
    s = str(s)
    while _w(s) > width:
        s = s[:-1]
    fill = " " * (width - _w(s))
    return fill + s if right else s + fill


def hr(ch="─", n=78):
    return ch * n


def show_info(p):
    d = p.info()
    acc = p.account() or {}
    items = {i.get("ReplyTextItemID"): i.get("Content") for i in (p.stats_items() or []) if isinstance(i, dict)}
    print(hr("═"))
    print("%s  （舞立方 ID %s）" % (d.get("UserName"), d.get("UserID")))
    print(hr("═"))
    rows = [
        ("战力 LvRatio", d.get("LvRatio")),
        ("积分", fmt_int(d.get("MusicScore"))),
        ("全国排名", fmt_int(d.get("RankNation"))),
        ("全连率", "%.2f%%" % (float(d.get("ComboPercent") or 0) / 100)),
        ("城市", d.get("CityName") or "-"),
        ("战队", d.get("TeamName") or "无"),
        ("胜场", d.get("WinTimes")),
        ("金币", fmt_int(acc.get("Gold"))),
        ("星币", fmt_int(acc.get("StarCoin"))),
        ("票券", fmt_int(acc.get("Ticket"))),
        ("游玩次数", items.get(5) or "-"),
        ("隐私", "已开启" if d.get("IsPrivate") else "未开启"),
    ]
    for k, v in rows:
        print("  %-14s %s" % (k, v))
    qm = d.get("QuanMinLevels") or {}
    cur = (qm.get("Current") or {}).get("MatchName") if isinstance(qm, dict) else None
    if cur:
        print("  %-14s %s" % ("全民星赛季", cur))
    print()


def show_songs(title, songs, note=""):
    print(hr("═"))
    print("%s  （%d 首%s）" % (title, len(songs), ("，" + note) if note else ""))
    print(hr("═"))
    print("  %-3s %-20s %-4s %-11s %8s %7s %6s" % ("#", "曲名", "定数", "难度", "ACC", "战力", "评级"))
    for i, s in enumerate(songs, 1):
        print("  %-3d %-20s %-4d %-11s %7.2f%% %7d %6s" % (
            i, (s["name"] or "")[:18], s["level"], s["levelTypeName"], s["acc"], s["rating"], s["grade"] or "-"))
    if songs:
        total = sum(s["rating"] for s in songs)
        print("  " + hr("-", 72))
        print("  合计 %d · 平均 %.1f" % (total, total / len(songs)))
    print()


def show_recent(p):
    songs = p.recent_songs()
    print(hr("═"))
    print("最近游玩 %d 首" % len(songs))
    print(hr("═"))
    for i, s in enumerate(songs, 1):
        print("  %-3d %-20s lv%-2d %6.2f%% miss=%-3d 战力%5d  %s  %s" % (
            i, (s["name"] or "")[:18], s["level"], s["acc"], s["miss"], s["rating"],
            s["grade"] or "-", s["time"]))
    print()


def show_history(p, as_csv=False):
    h = p.history() or []
    if not h:
        print("没有历史战力记录")
        return
    if as_csv:
        out = os.path.join(HERE, "ratio_history.csv")
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["时间", "战力", "变化前"])
            for r in h:
                w.writerow(_row([r["LogTime"], r["LvRatio"], r["LvRatioOld"]]))
        print("已导出 %s（%d 条）" % (out, len(h)))
        return
    print(hr("═"))
    print("历史战力 %d 条（%s → %s）" % (len(h), h[0]["LogTime"], h[-1]["LogTime"]))
    print(hr("═"))
    vals = [int(r["LvRatio"]) for r in h]
    lo, hi = min(vals), max(vals)
    print("  最低 %d   最高 %d   当前 %d" % (lo, hi, vals[-1]))
    # 按天聚合，画 ASCII 曲线
    by_day = {}
    for r in h:
        day = r["LogTime"][:10]
        by_day[day] = int(r["LvRatio"])
    days = sorted(by_day)
    if len(days) > 1:
        print()
        print("  按天收盘战力：")
        w = 46
        for day in days:
            v = by_day[day]
            ratio = (v - lo) / (hi - lo) if hi > lo else 0
            bar = "█" * max(1, int(ratio * w))
            print("    %s  %5d  %s" % (day, v, bar))
    print()


def show_ratio(p):
    """对照官方 LvRatio 与本地 B15/R15 口径。"""
    info = p.info()
    official = int(info.get("LvRatio") or 0)
    b15 = p.best(15)
    b15_avg = sum(s["rating"] for s in b15) / len(b15) if b15 else 0
    rep = (p.recent() or [])[:15]
    r15 = []
    for r in rep:
        lev = int(r.get("MusicLev", 0))
        lv = int(r.get("MusicLevel", 0))
        acc = int(r.get("PlayerPercent", 0)) / 100.0
        r15.append(compute_rating(lv, acc))
    r15_avg = sum(r15) / len(r15) if r15 else 0

    print(hr("═"))
    print("战力口径对照")
    print(hr("═"))
    print("  官方 LvRatio      %d" % official)
    print("  B15 平均          %.2f   （最佳 15 首，%d 首采样）" % (b15_avg, len(b15)))
    print("  R15 平均          %.2f   （最近 15 首，%d 首采样）" % (r15_avg, len(r15)))
    print("  拟合 B15*0.75 + R15*0.25 = %.2f" % (b15_avg * 0.75 + r15_avg * 0.25))
    print()
    print("  说明：单曲战力公式已与官方接口数据自洽；总战力的聚合权重官方未公开，")
    print("        上式是单玩家单时点的拟合，仅供参考。")
    print()


def cmd_export(p, outdir):
    os.makedirs(outdir, exist_ok=True)

    def dump(name, obj):
        path = os.path.join(outdir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        print("  [ok] %-28s %7d B" % (name, os.path.getsize(path)))

    print("导出到 %s" % outdir)
    dump("info.json", p.info())
    dump("account.json", p.account())
    dump("scores.json", p.scores())
    dump("history.json", p.history())
    dump("recent.json", p.recent())
    dump("best15.json", p.best(15))
    dump("best30.json", p.best(30))
    dump("ap30.json", p.ap(30))
    # CSV
    rows = p.official()
    csv_path = os.path.join(outdir, "scores.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["曲名", "MusicID", "定数", "难度类型", "ACC", "分数", "Miss", "战力", "评级", "时间"])
        for s in rows:
            w.writerow(_row([s["name"], s["musicId"], s["level"], s["levelTypeName"],
                             "%.2f" % s["acc"], s["score"], s["miss"], s["rating"],
                             s["grade"], s["time"]]))
    print("  [ok] %-28s %7d B" % ("scores.csv", os.path.getsize(csv_path)))


def cmd_level(p, lv, args):
    """按定数整理成绩：曲库全集 ∪ 我的成绩。"""
    charts = p.charts_at_level(lv)
    scores = p.scores()
    idx = {(str(r["musicId"]), r["levelType"]): r for r in scores}

    played, unplayed, outside = [], [], []
    lib_keys = set()
    for c in charts:
        key = (c["musicId"], c["levelType"])
        lib_keys.add(key)
        r = idx.get(key)
        if r:
            played.append(dict(c, **{k: r[k] for k in ("acc", "score", "combo", "miss",
                                                       "time", "rating", "grade")}))
        else:
            unplayed.append(c)
    for key, r in idx.items():
        if r["level"] == lv and key not in lib_keys:
            outside.append(r)

    played.sort(key=lambda x: -x["rating"])
    unplayed.sort(key=lambda x: (x["levelType"] > 15, -x["levelType"], x["name"] or ""))
    outside.sort(key=lambda x: -x["rating"])

    by_type = {}
    for c in charts:
        t = c["levelTypeName"]
        d = by_type.setdefault(t, {"total": 0, "played": [], "unplayed": 0})
        d["total"] += 1
        r = idx.get((c["musicId"], c["levelType"]))
        if r:
            d["played"].append(r)
        else:
            d["unplayed"] += 1

    # ---------------- 概览
    print(hr("═"))
    print("定数 %d · 成绩整理" % lv)
    print(hr("═"))
    klass = sum(1 for c in charts if c["levelType"] <= 15)
    print("  曲库定数 %d 谱面   %d 个（经典 %d / show+ %d）" % (lv, len(charts), klass, len(charts) - klass))
    print("  已游玩             %d 个（曲库内）· 未游玩 %d 个" % (len(played), len(unplayed)))
    if outside:
        print("  自制谱/曲库外       %d 个（定数 %d）" % (len(outside), lv))
    if played:
        accs = [x["acc"] for x in played]
        rats = [x["rating"] for x in played]
        print("  平均 ACC           %.2f%%（最高 %.2f%% · 最低 %.2f%%）" % (
            sum(accs) / len(accs), max(accs), min(accs)))
        print("  平均战力           %.1f（最高 %d · 最低 %d）" % (
            sum(rats) / len(rats), max(rats), min(rats)))
        print("  AP / FC            %d / %d" % (
            sum(1 for x in played if x["grade"] == "AP"),
            sum(1 for x in played if x["grade"] == "FC")))
        full = sum(1 for x in played if x["acc"] >= 98)
        print("  ACC ≥ 98%%          %d 个（可冲 98+ 的有 %d 个）" % (
            full, sum(1 for x in played if 95 <= x["acc"] < 98)))

    # ---------------- 分难度
    print()
    print("  按难度类型：")
    for t, d in sorted(by_type.items(), key=lambda kv: -kv[1]["total"]):
        if d["played"]:
            a = sum(x["acc"] for x in d["played"]) / len(d["played"])
            r = sum(x["rating"] for x in d["played"]) / len(d["played"])
            print("    %-11s 共 %3d · 已打 %3d · 均 ACC %5.2f%% · 均战力 %5.1f" % (
                t, d["total"], len(d["played"]), a, r))
        else:
            print("    %-11s 共 %3d · 已打   0 · 均 ACC     -  · 均战力     -" % (t, d["total"]))

    # ---------------- 已游玩
    limit = len(played) if args.all else args.limit
    print()
    print(hr("─"))
    print("已游玩 %d 个（战力降序%s）" % (len(played), "" if args.all else "，前 %d" % limit))
    print(hr("─"))
    print("  %s %s %s %s %s %s %s %s  %s" % (
        pad("#", 3), pad("曲名", 26), pad("难度", 12), pad("ACC", 8, True),
        pad("分数", 9, True), pad("Miss", 5, True), pad("战力", 6, True),
        pad("评", 3), "时间"))
    for i, s in enumerate(played[:limit], 1):
        print("  %s %s %s %s %s %s %s %s  %s" % (
            pad(i, 3), pad((s["name"] or "")[:22], 26), pad(s["levelTypeName"], 12),
            pad("%.2f%%" % s["acc"], 8, True), pad(s["score"], 9, True),
            pad(s["miss"], 5, True), pad(s["rating"], 6, True),
            pad(s["grade"] or "-", 3), (s["time"] or "")[:16]))
    if not args.all and len(played) > limit:
        print("  … 还有 %d 个，用 --all 看全部" % (len(played) - limit))

    # ---------------- 未游玩
    if unplayed:
        print()
        print(hr("─"))
        print("未游玩 %d 个（推分空间，按难度类型分组）" % len(unplayed))
        print(hr("─"))
        cnt = 0
        for s in unplayed:
            if cnt >= limit:
                print("  … 还有 %d 个，用 --all 看全部" % (len(unplayed) - cnt))
                break
            new = " 🆕" if s["isNew"] else ""
            print("  %s %s 物量 %s%s" % (
                pad(s["levelTypeName"], 12), pad((s["name"] or "")[:22], 24),
                pad(s["combo"], 4, True), new))
            cnt += 1

    # ---------------- 导出
    out = args.csv or os.path.join(HERE, "out", "level%d.csv" % lv)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["曲名", "MusicID", "定数", "难度类型", "物量", "ACC", "分数",
                    "Miss", "战力", "评级", "游玩时间", "状态"])
        for s in played:
            w.writerow(_row([s["name"], s["musicId"], s["level"], s["levelTypeName"], s["combo"],
                             "%.2f" % s["acc"], s["score"], s["miss"], s["rating"],
                             s["grade"], s["time"], "已游玩"]))
        for s in unplayed:
            w.writerow(_row([s["name"], s["musicId"], s["level"], s["levelTypeName"], s["combo"],
                             "", "", "", "", "", "", "未游玩"]))
        for s in outside:
            w.writerow(_row([s["name"], s["musicId"], s["level"], s["levelTypeName"], "",
                             "%.2f" % s["acc"], s["score"], s["miss"], s["rating"],
                             s["grade"], s["time"], "自制谱"]))
    print()
    print("已导出 %s（%d 行）" % (out, len(played) + len(unplayed) + len(outside)))


def cmd_all(p, args):
    """全部曲目 × 全部谱面的成绩总表（未游玩也列出）。"""
    lib = p.music_library()
    scores = p.scores()
    idx = {(str(r["musicId"]), r["levelType"]): r for r in scores}

    charts = []
    lib_keys = set()
    for s in lib:
        for it in (s.get("MusicItemList") or []):
            lev = int(it.get("MusicLevNew", 0))
            key = (str(s.get("MusicID")), lev)
            lib_keys.add(key)
            r = idx.get(key)
            old = it.get("MusicLev")
            if r is not None:                 # 有成绩就用成绩里的准确档位回填
                try:
                    if old is None or int(old) < 0:
                        old = r.get("diff")
                except (TypeError, ValueError):
                    old = r.get("diff")
            charts.append({
                "name": s.get("Name"),
                "musicId": key[0],
                "level": int(it.get("MusicLevel", 0)),
                "levelType": lev,
                "levelTypeName": level_type_name(lev, old),
                "combo": int(it.get("MusicCombo", 0)),
                "cls": s.get("MusicCategoryNew"),
                "acc": r["acc"] if r else None,
                "score": r["score"] if r else None,
                "miss": r["miss"] if r else None,
                "rating": r["rating"] if r else None,
                "grade": r["grade"] if r else None,
                "time": r["time"] if r else None,
                "state": "已游玩" if r else "未游玩",
            })

    outside = []
    for key, r in idx.items():
        if key in lib_keys:
            continue
        outside.append({
            "name": r["name"], "musicId": key[0], "level": r["level"],
            "levelType": r["levelType"], "levelTypeName": r["levelTypeName"],
            "combo": None, "cls": None, "acc": r["acc"], "score": r["score"],
            "miss": r["miss"], "rating": r["rating"], "grade": r["grade"],
            "time": r["time"], "state": "自制谱",
        })

    rows = charts + outside
    rows.sort(key=lambda x: (-x["level"], -x["levelType"], x["name"] or ""))

    played = [r for r in rows if r["state"] == "已游玩"]
    unplayed = [r for r in rows if r["state"] == "未游玩"]
    songs_total = len(lib)
    songs_played = len({r["musicId"] for r in played})

    # ---------------- 总览
    print(hr("═"))
    print("全部曲目 · 成绩总表")
    print(hr("═"))
    print("  曲库曲目         %d 首 · 谱面 %d 个" % (songs_total, len(charts)))
    print("  已游玩谱面       %d 个（覆盖 %d 首曲目，%.1f%%）" % (
        len(played), songs_played, 100.0 * songs_played / max(1, songs_total)))
    print("  未游玩谱面       %d 个" % len(unplayed))
    if outside:
        print("  曲库外/自制谱     %d 个" % len(outside))
    if played:
        accs = [r["acc"] for r in played]
        rats = [r["rating"] for r in played]
        print("  平均 ACC         %.2f%%（最高 %.2f%% · 最低 %.2f%%）" % (
            sum(accs) / len(accs), max(accs), min(accs)))
        print("  平均战力         %.1f（最高 %d）" % (sum(rats) / len(rats), max(rats)))
        print("  AP / FC / 未满   %d / %d / %d" % (
            sum(1 for r in played if r["grade"] == "AP"),
            sum(1 for r in played if r["grade"] == "FC"),
            sum(1 for r in played if not r["grade"])))

    # ---------------- 按定数分布
    print()
    print("  按定数分布：")
    print("    %s %s %s %s %s" % (pad("定数", 6), pad("谱面", 6, True),
                                 pad("已玩", 6, True), pad("未玩", 6, True),
                                 pad("平均 ACC", 10, True)))
    for lv in range(20, 0, -1):
        group = [r for r in charts if r["level"] == lv]
        if not group:
            continue
        pl = [r for r in group if r["state"] == "已游玩"]
        a = ("%.2f%%" % (sum(x["acc"] for x in pl) / len(pl))) if pl else "-"
        print("    %s %s %s %s %s" % (pad(lv, 6), pad(len(group), 6, True),
                                     pad(len(pl), 6, True),
                                     pad(len(group) - len(pl), 6, True), pad(a, 10, True)))

    # ---------------- 按难度类型分布
    by_type = {}
    for r in charts:
        d = by_type.setdefault(r["levelTypeName"], {"t": 0, "p": 0})
        d["t"] += 1
        if r["state"] == "已游玩":
            d["p"] += 1
    print()
    print("  按难度类型：")
    for t, d in sorted(by_type.items(), key=lambda kv: (kv[0].startswith("show"), kv[0])):
        print("    %s %s %s %s" % (pad(t, 12), pad("谱面 %d" % d["t"], 10, True),
                                  pad("已玩 %d" % d["p"], 10, True),
                                  pad("未玩 %d" % (d["t"] - d["p"]), 10, True)))

    # ---------------- 明细预览
    limit = args.limit
    print()
    print(hr("─"))
    print("明细（按定数降序，前 %d 条 / 共 %d 条）" % (min(limit, len(rows)), len(rows)))
    print(hr("─"))
    print("  %s %s %s %s %s %s %s %s %s" % (
        pad("定数", 5, True), pad("曲名", 28), pad("难度", 12), pad("物量", 5, True),
        pad("ACC", 8, True), pad("战力", 6, True), pad("评", 3), pad("状态", 7), "时间"))
    for r in rows[:limit]:
        acc = ("%.2f%%" % r["acc"]) if r["acc"] is not None else "-"
        rat = str(r["rating"]) if r["rating"] is not None else "-"
        print("  %s %s %s %s %s %s %s %s %s" % (
            pad(r["level"], 5, True), pad((r["name"] or "")[:24], 28),
            pad(r["levelTypeName"], 12),
            pad(r["combo"] if r["combo"] is not None else "-", 5, True),
            pad(acc, 8, True), pad(rat, 6, True), pad(r["grade"] or "-", 3),
            pad(r["state"], 7), (r["time"] or "")[:16]))
    if len(rows) > limit:
        print("  … 还有 %d 条，见 CSV" % (len(rows) - limit))

    # ---------------- 导出
    outdir = args.out
    os.makedirs(outdir, exist_ok=True)

    p1 = os.path.join(outdir, "all_charts.csv")
    with open(p1, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["曲名", "MusicID", "定数", "难度类型", "物量", "ACC", "分数",
                    "Miss", "战力", "评级", "游玩时间", "状态", "曲风"])
        for r in rows:
            w.writerow(_row([r["name"], r["musicId"], r["level"], r["levelTypeName"],
                             r["combo"] if r["combo"] is not None else "",
                             "%.2f" % r["acc"] if r["acc"] is not None else "",
                             r["score"] if r["score"] is not None else "",
                             r["miss"] if r["miss"] is not None else "",
                             r["rating"] if r["rating"] is not None else "",
                             r["grade"] or "", r["time"] or "", r["state"], r["cls"] or ""]))

    # 每首歌一行：取该曲最佳成绩
    by_song = {}
    for r in rows:
        k = r["musicId"]
        s = by_song.setdefault(k, {"name": r["name"], "charts": 0, "played": 0,
                                   "best": None, "levels": []})
        s["charts"] += 1
        if r["state"] != "未游玩":
            s["played"] += 1
            s["levels"].append(r["level"])
            if s["best"] is None or (r["rating"] or 0) > (s["best"]["rating"] or 0):
                s["best"] = r
    p2 = os.path.join(outdir, "all_songs.csv")
    with open(p2, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["曲名", "MusicID", "谱面数", "已打数", "最高定数", "最佳战力",
                    "最佳难度", "最佳ACC", "评级", "状态"])
        for k, s in sorted(by_song.items(), key=lambda kv: -(kv[1]["best"]["rating"] if kv[1]["best"] else 0)):
            b = s["best"]
            w.writerow(_row([s["name"], k, s["charts"], s["played"],
                             max(s["levels"]) if s["levels"] else "",
                             b["rating"] if b else "", b["levelTypeName"] if b else "",
                             "%.2f" % b["acc"] if b else "", (b["grade"] or "") if b else "",
                             "已游玩" if s["played"] else "未游玩"]))

    print()
    print("已导出：")
    print("  %s  %d 行（每谱面一行，含未游玩）" % (p1, len(rows)))
    print("  %s  %d 行（每曲一行，取最佳）" % (p2, len(by_song)))


def cmd_ranked(p, args):
    """已游玩成绩：定数降序优先，完成度(ACC)降序其次；排除自制谱。"""
    lib = p.music_library()
    scores = p.scores()
    charts = {}
    for s in lib:
        for it in (s.get("MusicItemList") or []):
            charts[(str(s.get("MusicID")), int(it.get("MusicLevNew", 0)))] = {
                "combo": int(it.get("MusicCombo", 0)),
                "cls": s.get("MusicCategoryNew"),
            }

    rows = []
    skipped = 0
    for r in scores:
        if r["ownerType"] != 1:          # 自制谱不进统计
            skipped += 1
            continue
        key = (str(r["musicId"]), r["levelType"])
        c = charts.get(key, {})
        rows.append({
            "name": r["name"], "musicId": key[0], "level": r["level"],
            "levelTypeName": r["levelTypeName"], "acc": r["acc"],
            "score": r["score"], "miss": r["miss"], "rating": r["rating"],
            "grade": r["grade"], "time": r["time"],
            "combo": c.get("combo"), "inLib": key in charts,
        })

    rows.sort(key=lambda x: (-x["level"], -x["acc"], -x["rating"]))

    # ---------------- 总览
    accs = [r["acc"] for r in rows]
    levels = sorted({r["level"] for r in rows}, reverse=True)
    print(hr("═"))
    print("已游玩成绩 · 定数倒序 → 完成度(ACC)倒序")
    print(hr("═"))
    print("  官谱成绩         %d 个（已排除自制谱 %d 个）" % (len(rows), skipped))
    print("  覆盖定数         %d - %d（共 %d 档）" % (min(levels), max(levels), len(levels)))
    print("  平均完成度       %.2f%%（最高 %.2f%% · 最低 %.2f%%）" % (
        sum(accs) / len(accs), max(accs), min(accs)))
    print("  平均战力         %.1f" % (sum(r["rating"] for r in rows) / len(rows)))
    print("  评级             AP %d · FC %d · 未满 %d" % (
        sum(1 for r in rows if r["grade"] == "AP"),
        sum(1 for r in rows if r["grade"] == "FC"),
        sum(1 for r in rows if not r["grade"])))
    if any(not r["inLib"] for r in rows):
        print("  曲库外官谱       %d 个（已下架，仍计入）" % sum(1 for r in rows if not r["inLib"]))

    # ---------------- 按定数分组
    per = len(rows) if args.all else args.per_level
    for lv in levels:
        group = [r for r in rows if r["level"] == lv]
        gacc = sum(r["acc"] for r in group) / len(group)
        print()
        print(hr("─"))
        print("定数 %d · %d 个 · 均 ACC %.2f%% · 均战力 %.0f" % (
            lv, len(group), gacc, sum(r["rating"] for r in group) / len(group)))
        print(hr("─"))
        print("  %s %s %s %s %s %s %s %s" % (
            pad("#", 3), pad("曲名", 28), pad("难度", 12), pad("ACC", 8, True),
            pad("分数", 8, True), pad("Miss", 5, True), pad("战力", 6, True),
            pad("评", 3)))
        for i, r in enumerate(group[:per], 1):
            print("  %s %s %s %s %s %s %s %s" % (
                pad(i, 3), pad((r["name"] or "")[:24], 28), pad(r["levelTypeName"], 12),
                pad("%.2f%%" % r["acc"], 8, True), pad(r["score"], 8, True),
                pad(r["miss"], 5, True), pad(r["rating"], 6, True),
                pad(r["grade"] or "-", 3)))
        if len(group) > per:
            print("  … 还有 %d 个（--all 全列）" % (len(group) - per))

    # ---------------- 导出
    out = args.csv or os.path.join(HERE, "out", "played_ranked.csv")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["排名", "定数", "曲名", "MusicID", "难度类型", "完成度ACC",
                    "分数", "Miss", "战力", "评级", "物量", "游玩时间"])
        for i, r in enumerate(rows, 1):
            w.writerow(_row([i, r["level"], r["name"], r["musicId"], r["levelTypeName"],
                             "%.2f" % r["acc"], r["score"], r["miss"], r["rating"],
                             r["grade"] or "", r["combo"] if r["combo"] is not None else "",
                             r["time"] or ""]))
    print()
    print("已导出 %s（%d 行）" % (out, len(rows)))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="舞立方官方接口本地客户端")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    sub.add_parser("ratio")
    for name in ("b15", "b30", "recent", "export"):
        sp = sub.add_parser(name)
        if name in ("b15", "b30"):
            sp.add_argument("--all", action="store_true", help="包含自制谱")
        if name == "export":
            sp.add_argument("--out", default="out")
    sub.add_parser("ap30").add_argument("--all", action="store_true")
    sub.add_parser("history").add_argument("--csv", action="store_true")
    p_lv = sub.add_parser("level")
    p_lv.add_argument("--lv", type=int, default=12, help="定数（1-19），默认 12")
    p_lv.add_argument("--all", action="store_true", help="不打折，全部列出")
    p_lv.add_argument("--limit", type=int, default=40, help="每段最多显示多少行")
    p_lv.add_argument("--csv", default="", help="CSV 输出路径")
    p_all = sub.add_parser("all")
    p_all.add_argument("--limit", type=int, default=30, help="终端预览条数")
    p_all.add_argument("--out", default=os.path.join(HERE, "out"), help="CSV 输出目录")
    p_rk = sub.add_parser("ranked")
    p_rk.add_argument("--per-level", type=int, default=10, help="每个定数显示几条")
    p_rk.add_argument("--all", action="store_true", help="每个定数全部列出")
    p_rk.add_argument("--csv", default="", help="CSV 输出路径")
    args = ap.parse_args()

    tok = load_token()
    p = Player(tok)
    print("[i] 舞立方 ID %s · token 到期 %s" % (p.uid, tok.get("expires")))

    if args.cmd == "info":
        show_info(p)
    elif args.cmd == "ratio":
        show_ratio(p)
    elif args.cmd in ("b15", "b30"):
        n = 15 if args.cmd == "b15" else 30
        pool = p.scores() if args.all else p.official()
        songs = sorted(pool, key=lambda x: -x["rating"])[:n]
        show_songs("%s 战力前 %d" % (args.cmd.upper(), n), songs,
                   "含自制谱" if args.all else "仅官谱")
    elif args.cmd == "ap30":
        aps = sorted(p.scores() if args.all else p.official(), key=lambda x: -x["rating"])
        aps = [s for s in aps if s["grade"] == "AP"][:30]
        show_songs("AP30 满分成绩前 30", aps)
    elif args.cmd == "recent":
        show_recent(p)
    elif args.cmd == "history":
        show_history(p, args.csv)
    elif args.cmd == "level":
        cmd_level(p, args.lv, args)
    elif args.cmd == "all":
        cmd_all(p, args)
    elif args.cmd == "ranked":
        cmd_ranked(p, args)
    elif args.cmd == "export":
        cmd_export(p, args.out)


if __name__ == "__main__":
    main()
