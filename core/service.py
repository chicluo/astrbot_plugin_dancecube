# -*- coding: utf-8 -*-
"""舞立方业务逻辑（框架无关）。

任何 bot 框架接入只需要三步：

1. `DanceCubeService(config, data_dir, bundled_data_dir, notifier, logger)`
2. 把一条用户消息整理成 `RequestContext`（谁、在哪个群、平台、完整文本）
3. `async for reply in service.song(ctx): ...` 把 `Text / Image / File` 发出去

`notifier` 是可选的异步回调，用于登录流程结束后的主动推送：
`await notifier(ctx, [Text(...), Image(...)])`。
"""
import asyncio
import json
import os
import random
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import catalog as dc_catalog
from . import client as dc_client
from . import image as dc_image
from . import login as dc_login
from . import music as dc_music
from . import net
from .client import Player
from .export import export_scores
from .profile_image import build_info
from .push import PushStore
from .safe import safe_error

PLUGIN_NAME = "astrbot_plugin_dancecube"

# 「/曲目 官谱全表」「/曲目 自制谱全表」：直接出 xlsx，不按曲名搜
CATALOG_KEYS = {
    "官谱全表": "official",
    "官谱表": "official",
    "官谱表格": "official",
    "自制谱全表": "user",
    "自制谱表": "user",
    "自制谱表格": "user",
}


# ---------------------------------------------------------------- 消息类型
@dataclass
class Text:
    text: str


@dataclass
class Image:
    path: str


@dataclass
class File:
    path: str
    name: str


@dataclass
class RequestContext:
    """一次请求的上下文，由各框架适配器填充。"""

    user_id: str
    group_id: str = ""
    is_group: bool = False
    platform: str = ""
    text: str = ""  # 完整消息文本（含命令前缀），多词歌名要靠它
    origin: str = ""  # 会话标识，用于登录完成后的主动推送


# ---------------------------------------------------------------- 小工具
def as_bool(value, default=False):
    """配置可能被手改成字符串，bool("false") 是 True，必须显式解析。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def safe_id(value) -> str:
    """QQ/openid 只保留安全字符，避免拼路径时被 ../ 之类带出去。"""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(value or "user"))


def _harden_nt(path, is_dir):
    """Windows 上 os.chmod 不映射到 NTFS ACL，改用 icacls 收紧到当前用户 + SYSTEM + Administrators。"""
    try:
        import subprocess
        user = os.environ.get("USERNAME", "")
        domain = os.environ.get("USERDOMAIN", "")
        owner = ("%s\\%s" % (domain, user)) if domain and user else user
        if not owner:
            return
        rights = "(OI)(CI)F" if is_dir else "F"
        args = ["icacls", str(path), "/inheritance:r"]
        for who in (owner, "SYSTEM", "Administrators"):
            args += ["/grant:r", "%s:%s" % (who, rights)]
        subprocess.run(args, capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def harden_dir(path):
    if os.name == "nt":
        _harden_nt(path, True)
        return
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def harden_file(path):
    if os.name == "nt":
        _harden_nt(path, False)
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def file_send_hint(err):
    """发送文件失败时，把常见原因翻成人话。"""
    s = str(err)
    low = s.lower()
    if "too large" in low or "file_size" in low or "size limit" in low or "超过" in s:
        return "文件超过接口大小限制（单文件硬上限 200MB）。"
    if "429" in s or "rate" in low or "qps" in low or "频率" in s:
        return "发送太频繁被限流，过几分钟再试。"
    if "file_info" in low or "expired" in low:
        return "上传凭证过期，重发一次指令即可。"
    if "401" in s or "token" in low or "凭证" in s:
        return "机器人平台凭证异常，请管理员检查配置。"
    return "可能是网络波动或接口临时异常，稍后再试即可。"


class _NullLogger:
    """未注入 logger 时的占位实现：不依赖标准库 logging 模块。"""

    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class DanceCubeService:
    """把命令逻辑从框架里拆出来，所有方法只依赖 RequestContext。"""

    def __init__(self, config, data_dir, bundled_data_dir=None,
                 notifier=None, logger=None):
        self.config = config
        self.logger = logger or _NullLogger()
        self.notifier = notifier
        self.data_dir = Path(data_dir)
        self.tokens_dir = self.data_dir / "tokens"
        self.tmp_dir = self.data_dir / "tmp"
        self.tokens_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        harden_dir(self.tokens_dir)
        # 让 Player 的曲库缓存直接用同一份快照，避免两套缓存
        dc_client.set_music_library_path(self.data_dir / "music" / "music_library.json")
        self._pending: dict[str, asyncio.Task] = {}
        self._last_call: dict[str, float] = {}
        self._catalog_lock = asyncio.Lock()
        self._mlib = None
        self._warned_openid = False
        self._sched_task = None
        self._push = PushStore(str(self.data_dir / "push_targets.json"))
        self._ensure_music_cache(bundled_data_dir)
        self._check_config()

    # ------------------------------------------------------------ 配置
    def _check_config(self):
        groups = [str(g).strip() for g in (self.config.get("allowed_groups") or [])]
        if "*" in groups:
            self.logger.warning(
                "[dancecube] allowed_groups 含 *，所有群都能使用登录入口，请确认这是本意"
            )
        if as_bool(self.config.get("login_admin_only"), False) and not str(
            self.config.get("owner_qq") or ""
        ).strip():
            self.logger.warning(
                "[dancecube] login_admin_only=true 但 owner_qq 为空，所有人都会被拒绝登录"
            )
        insecure = as_bool(self.config.get("insecure_tls"), False)
        net.set_verify_tls(not insecure)
        if insecure:
            self.logger.warning(
                "[dancecube] 已关闭 TLS 证书校验（insecure_tls=true），token 可能被中间人截获"
            )

    def _warn_openid(self):
        if self._warned_openid:
            return
        self._warned_openid = True
        self.logger.warning(
            "[dancecube] allowed_groups 里填的是数字群号，但 qq_official 平台拿到的是 "
            "group_openid，永远匹配不上；请改填 openid，或用 * 放开全部群"
        )

    # ------------------------------------------------------------ 权限
    def allowed(self, ctx: RequestContext) -> bool:
        owner = str(self.config.get("owner_qq") or "").strip()
        if owner and ctx.user_id == owner:
            return True  # 机器人主人不受群白名单限制
        if not ctx.is_group:
            return as_bool(self.config.get("allow_private"), True)
        groups = {
            str(g).strip()
            for g in (self.config.get("allowed_groups") or [])
            if str(g).strip()
        }
        if "*" in groups:
            return True
        if ctx.group_id and ctx.group_id in groups:
            return True
        if ctx.group_id and ctx.platform == "qq_official" and any(
            g.isdigit() for g in groups
        ):
            self._warn_openid()
        return False

    # ------------------------------------------------------------ 凭证
    def _token_path(self, uid: str) -> Path:
        return self.tokens_dir / ("%s.json" % safe_id(uid))

    def load_token(self, uid: str):
        path = self._token_path(uid)
        if not path.exists():
            return None
        try:
            tok = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        return tok if tok.get("access_token") else None

    def save_token(self, uid: str, tok: dict) -> None:
        path = self._token_path(uid)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(tok, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        harden_file(path)

    def drop_token(self, uid: str) -> bool:
        path = self._token_path(uid)
        if path.exists():
            path.unlink()
            return True
        return False

    # ------------------------------------------------------------ 临时文件
    def _tmp_path(self, prefix: str, uid: str) -> Path:
        return self.tmp_dir / ("%s_%s_%d.png" % (prefix, safe_id(uid), int(time.time() * 1000)))

    def _clean_tmp(self, keep_seconds: int = 3600) -> None:
        deadline = time.time() - keep_seconds
        for p in self.tmp_dir.glob("*.png"):
            try:
                if p.stat().st_mtime < deadline:
                    p.unlink()
            except OSError:
                pass

    def _cooldown_wait(self, uid: str, seconds: int = 30, kind: str = "query") -> int:
        # 主人不设冷却（测试时不想等）
        owner = str(self.config.get("owner_qq") or "").strip()
        if owner and uid == owner:
            return 0
        key = "%s:%s" % (kind, uid)
        now = time.time()
        last = self._last_call.get(key, 0.0)
        if now - last < seconds:
            return max(1, int(seconds - (now - last)))
        self._last_call[key] = now
        return 0

    # ------------------------------------------------------------ 公共查询
    async def _player(self, ctx: RequestContext):
        wait = self._cooldown_wait(ctx.user_id)
        if wait:
            return None, "操作太频繁，请 %d 秒后再试。" % wait
        tok = self.load_token(ctx.user_id)
        if not tok:
            return None, "你还没有登录舞立方。\n请先发送 /舞立方登录 扫码。"
        try:
            player = Player(tok)
            status, info = await asyncio.to_thread(player.info_raw)
        except Exception as e:  # noqa: BLE001
            return None, "连接舞立方失败：%s" % safe_error(e)
        if isinstance(info, dict) and info.get("UserID"):
            return player, ""
        # 只有明确的认证失败才删 token；网络抖动保留凭证
        if status in (401, 403):
            self.drop_token(ctx.user_id)
            return None, "你的登录凭证已失效，请重新发送 /舞立方登录 扫码。"
        if status == 0:
            return None, "连接舞立方失败（网络超时），请稍后再试。"
        return None, "舞立方接口返回异常（HTTP %s），请稍后再试。" % status

    async def _render(self, player: Player, mode: str, uid: str) -> Path:
        if mode == "info":
            img = await asyncio.to_thread(build_info, player)
        else:
            img = await asyncio.to_thread(dc_image.build, player, mode)
        out = self._tmp_path(mode, uid)
        await asyncio.to_thread(img.save, str(out), "PNG")
        await asyncio.to_thread(self._clean_tmp)
        return out

    async def _notify(self, ctx: RequestContext, replies):
        if self.notifier is None:
            self.logger.info("[dancecube] 没有配置 notifier，跳过主动推送：%r", replies)
            return
        try:
            await self.notifier(ctx, list(replies))
        except Exception as e:  # noqa: BLE001
            self.logger.warning("登录结果主动推送失败：%s", e)

    # ------------------------------------------------------------ 登录 / 退出
    async def _login_scene_head(self, uid: str) -> str:
        old_tok = self.load_token(uid)
        if not old_tok:
            return "👤 新玩家登录：扫码后绑定你的舞立方账号。\n"
        try:
            old_player = Player(old_tok)
            old_info = (
                await asyncio.wait_for(
                    asyncio.to_thread(old_player.info), timeout=8
                )
                or {}
            )
            old_name = old_info.get("UserName")
            old_id = old_info.get("UserID")
            if old_name:
                return (
                    "🔄 切换账号：你当前已登录「%s」（角色 ID %s），"
                    "本次扫码会换成新账号。\n" % (old_name, old_id)
                )
        except (Exception, SystemExit):  # noqa: BLE001
            pass
        return "🔄 切换账号：检测到你已有登录凭证（可能已失效），本次扫码会重新绑定。\n"

    async def login(self, ctx: RequestContext):
        if not self.allowed(ctx):
            return
        uid = ctx.user_id
        if as_bool(self.config.get("login_admin_only"), False):
            owner = str(self.config.get("owner_qq") or "").strip()
            if not owner:
                yield Text(
                    "管理员开了「仅管理员可登录」却没配 owner_qq，现在谁都登不了；请联系管理员补配置。"
                )
                return
            if uid != owner:
                yield Text("管理员开启了「仅管理员可登录」，请联系管理员。")
                return
        wait = self._cooldown_wait(uid, seconds=10, kind="login")
        if wait:
            yield Text("登录操作太频繁，请 %d 秒后再试。" % wait)
            return
        running = self._pending.get(uid)
        if running and not running.done():
            yield Text("你已有一次登录流程正在进行中，请先扫码或等待二维码过期。")
            return

        head = await self._login_scene_head(uid)
        try:
            png, meta = await asyncio.to_thread(dc_login.request_qr)
        except Exception as e:  # noqa: BLE001
            yield Text("获取二维码失败：%s" % safe_error(e))
            return

        self._clean_tmp()
        qr_path = self._tmp_path("qr", uid)
        qr_path.write_bytes(png)
        timeout = int(self.config.get("qr_timeout_seconds", 120) or 120)

        # 先挂轮询任务，再发二维码：消费方提前结束迭代也不影响登录
        self._pending[uid] = asyncio.create_task(
            self._wait_login(ctx, uid, meta["id"], timeout)
        )

        yield Text(
            head
            + "请使用微信扫描下方二维码完成舞立方登录。\n"
            "⚠️ 非本人请勿扫码：扫码即授权本次指令的发起者使用你的账号。\n"
            "二维码只属于 %s，有效期 %d 秒。" % (uid, timeout)
        )
        yield Image(str(qr_path))

    async def _wait_login(self, ctx: RequestContext, uid: str, qr_id: str, timeout: int):
        try:
            # 先让二维码消息发出去，避免极端“秒扫”时成功消息排在二维码之前
            await asyncio.sleep(1.0)
            tok = await asyncio.to_thread(dc_login.poll_token, qr_id, timeout)
            if not tok:
                await self._notify(ctx, [Text("二维码已过期，请重新发送 /舞立方登录。")])
                return
            self.save_token(uid, tok)

            try:
                player = Player(tok)
                info = await asyncio.to_thread(player.info)
                info = info if isinstance(info, dict) else {}
                name = info.get("UserName") or "舞立方玩家"
                role_id = info.get("UserID") or "-"
                self.logger.info(
                    "账号绑定：发起者 %s · 角色 %s（%s）· 平台 %s",
                    uid, name, role_id, ctx.platform or "unknown",
                )
                img_path = await self._render(player, "info", uid)
                replies = [
                    Text("✅ 登录成功！已绑定角色：%s（ID %s）。\n这是你的个人信息：" % (name, role_id)),
                    Image(str(img_path)),
                ]
            except Exception as e:  # noqa: BLE001
                replies = [Text("✅ 登录成功，但个人信息生成失败：%s" % safe_error(e))]
            await self._notify(ctx, replies)
        except Exception as e:  # noqa: BLE001
            await self._notify(ctx, [Text("登录流程出错：%s" % safe_error(e))])
        finally:
            self._pending.pop(uid, None)

    async def quit(self, ctx: RequestContext):
        if not self.allowed(ctx):
            return
        if self.drop_token(ctx.user_id):
            yield Text("退出成功")
        else:
            yield Text("您未登录，无需退出")

    # ------------------------------------------------------------ 查询
    async def info_card(self, ctx: RequestContext):
        if not self.allowed(ctx):
            return
        player, err = await self._player(ctx)
        if not player:
            yield Text(err)
            return
        try:
            path = await self._render(player, "info", ctx.user_id)
            yield Image(str(path))
        except Exception as e:  # noqa: BLE001
            yield Text("查询失败：%s" % safe_error(e))

    async def ratio(self, ctx: RequestContext, mode: str):
        if not self.allowed(ctx):
            return
        player, err = await self._player(ctx)
        if not player:
            yield Text(err)
            return
        try:
            path = await self._render(player, mode, ctx.user_id)
            yield Image(str(path))
        except Exception as e:  # noqa: BLE001
            yield Text("生成战力分析图失败：%s" % safe_error(e))

    async def song(self, ctx: RequestContext, arg: str = ""):
        """查曲目或出全表；优先用完整消息文本剥命令，arg 只做兜底。"""
        if not self.allowed(ctx):
            return
        name = ""
        full = (ctx.text or "").strip()
        if full:
            for prefix in ("/", "／"):
                if full.startswith(prefix):
                    full = full[len(prefix):]
                    break
            for cmd in ("曲目", "查歌", "song", "音乐"):
                if full.startswith(cmd):
                    full = full[len(cmd):].strip()
                    break
            name = full
        if not name:
            name = (arg or "").strip()
        name = re.sub(r"[\s\u3000\u00a0]+", " ", name).strip()
        self.logger.info("曲目查询参数: %r", name)
        if not name:
            yield Text(
                "用法：/曲目 <曲名或ID>\n例如：/曲目 Calavera\n"
                "拉全表：/曲目 官谱全表 、/曲目 自制谱全表"
            )
            return
        kind = CATALOG_KEYS.get(name)
        if kind:
            async for reply in self._send_catalog(kind):
                yield reply
            return
        try:
            lib = self._music_lib()
            hits = await asyncio.to_thread(lib.find, name, 50)
        except Exception as e:  # noqa: BLE001
            yield Text("曲库查询失败：%s" % safe_error(e))
            return
        if not hits:
            if getattr(lib, "last_busy", False):
                yield Text("曲库正在全量拉取中，请过 3 分钟再试。")
                return
            yield Text("曲库里没有找到含\u300c%s\u300d的曲子。" % name)
            return
        if len(hits) == 1:
            record = await asyncio.to_thread(lib.probe_duration, hits[0])
            yield Text(lib.format(record))
            return
        show = hits[:10]
        lines = []
        for h in show:
            tag = "自制谱" if h.get("source") == "user" else "官谱"
            lines.append("%s（%s · ID %s）" % (h.get("name") or "?", tag, h.get("musicId")))
        raw = getattr(lib, "last_raw_total", len(hits))
        if raw != len(hits):
            head = "共 %d 首（去重后 %d 首），显示前 %d：" % (raw, len(hits), len(show))
        else:
            head = "共 %d 首，显示前 %d：" % (raw, len(show))
        yield Text("%s\n%s\n\n用 /曲目 <ID> 精确查询。" % (head, "\n".join(lines)))

    # ------------------------------------------------------------ 曲库全表
    def _catalog_dir(self) -> Path:
        d = self.tmp_dir / "catalog"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return d

    def _catalog_ttl(self) -> int:
        try:
            minutes = int(self.config.get("catalog_ttl_minutes", 30) or 30)
        except (TypeError, ValueError):
            minutes = 30
        return max(1, minutes) * 60

    def _catalog_age(self, path):
        try:
            st = path.stat()
            if st.st_size > 0:
                return time.time() - st.st_mtime
        except OSError:
            pass
        return None

    def _build_catalog(self, kind):
        lib = self._music_lib()
        path = self._catalog_dir() / ("%s.xlsx" % kind)
        if kind == "official":
            _, rows, songs = dc_catalog.build_official(lib, str(path))
            return path, rows, songs
        _, rows = dc_catalog.build_user(lib, str(path))
        return path, rows, None

    async def _send_catalog(self, kind: str):
        label = "官谱全表" if kind == "official" else "自制谱全表"
        path = self._catalog_dir() / ("%s.xlsx" % kind)

        age_sec = self._catalog_age(path)
        if age_sec is not None and age_sec < self._catalog_ttl():
            yield Text("%s（%d 分钟前生成，直接复用）" % (label, int(age_sec // 60)))
        else:
            yield Text("正在生成%s，请稍候…" % label)
            rows = songs = None
            err = None
            async with self._catalog_lock:
                age_sec = self._catalog_age(path)
                if age_sec is None or age_sec >= self._catalog_ttl():
                    try:
                        path, rows, songs = await asyncio.to_thread(
                            self._build_catalog, kind
                        )
                    except Exception as e:  # noqa: BLE001
                        err = e
            if err is not None:
                yield Text("生成%s失败：%s" % (label, safe_error(err)))
                return
            if rows is None:
                yield Text("%s（刚有人生成好，直接复用）" % label)
            else:
                extra = "，%d 首曲目" % songs if songs else ""
                yield Text("%s已生成：%d 行%s" % (label, rows, extra))

        yield Text("手机QQ内浏览表格无筛选功能，请下载后使用其他应用打开。")
        shown = "%s_%s.xlsx" % (label, time.strftime("%Y%m%d"))
        yield File(str(path), shown)

    # ------------------------------------------------------------ 导出成绩
    async def export(self, ctx: RequestContext):
        if not self.allowed(ctx):
            return
        player, err = await self._player(ctx)
        if not player:
            yield Text(err)
            return
        yield Text("正在拉取全部成绩并生成 xlsx，请稍候…")
        out_dir = self._export_dir()
        try:
            path, n, name, role_id = await asyncio.to_thread(
                export_scores, player, out_dir
            )
        except Exception as e:  # noqa: BLE001
            yield Text("导出失败：%s" % safe_error(e))
            return
        self.logger.info("成绩导出：%s（角色 %s）%d 条 -> %s", name, role_id, n, path)
        yield Text("手机QQ内浏览表格无筛选功能，请下载后使用其他应用打开。")
        yield File(str(path), Path(path).name, )

    # ------------------------------------------------------------ 谱面推送
    def _export_dir(self) -> str:
        """导出目录固定在插件数据目录内；配置的路径越界时忽略。"""
        base = self.data_dir.resolve()
        raw = str(self.config.get("export_dir") or "").strip()
        if raw:
            try:
                p = Path(raw).expanduser().resolve()
                if os.path.commonpath([str(p), str(base)]) == str(base):
                    p.mkdir(parents=True, exist_ok=True)
                    return str(p)
            except (ValueError, OSError):
                pass
            self.logger.warning(
                "[dancecube] export_dir 不在插件数据目录内，已忽略：%s", raw
            )
        out = base / "export"
        out.mkdir(parents=True, exist_ok=True)
        return str(out)

    def _is_owner(self, ctx: RequestContext) -> bool:
        owner = str(self.config.get("owner_qq") or "").strip()
        return bool(owner) and ctx.user_id == owner

    async def push_toggle(self, ctx: RequestContext, arg: str = ""):
        """打开/关闭/查看本群的谱面更新推送（开关仅主控可用）。"""
        low = (arg or "").strip().lower()
        if low in ("开", "on", "开启", "打开", "订阅"):
            if not self._is_owner(ctx):
                yield Text("只有主控可以开启谱面推送。")
                return
            if not ctx.origin:
                yield Text("拿不到会话标识，开启失败。")
                return
            self._push.set_enabled(ctx.origin, True)
            yield Text("已开启谱面更新推送：每天 08:00 有更新时发送到这里。")
            return
        if low in ("关", "off", "关闭", "退订"):
            if not self._is_owner(ctx):
                yield Text("只有主控可以关闭谱面推送。")
                return
            if self._push.set_enabled(ctx.origin, False):
                yield Text("已关闭谱面更新推送。")
            else:
                yield Text("这里本来就没开推送。")
            return
        if self._push.is_enabled(ctx.origin):
            yield Text("本群已开启谱面更新推送（每天 08:00，有更新才发）。")
        else:
            yield Text("本群未开启谱面更新推送。主控可用「/曲目推送 开」开启。")

    # ------------------------------------------------------------ 生命周期
    def _ensure_music_cache(self, bundled_dir):
        if not bundled_dir:
            return
        bundled = Path(bundled_dir)
        dest = self.data_dir / "music"
        if not bundled.is_dir():
            return
        try:
            dest.mkdir(parents=True, exist_ok=True)
            for src in bundled.glob("*.json"):
                target = dest / src.name
                if not target.exists():
                    shutil.copy(src, target)  # 用 copy 不用 copy2：让 mtime 变成此刻
        except OSError:
            pass

    def _music_lib(self):
        if self._mlib is None:
            self._mlib = dc_music.MusicLib(
                str(self.data_dir / "music" / "music_library.json"),
                str(self.data_dir.parent),
            )
        return self._mlib

    # ------------------------------------------------------------ 定时刷新
    def start(self):
        """启动曲库定时刷新（需要运行中的事件循环，插件初始化时调用）。"""
        if self._sched_task is not None and not self._sched_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.logger.warning("[dancecube] 没有运行中的事件循环，跳过定时刷新")
            return
        self._sched_task = loop.create_task(self._scheduler_loop())
        self.logger.info("[dancecube] 曲库定时刷新已启动（每天 03:00-05:00 随机时刻）")

    def _next_refresh_delay(self) -> float:
        """距离下一次 03:00-05:00 随机时刻的秒数。"""
        now = datetime.now()
        base = now.replace(hour=3, minute=0, second=0, microsecond=0)
        target = base + timedelta(minutes=random.randint(0, 120))
        if target <= now:
            target = base + timedelta(days=1) + timedelta(minutes=random.randint(0, 120))
        return max(60.0, (target - now).total_seconds())

    def _seconds_until_push(self) -> float:
        """距离下一个 08:00 推送点的秒数。"""
        now = datetime.now()
        target = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return max(30.0, (target - now).total_seconds())

    async def _scheduler_loop(self):
        while True:
            try:
                await asyncio.sleep(self._next_refresh_delay())
                await self._refresh_libraries()  # 只检测，把变更写进 pending
                await asyncio.sleep(self._seconds_until_push())  # 等早八
                await self._push_pending()  # 统一推给已开启的会话
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.logger.warning("[dancecube] 定时任务异常：%s", e)
                await asyncio.sleep(600)

    async def _refresh_libraries(self):
        """官谱先刷、再刷自制谱；刷新前后各拍一次快照，差异记入 pending。"""
        lib = self._music_lib()
        old = await asyncio.to_thread(self._snapshot, lib)
        ok = await asyncio.to_thread(lib.refresh_official)
        while not ok:
            self.logger.warning("[dancecube] 官谱定时刷新失败，30 分钟后重试")
            await asyncio.sleep(30 * 60)
            ok = await asyncio.to_thread(lib.refresh_official)
        self.logger.info("[dancecube] 官谱已定时刷新")

        ok = await asyncio.to_thread(lib.refresh_user)
        while not ok:
            self.logger.warning("[dancecube] 自制谱定时刷新失败，30 分钟后重试")
            await asyncio.sleep(30 * 60)
            ok = await asyncio.to_thread(lib.refresh_user)
        self.logger.info("[dancecube] 自制谱已定时刷新")

        new = await asyncio.to_thread(self._snapshot, lib)
        self._record_changes(old, new)

    @staticmethod
    def _snapshot(lib) -> dict:
        """直接读缓存文件得到 {MusicID: 曲名}，不触发加载或请求。"""
        out = {}
        for key, path in (("official", lib.official_path()), ("user", lib.user_path())):
            items = {}
            try:
                with open(path, encoding="utf-8") as f:
                    for x in json.load(f):
                        if not isinstance(x, dict):
                            continue
                        mid = str(x.get("MusicID") or x.get("musicId") or "")
                        if mid:
                            items[mid] = str(x.get("Name") or x.get("name") or mid)
            except (OSError, ValueError):
                pass
            out[key] = items
        return out

    def _record_changes(self, old, new):
        changes = {}
        for key in ("official", "user"):
            o, n = old.get(key) or {}, new.get(key) or {}
            changes[key + "_new"] = [n[k] for k in n if k not in o]
            changes[key + "_gone"] = [o[k] for k in o if k not in n]
        if not any(changes.values()):
            return
        changes["date"] = datetime.now().strftime("%Y-%m-%d")
        self._push.save_pending(changes)
        self.logger.info(
            "[dancecube] 曲库变更：官谱 +%d/-%d，自制谱 +%d/-%d（等 08:00 推送）",
            len(changes["official_new"]),
            len(changes["official_gone"]),
            len(changes["user_new"]),
            len(changes["user_gone"]),
        )

    @staticmethod
    def _format_changes(pending) -> str:
        date = str(pending.get("date") or "")[5:] or datetime.now().strftime("%m-%d")
        lines = ["【舞立方 · 曲库更新 %s】" % date]
        for key, label in (
            ("official_new", "官谱新增"),
            ("official_gone", "官谱下架"),
            ("user_new", "自制谱新增"),
            ("user_gone", "自制谱下架"),
        ):
            items = pending.get(key) or []
            if items:
                lines.append("%s %d 首：%s" % (label, len(items), "、".join(items)))
        lines.append("完整列表：/曲目 官谱全表")
        return "\n".join(lines)

    async def _push_pending(self):
        """把早八该发的变更推给所有已开启的会话，推完即清。"""
        if not self.notifier:
            self.logger.warning("[dancecube] 没有配置 notifier，曲库更新无法推送")
            return
        pending = self._push.take_pending()
        if not pending:
            return
        text = self._format_changes(pending)
        targets = self._push.enabled_targets()
        failed = 0
        for t in targets:
            umo = str(t.get("umo") or "").strip()
            if not umo:
                continue
            try:
                await self.notifier(RequestContext(user_id="", origin=umo), [Text(text)])
            except Exception as e:  # noqa: BLE001
                failed += 1
                self.logger.warning("[dancecube] 推送失败（%s）：%s", umo, e)
        if targets and failed == len(targets):
            # 全部失败：保留变更，下次早八再试，避免一口气全丢
            self._push.save_pending(pending)
            self.logger.warning("[dancecube] 全部推送失败，变更保留到下次")
            return
        self.logger.info("[dancecube] 曲库更新已推送到 %d 个会话", len(targets))

    def terminate(self):
        if self._sched_task is not None:
            try:
                self._sched_task.cancel()
            except Exception:  # noqa: BLE001
                pass
            self._sched_task = None
        for task in list(self._pending.values()):
            try:
                task.cancel()
            except Exception:  # noqa: BLE001
                pass
        self._pending.clear()
