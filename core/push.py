"""曲库变更订阅：凌晨只检测并存变更，早八再推送，避免深夜扰人。

每个会话（UMO）独立开关，状态和待推送内容都存在同一个 JSON 文件里，
重启后不丢；pending 被取出后清空，推不出去也不会重复轰炸。
"""

import json
import time
from pathlib import Path

from . import NullLogger


class PushStore:
    """订阅目标与待推送变更的持久化（原子写）。"""

    def __init__(self, path, logger=None):
        self.path = Path(path)
        self.logger = logger or NullLogger()

    # ---------------------------------------------------------- 读写
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            # 读坏了会按空表继续走，之后的 _save 可能把订阅和待推送整个覆盖掉——
            # 这种「静默丢数据」必须在日志里留一句
            self.logger.warning("[dancecube] 推送状态读取失败（%s）：%s", self.path.name, e)
            return {}

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    # ---------------------------------------------------------- 订阅目标
    def targets(self):
        return list(self._load().get("targets") or [])

    def enabled_targets(self):
        return [t for t in self.targets() if t.get("enabled")]

    def is_enabled(self, umo):
        return any(t.get("umo") == umo and t.get("enabled") for t in self.targets())

    def set_enabled(self, umo, enabled):
        """开启/关闭某个会话；返回目标是否存在（关闭不存在的目标返回 False）。"""
        umo = (umo or "").strip()
        if not umo:
            return False
        data = self._load()
        targets = data.setdefault("targets", [])
        for t in targets:
            if t.get("umo") == umo:
                t["enabled"] = bool(enabled)
                t["updated_at"] = int(time.time())
                self._save(data)
                return True
        if not enabled:
            return False
        targets.append({"umo": umo, "enabled": True, "added_at": int(time.time())})
        self._save(data)
        return True

    # ---------------------------------------------------------- 待推送变更
    def save_pending(self, changes):
        """把一次检测到的变化并入 pending，等早八统一推。"""
        data = self._load()
        pending = data.get("pending") or {}
        for key in ("official_new", "official_gone", "user_new", "user_gone"):
            merged = list(
                dict.fromkeys((pending.get(key) or []) + (changes.get(key) or []))
            )
            pending[key] = merged
        pending["date"] = changes.get("date") or time.strftime("%Y-%m-%d")
        data["pending"] = pending
        self._save(data)

    def peek_pending(self):
        """只看不删：有没有待推送内容。用于决定要不要调 take_pending。"""
        pending = self._load().get("pending") or {}
        keys = ("official_new", "official_gone", "user_new", "user_gone")
        return pending if any(pending.get(k) for k in keys) else {}

    def take_pending(self):
        """取出并清空 pending；没有变化时返回空 dict。"""
        data = self._load()
        pending = data.get("pending") or {}
        if not any(pending.get(k) for k in ("official_new", "official_gone", "user_new", "user_gone")):
            return {}
        data["pending"] = {}
        self._save(data)
        return pending

    # ---------------------------------------------------------- 每日进度
    def get_progress(self):
        """取「最后一次刷新/推送的日期」，用于重启后判断今天还要不要做。

        没有这两个日期时，重启会让调度从头睡到下一个 03:xx，当天的 08:00 推送
        就被整段跳过——所以进度必须落盘，不能只放内存。
        """
        data = self._load()
        prog = data.get("progress") or {}
        return {
            "refreshed": str(prog.get("refreshed") or ""),
            "pushed": str(prog.get("pushed") or ""),
        }

    def set_progress(self, key, date):
        """记录某天已完成的动作。key 只能是 refreshed / pushed。"""
        if key not in ("refreshed", "pushed"):
            return
        data = self._load()
        prog = data.get("progress") or {}
        prog[key] = str(date)
        data["progress"] = prog
        self._save(data)
