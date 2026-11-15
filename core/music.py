# -*- coding: utf-8 -*-
"""舞立方曲库查询：官谱 + 商店自制谱。

官谱：GetValidMusicList（950 首）
自制谱：GetGoodsMusic（商店在售，6000+ 首，带作者、标签、价格、点赞）

谱面展示规则（和游戏内商品详情一致）：
  经典谱：[初]基础(14) / [中]进阶(11) / [高]专家(13)
  show+ ：show+传奇(17)
  定数为 0 也要显示；未填的标「未填」。
排序按档位（初/中/高），不按定数——自制谱的定数是作者自填的，
初级定数比高级高是正常现象。
"""
import glob
import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request

BASE = "https://dancedemo.shenghuayule.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
OFFICIAL_TTL = 24 * 3600
USER_TTL = 6 * 3600

SHOW_TYPE = {101: "基础", 102: "进阶", 103: "专家", 104: "大师", 105: "传奇"}
CLASSIC_TIER = {0: "初级", 1: "中级", 2: "高级"}
CLASSIC_RAW = {11: "基础", 12: "进阶", 13: "专家", 14: "大师", 15: "传奇"}


def _tier_short(tier):
    return {"初级": "初", "中级": "中", "高级": "高"}.get(tier, "")


def _load_tokens(plugin_data_dir):
    """返回所有可用 token，按文件排序。"""
    pattern = os.path.join(
        plugin_data_dir, "astrbot_plugin_dancecube", "tokens", "*.json"
    )
    out = []
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, encoding="utf-8") as f:
                tok = json.load(f)
            if tok.get("access_token"):
                out.append(tok["access_token"])
        except (OSError, ValueError):
            continue
    return out


def _find_token(plugin_data_dir):
    tokens = _load_tokens(plugin_data_dir)
    return tokens[0] if tokens else None


def _api(path, token, params=None):
    if not token:
        raise RuntimeError("曲库尚未初始化，请先让任意用户执行 /舞立方登录")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Authorization": "Bearer " + token}
    )
    with urllib.request.urlopen(req, timeout=40, context=ctx) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _norm_levels(items):
    """统一谱面结构：{tier, raw, level, combo, isShow}。

    MusicLev = -1 的经典谱面官方没标档位、游戏里无法游玩，直接排除；
    show+ 谱面不受此限制。
    """
    out = []
    for it in items or []:
        lev_new = int(it.get("MusicLevNew") or 0)
        old = it.get("MusicLev")
        if old is None:
            old = it.get("MusicLevOld")
        try:
            old = int(old)
        except (TypeError, ValueError):
            old = -1
        level = int(it.get("MusicLevel") or it.get("MusicRank") or 0)
        combo = int(it.get("MusicCombo") or it.get("ComboCount") or 0)

        if lev_new >= 100:
            tier, raw = "show+", SHOW_TYPE.get(lev_new, str(lev_new))
        elif old in CLASSIC_TIER:
            tier, raw = CLASSIC_TIER[old], CLASSIC_RAW.get(lev_new, "经典")
        else:
            # MusicLev = -1（没标档位）。有定数或物量说明是真实谱面，
            # 两个都没有才是占位条目（游戏里玩不到）
            if level <= 0 and combo <= 0:
                continue
            tier, raw = "", CLASSIC_RAW.get(lev_new, "经典")

        out.append(
            {
                "tier": tier,
                "raw": raw,
                "level": level,
                "combo": combo,
                "isShow": lev_new >= 100,
                "levNew": lev_new,
                "order": old if old in (0, 1, 2) else 9,
                "isBad": bool(it.get("IsBad")),
            }
        )
    # 经典按 MusicLevNew（11-15）升序，show+ 排在最后
    return sorted(out, key=lambda x: (x["isShow"], x.get("levNew") or 0))


def level_label(lv):
    """生成 [初]基础(14) / [秀]大师(18) / [S+]传奇(17) 这样的标签。"""
    if lv["isShow"]:
        name = "[S+]%s" % (lv["raw"] or "")
    elif lv["tier"]:
        name = "[%s]%s" % (_tier_short(lv["tier"]), lv["raw"])
    else:
        # 舞立方秀专属，二代机玩不了
        name = "[秀]%s" % (lv["raw"] or "")
    if lv["level"] > 0:
        name += "(%d)" % lv["level"]
    elif lv["level"] == 0:
        name += "(0)"
    else:
        name += "(未填)"
    return name


def _norm_official(song):
    return {
        "source": "official",
        "name": song.get("Name") or "?",
        "musicId": song.get("MusicID"),
        "levels": _norm_levels(song.get("MusicItemList")),
        "price": None,
        "like": None,
        "ownerId": None,
        "ownerName": "",
        "tags": [],
    }


def _norm_user(item):
    return {
        "source": "user",
        "name": item.get("GoodsName") or "?",
        "musicId": item.get("MusicID"),
        "levels": _norm_levels(item.get("LevelList")),
        "price": item.get("PriceSale"),
        "like": item.get("LikeCount"),
        "ownerId": item.get("OwnerID"),
        "ownerName": item.get("OwnerName") or "",
        "tags": item.get("TagList") or [],
        "hasShow": bool(item.get("HasShow")),
        "intro": item.get("GoodsIntro") or "",
    }


class MusicLib:
    def __init__(self, cache_path, plugin_data_dir):
        self.cache_path = cache_path
        self.plugin_data_dir = plugin_data_dir
        self._official = None
        self._user = None
        self._duration = None

    def load_duration(self):
        """曲目时长表（musicId → 秒），官谱和自制谱合并。"""
        if self._duration is not None:
            return self._duration
        import os as _os

        base = _os.path.dirname(str(self.cache_path))
        merged = {}
        for name in ("music_duration.json", "music_duration_user.json"):
            try:
                with open(_os.path.join(base, name), encoding="utf-8") as f:
                    merged.update(json.load(f))
            except (OSError, ValueError):
                continue
        self._duration = merged
        return self._duration

    def load_bitrate(self):
        """音频文件大小表（musicId → 字节），用来算比特率。"""
        if getattr(self, "_size", None) is not None:
            return self._size
        import os as _os

        p = _os.path.join(_os.path.dirname(str(self.cache_path)), "music_bitrate.json")
        try:
            with open(p, encoding="utf-8") as f:
                self._size = json.load(f)
        except (OSError, ValueError):
            self._size = {}
        return self._size

    def _attach_duration(self, records):
        dur = self.load_duration()
        size = self.load_bitrate()
        for r in records:
            mid = str(r.get("musicId"))
            d = dur.get(mid)
            r["duration"] = d
            b = size.get(mid)
            if d and b:
                r["kbps"] = round(b * 8 / d / 1000)
        return records

    def _fetch(self, path, params=None):
        """依次尝试所有已登录用户 token，避免单个凭证失效就把曲库查死。"""
        tokens = _load_tokens(self.plugin_data_dir)
        if not tokens:
            raise RuntimeError("曲库尚未初始化，请先让任意用户执行 /舞立方登录")
        last_err = None
        for tok in tokens:
            try:
                return _api(path, tok, params)
            except Exception as e:  # noqa: BLE001
                last_err = e
                continue
        raise last_err if last_err else RuntimeError("曲库请求失败")

    # ---------------------------------------------------------- 官谱
    def load_official(self):
        if self._official is not None:
            return self._official
        path = str(self.cache_path)
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < OFFICIAL_TTL:
            try:
                with open(path, encoding="utf-8") as f:
                    self._official = [_norm_official(x) for x in json.load(f)]
                return self._official
            except (OSError, ValueError):
                pass
        songs = self._fetch("/Dance/MusicData/GetValidMusicList")
        if not isinstance(songs, list):
            raise RuntimeError("官谱接口返回异常")
        self._official = [_norm_official(x) for x in songs]
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(songs, f, ensure_ascii=False)
        except OSError:
            pass
        return self._official

    # ---------------------------------------------------------- 自制谱
    def load_user(self, force=False):
        if self._user is not None and not force:
            return self._user
        path = str(self.cache_path).replace(".json", "_user.json")
        if not force and os.path.exists(path):
            if time.time() - os.path.getmtime(path) < USER_TTL:
                try:
                    with open(path, encoding="utf-8") as f:
                        self._user = json.load(f)
                    return self._user
                except (OSError, ValueError):
                    pass
        items, page = [], 1
        while page <= 20:
            d = self._fetch(
                "/Dance/api/Goods/GetGoodsMusic",
                {"page": page, "pagesize": 1000, "orderby": 3, "ordertype": 2},
            )
            lst = (d or {}).get("List") or []
            items.extend(_norm_user(x) for x in lst)
            total = int((d or {}).get("RecordCount") or 0)
            if not lst or len(items) >= total:
                break
            page += 1
            time.sleep(0.3)
        self._user = items
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False)
        except OSError:
            pass
        return self._user

    # ---------------------------------------------------------- 检索
    def _search(self, records, keyword):
        low = keyword.lower()
        exact, prefix, contains = [], [], []
        for r in records:
            name = (r.get("name") or "").strip()
            if not name:
                continue
            nlow = name.lower()
            if nlow == low:
                exact.append(r)
            elif nlow.startswith(low):
                prefix.append(r)
            elif low in nlow:
                contains.append(r)
        return exact + prefix + contains

    def find(self, text, limit=5):
        text = (text or "").strip().lstrip("#")
        if not text:
            return []
        if text.isdigit():
            mid = int(text)
            for loader in (self.load_official, self.load_user):
                try:
                    hits = [r for r in loader() if int(r.get("musicId") or 0) == mid]
                except Exception:  # noqa: BLE001
                    hits = []
                if hits:
                    return self._attach_duration([self.enrich_user(r) for r in hits[:limit]])
        out = []
        try:
            out.extend(self._search(self.load_official(), text))
        except Exception:  # noqa: BLE001
            pass
        if out:
            return out[:limit]
        try:
            out.extend(self._search(self.load_user(), text))
        except Exception:  # noqa: BLE001
            pass
        return self._attach_duration([self.enrich_user(r) for r in out[:limit]])

    def find_in_question(self, question):
        q = (question or "").strip()
        if not q:
            return None
        qlow = q.lower()
        for loader in (self.load_official, self.load_user):
            try:
                records = loader()
            except Exception:  # noqa: BLE001
                continue
            best = None
            for r in records:
                name = (r.get("name") or "").strip()
                if len(name) >= 2 and name.lower() in qlow:
                    if best is None or len(name) > len(best.get("name") or ""):
                        best = r
            if best is not None:
                return self._attach_duration([self.enrich_user(best)])[0]
        for token in re.findall(r"(?<![\d.])#?(\d{1,6})(?![\d.])", q):
            mid = int(token)
            for loader in (self.load_official, self.load_user):
                try:
                    for r in loader():
                        if int(r.get("musicId") or 0) == mid:
                            return self._attach_duration([self.enrich_user(r)])[0]
                except Exception:  # noqa: BLE001
                    continue
        return None

    # ---------------------------------------------------------- 补充 show+
    def enrich_user(self, record):
        """用 GetGoodsInfo 补 show+ 谱面（列表接口只给经典三档）。"""
        if record.get("source") != "user":
            return record
        mid = record.get("musicId")
        if not mid:
            return record
        if not record.get("hasShow"):
            return record
        try:
            d = self._fetch("/Dance/api/MusicData/GetGoodsInfo", {"musicId": mid})
        except Exception:  # noqa: BLE001
            return record

        levels = list(record.get("levels") or [])
        have_show = any(lv.get("isShow") for lv in levels)
        if not have_show:
            for it in d.get("LevelList") or []:
                try:
                    lv_new = int(it.get("MusicLevNew") or 0)
                    level = int(it.get("MusicLevel") or 0)
                except (TypeError, ValueError):
                    continue
                if lv_new < 100 or level < 0:
                    continue
                levels.append(
                    {
                        "tier": "show+",
                        "raw": SHOW_TYPE.get(lv_new, str(lv_new)),
                        "level": level,
                        "combo": 0,
                        "isShow": True,
                        "order": 9,
                    }
                )
            levels.sort(key=lambda x: x["isShow"])
            record["levels"] = levels

        if d.get("BPM"):
            record["bpm"] = d.get("BPM")
        if d.get("GoodsIntro"):
            record["intro"] = d.get("GoodsIntro")
        return record

    # ---------------------------------------------------------- 格式化
    @staticmethod
    def format(record):
        tag = "自制谱" if record.get("source") == "user" else "官谱"
        levels = record.get("levels") or []
        has_bad = any(lv.get("isBad") for lv in levels)
        title = "🎵 %s（%s · 音乐 ID %s）" % (
            record.get("name") or "?",
            tag,
            record.get("musicId"),
        )
        if has_bad:
            title += "\n  ⚠️ 存在异常谱面，游戏内购买时会提示"
        lines = [title]
        dur = record.get("duration")
        if dur:
            label = "%d:%02d" % (int(dur) // 60, int(dur) % 60)
            if record.get("source") == "user":
                kbps = record.get("kbps")
                extra = " · %d kbps" % kbps if kbps else ""
                lines.append("  音频时长 %s%s" % (label, extra))
            else:
                lines.append("  时长 %s" % label)
        elif record.get("source") == "official":
            lines.append("  时长：商店隐藏歌曲，无法拉取")
        owner_id = record.get("ownerId")
        owner_name = record.get("ownerName")
        if owner_id or owner_name:
            lines.append("  作者：【%s】%s" % (owner_id or "-", owner_name or ""))
        tags = record.get("tags") or []
        if tags:
            lines.append("  标签：%s" % " / ".join(tags))

        for lv in levels:
            # 定数未填（-1）的谱面按不存在处理，不展示
            if lv.get("level") is not None and lv["level"] < 0 and not lv.get("isShow"):
                continue
            label = level_label(lv)
            if lv.get("isBad"):
                label = "⚠️ " + label
            combo = " · 物量 %d" % lv["combo"] if lv["combo"] > 0 else ""
            lines.append("  %s%s" % (label, combo))
        if not levels:
            lines.append("  暂无谱面数据")

        extra = []
        if record.get("price") is not None:
            extra.append("%s 金币" % int(record["price"]))
        if record.get("like") is not None:
            extra.append("%d 赞" % int(record["like"]))
        if extra:
            lines.append("  " + " · ".join(extra))
        return "\n".join(lines)
