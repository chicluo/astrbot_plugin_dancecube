"""舞立方（Dance Cube）QQ 机器人插件。

命令：
  /舞立方登录  —— 生成二维码，仅触发者扫码；成功后记住该 QQ 的 token 并展示个人信息
  /mydc        —— 个人信息图（积分 / 全连率 / 全国排名 / 金币 / 游玩次数 / 天梯）
  /myrt        —— B15 + R15 战力分析图
  /myrtb30     —— B30 战力分析图
  /myrtap30    —— AP30 战力分析图
  /导出成绩    —— 导出全部成绩为 xlsx（保存到管理员本机）
  /舞立方退出  —— 清除当前 QQ 的 token

权限：好友私聊可用；群聊需群号在 allowed_groups 配置里。
"""
import asyncio
import json
import re
import time
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core.platform.message_type import MessageType
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .core import image as dc_image
from .core import login as dc_login
from .core.client import Player
from .core.export import DEFAULT_EXPORT_DIR, export_scores
from .core.profile_image import build_info

PLUGIN_NAME = "astrbot_plugin_dancecube"


class DanceCube(star.Star):
    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config
        base = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.tokens_dir = base / "tokens"
        self.tmp_dir = base / "tmp"
        self.tokens_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._pending: dict[str, asyncio.Task] = {}
        self._last_call: dict[str, float] = {}
        self._ensure_music_cache()

    # ------------------------------------------------------------ 基础工具
    def _allowed(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id() or "")
        owner = str(self.config.get("owner_qq") or "").strip()
        if owner and sender == owner:
            return True  # 机器人主人：不受群白名单限制
        message_type = event.get_message_type()
        if message_type == MessageType.FRIEND_MESSAGE:
            return bool(self.config.get("allow_private", True))
        if message_type == MessageType.GROUP_MESSAGE:
            groups = {
                str(g).strip()
                for g in (self.config.get("allowed_groups") or [])
                if str(g).strip()
            }
            if "*" in groups:
                return True  # 所有群放行
            return str(event.get_group_id() or "") in groups
        # 其它消息类型（官方机器人偶发的 OTHER_MESSAGE）按私聊处理
        if message_type == MessageType.OTHER_MESSAGE:
            return bool(self.config.get("allow_private", True))
        return False

    def _token_path(self, qq: str) -> Path:
        return self.tokens_dir / f"{qq}.json"

    def _load_token(self, qq: str):
        path = self._token_path(qq)
        if not path.exists():
            return None
        try:
            tok = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        return tok if tok.get("access_token") else None

    def _save_token(self, qq: str, tok: dict) -> None:
        self._token_path(qq).write_text(
            json.dumps(tok, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _drop_token(self, qq: str) -> bool:
        path = self._token_path(qq)
        if path.exists():
            path.unlink()
            return True
        return False

    def _tmp_path(self, prefix: str, qq: str) -> Path:
        return self.tmp_dir / f"{prefix}_{qq}_{int(time.time() * 1000)}.png"

    def _clean_tmp(self, keep_seconds: int = 3600) -> None:
        deadline = time.time() - keep_seconds
        for p in self.tmp_dir.glob("*.png"):
            try:
                if p.stat().st_mtime < deadline:
                    p.unlink()
            except OSError:
                pass

    def _cooldown_wait(self, qq: str, seconds: int = 30) -> int:
        """同一用户短时间内的重复请求拦截，返回需要等待的秒数（0 表示放行）。"""
        now = time.time()
        last = self._last_call.get(qq, 0.0)
        if now - last < seconds:
            return max(1, int(seconds - (now - last)))
        self._last_call[qq] = now
        return 0

    async def _player(self, event: AstrMessageEvent):
        """取当前用户 Player；返回 (player, 错误信息)。"""
        qq = str(event.get_sender_id())
        wait = self._cooldown_wait(qq)
        if wait:
            return None, f"操作太频繁，请 {wait} 秒后再试。"
        tok = self._load_token(qq)
        if not tok:
            return None, "你还没有登录舞立方。\n请先发送 /舞立方登录 扫码。"
        try:
            player = Player(tok)
            info = await asyncio.to_thread(player.info)
        except Exception as e:  # noqa: BLE001
            return None, f"连接舞立方失败：{e}"
        if not isinstance(info, dict) or not info.get("UserID"):
            self._drop_token(qq)
            return None, "你的登录凭证已失效，请重新发送 /舞立方登录 扫码。"
        return player, ""

    async def _render(self, player: Player, mode: str, qq: str) -> Path:
        """在线程池里出图，返回本地图片路径。"""
        if mode == "info":
            img = await asyncio.to_thread(build_info, player)
        else:
            img = await asyncio.to_thread(dc_image.build, player, mode)
        out = self._tmp_path(mode, qq)
        await asyncio.to_thread(img.save, str(out), "PNG")
        # 每次出图顺手清掉旧临时文件，避免长期运行堆积
        await asyncio.to_thread(self._clean_tmp)
        return out

    # ------------------------------------------------------------ 登录
    async def _login_scene_head(self, qq: str) -> str:
        """返回登录场景提示：新玩家登录 / 切换账号。"""
        old_tok = self._load_token(qq)
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
                    f"🔄 切换账号：你当前已登录「{old_name}」（角色 ID {old_id}），"
                    "本次扫码会换成新账号。\n"
                )
        except (Exception, SystemExit):  # noqa: BLE001
            pass
        return "🔄 切换账号：检测到你已有登录凭证（可能已失效），本次扫码会重新绑定。\n"

    @filter.command("舞立方登录", alias={"dclogin"})
    async def dc_login(self, event: AstrMessageEvent):
        if not self._allowed(event):
            return

        qq = str(event.get_sender_id())
        wait = self._cooldown_wait(qq, seconds=10)
        if wait:
            yield event.plain_result(f"登录操作太频繁，请 {wait} 秒后再试。")
            return
        running = self._pending.get(qq)
        if running and not running.done():
            yield event.plain_result("你已有一次登录流程正在进行中，请先扫码或等待二维码过期。")
            return

        # 判断是新玩家登录，还是已有凭证要换绑
        head = await self._login_scene_head(qq)

        try:
            png, meta = await asyncio.to_thread(dc_login.request_qr)
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"获取二维码失败：{e}")
            return

        self._clean_tmp()
        qr_path = self._tmp_path("qr", qq)
        qr_path.write_bytes(png)
        timeout = int(self.config.get("qr_timeout_seconds", 120) or 120)

        yield event.chain_result(
            [
                Comp.Plain(
                    head
                    + "请使用微信扫描下方二维码完成舞立方登录。\n"
                    f"⚠️ 此二维码只属于你（{event.get_sender_id()}），请勿转发给他人。\n"
                    f"有效期 {timeout} 秒。"
                ),
                Comp.Image.fromFileSystem(str(qr_path)),
            ]
        )

        self._pending[qq] = asyncio.create_task(
            self._wait_login(event, qq, meta["id"], timeout)
        )

    async def _notify(self, event: AstrMessageEvent, chain) -> None:
        """给触发者补发消息：优先用原事件做被动回复，失败再退回主动发送。"""
        if isinstance(chain, str):
            chain = MessageChain().message(chain)
        try:
            await event.send(chain)
            return
        except Exception as e:  # noqa: BLE001
            try:
                self.logger.warning("登录结果主动回复失败：%s", e)
            except Exception:  # noqa: BLE001
                pass
        try:
            await self.context.send_message(event.unified_msg_origin, chain)
        except Exception as e:  # noqa: BLE001
            try:
                self.logger.warning("登录结果兜底发送也失败：%s", e)
            except Exception:  # noqa: BLE001
                pass

    async def _wait_login(
        self, event: AstrMessageEvent, qq: str, qr_id: str, timeout: int
    ) -> None:
        try:
            tok = await asyncio.to_thread(dc_login.poll_token, qr_id, timeout)
            if not tok:
                await self._notify(event, "二维码已过期，请重新发送 /舞立方登录。")
                return
            self._save_token(qq, tok)

            try:
                player = Player(tok)
                info = await asyncio.to_thread(player.info)
                name = (info or {}).get("UserName") or "舞立方玩家"
                img_path = await self._render(player, "info", qq)
                chain = (
                    MessageChain()
                    .message(f"✅ 登录成功！欢迎你，{name}。\n这是你的个人信息：")
                    .file_image(str(img_path))
                )
            except Exception as e:  # noqa: BLE001
                chain = MessageChain().message(f"✅ 登录成功，但个人信息生成失败：{e}")
            await self._notify(event, chain)
        except Exception as e:  # noqa: BLE001
            await self._notify(event, MessageChain().message(f"登录流程出错：{e}"))
        finally:
            self._pending.pop(qq, None)

    @filter.command("舞立方退出", alias={"dcquit"})
    async def dc_quit(self, event: AstrMessageEvent):
        """退出登录：清掉本地 token；未登录时提示无需退出。"""
        if not self._allowed(event):
            return
        if self._drop_token(str(event.get_sender_id())):
            yield event.plain_result("退出成功")
        else:
            yield event.plain_result("您未登录，无需退出")

    # ------------------------------------------------------------ 查询
    @filter.command("mydc", alias={"个人信息"})
    async def mydc(self, event: AstrMessageEvent):
        if not self._allowed(event):
            return
        player, err = await self._player(event)
        if not player:
            yield event.plain_result(err)
            return
        try:
            path = await self._render(player, "info", str(event.get_sender_id()))
            yield event.image_result(str(path))
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"查询失败：{e}")

    @filter.command("myrt", alias={"战力分析"})
    async def myrt(self, event: AstrMessageEvent):
        result = await self._send_ratio(event, "both")
        if result is not None:
            yield result

    @filter.command("myrtb30", alias={"战力分析b30"})
    async def myrtb30(self, event: AstrMessageEvent):
        result = await self._send_ratio(event, "b30")
        if result is not None:
            yield result

    @filter.command("myrtap30", alias={"战力分析ap30"})
    async def myrtap30(self, event: AstrMessageEvent):
        result = await self._send_ratio(event, "ap30")
        if result is not None:
            yield result

    @filter.command("曲目", alias={"查歌", "song", "音乐"})
    async def dc_song(self, event: AstrMessageEvent, name: str = ""):
        """查曲目：音乐ID / 各难度定数 / 物量 / 时长 / 比特率。"""
        if not self._allowed(event):
            return
        # 规范化空白：QQ 里可能带全角空格或不间断空格
        # AstrBot 的 name: str 只拿第一个空格前的 token，这里用完整消息文本补全
        full = (event.get_message_str() or "").strip()
        for prefix in ("/", "／"):
            if full.startswith(prefix):
                full = full[len(prefix):]
                break
        for cmd in ("曲目", "查歌", "song", "音乐"):
            if full.startswith(cmd):
                full = full[len(cmd):].strip()
                break
        if full:
            name = full
        raw_name = name
        name = re.sub(r"[\s\u3000\u00a0]+", " ", (name or "")).strip()
        try:
            self.logger.info("曲目查询参数: raw=%r normalized=%r", raw_name, name)
        except Exception:  # noqa: BLE001
            pass
        if not name:
            yield event.plain_result("用法：/曲目 <曲名或ID>\n例如：/曲目 Calavera")
            return
        try:
            lib = self._music_lib()
            hits = await asyncio.to_thread(lib.find, name, 50)
        except Exception as e:  # noqa: BLE001
            yield event.plain_result("曲库查询失败：%s" % e)
            return
        if not hits:
            yield event.plain_result("曲库里没有找到含\u300c%s\u300d的曲子。" % name)
            return
        if len(hits) == 1:
            record = await asyncio.to_thread(lib.probe_duration, hits[0])
            yield event.plain_result(lib.format(record))
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
        yield event.plain_result(
            "%s\n%s\n\n用 /曲目 <ID> 精确查询。" % (head, "\n".join(lines))
        )

    def _ensure_music_cache(self):
        """首次部署时把插件自带的曲库快照复制到 plugin_data，省一次全量拉取。"""
        import shutil
        from pathlib import Path

        bundled = Path(__file__).resolve().parent / "data"
        dest = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_dancecube" / "music"
        if not bundled.is_dir():
            return
        try:
            dest.mkdir(parents=True, exist_ok=True)
            for src in bundled.glob("*.json"):
                target = dest / src.name
                if not target.exists():
                    shutil.copy(src, target)  # 用 copy 不用 copy2：让 mtime 变成此刻，避免一复制就被判过期
        except OSError:
            pass

    def _music_lib(self):
        if getattr(self, "_mlib", None) is None:
            import os as _os
            from .core import music as music_lib_mod

            self._ensure_music_cache()
            base = get_astrbot_plugin_data_path()
            self._mlib = music_lib_mod.MusicLib(
                _os.path.join(base, "astrbot_plugin_dancecube", "music", "music_library.json"),
                base,
            )
        return self._mlib

    @filter.command("导出成绩", alias={"dcxlsx", "成绩导出"})
    async def dc_xlsx(self, event: AstrMessageEvent):
        """把该用户全部成绩导出为 xlsx；优先直接发文件，失败再提示联系管理员。"""
        if not self._allowed(event):
            return
        player, err = await self._player(event)
        if not player:
            yield event.plain_result(err)
            return

        yield event.plain_result("正在拉取全部成绩并生成 xlsx，请稍候…")

        out_dir = str(self.config.get("export_dir") or DEFAULT_EXPORT_DIR)
        try:
            path, n, name, role_id = await asyncio.to_thread(
                export_scores, player, out_dir
            )
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"导出失败：{e}")
            return

        # 路径只记进日志，不回复给用户
        try:
            self.logger.info("成绩导出：%s（角色 %s）%d 条 -> %s", name, role_id, n, path)
        except Exception:  # noqa: BLE001
            pass

        # 先尝试把文件直接发给用户；成功则不再提示，失败才让用户联系管理员
        try:
            await event.send(
                MessageChain([Comp.File(name=Path(path).name, file=str(path))])
            )
        except Exception as e:  # noqa: BLE001
            try:
                self.logger.warning("成绩文件发送失败：%s", e)
            except Exception:  # noqa: BLE001
                pass
            yield event.plain_result(
                "✅ 导出完成：%s（角色 ID %s）· 共 %d 条成绩\n"
                "成绩表已导出到管理员本机，如需获取文件请联系管理员。"
                % (name, role_id, n)
            )

    async def _send_ratio(self, event: AstrMessageEvent, mode: str):
        if not self._allowed(event):
            return None
        player, err = await self._player(event)
        if not player:
            return event.plain_result(err)
        try:
            path = await self._render(player, mode, str(event.get_sender_id()))
            return event.image_result(str(path))
        except Exception as e:  # noqa: BLE001
            return event.plain_result(f"生成战力分析图失败：{e}")
