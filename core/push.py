"""曲库变更订阅：凌晨只检测并存变更，早八再推送，避免深夜扰人。

每个会话（UMO）独立开关，状态和待推送内容都存在同一个 JSON 文件里，
重启后不丢；pending 被取出后清空，推不出去也不会重复轰炸。
"""

import json
import time
from pathlib import Path


class PushStore:
    """订阅目标与待推送变更的持久化（原子写）。"""

    def __init__(self, path):
        self.path = Path(path)

    # ---------------------------------------------------------- 读写
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
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
        """开启/关闭某个会话；关闭时返回是否本来就在开。"""
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
                return bool(enabled)
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

    def take_pending(self):
        """取出并清空 pending；没有变化时返回空 dict。"""
        data = self._load()
        pending = data.get("pending") or {}
        if not any(pending.get(k) for k in ("official_new", "official_gone", "user_new", "user_gone")):
            return {}
        data["pending"] = {}
        self._save(data)
        return pending
