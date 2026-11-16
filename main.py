"""舞立方（Dance Cube）QQ 机器人插件。

命令：
  /舞立方登录  —— 生成二维码，仅触发者扫码；成功后记住该 QQ 的 token 并展示个人信息
  /mydc        —— 个人信息图（积分 / 全连率 / 全国排名 / 金币 / 游玩次数 / 天梯）
  /myrt        —— B15 + R15 战力分析图
  /myrtb30     —— B30 战力分析图
  /myrtap30    —— AP30 战力分析图
  /导出成绩    —— 导出全部成绩为 xlsx（保存到管理员本机）
  /舞立方退出  —— 清除当前 QQ 的 token

权限：好友私聊可用（仅 aiocqhttp）；群聊需群标识在 allowed_groups 配置里，
      aiocqhttp 填真实群号，qq_official 填 group_openid。
"""
import asyncio
import json
import os
import re
import time
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core.platform.message_type import MessageType
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .core import catalog as dc_catalog
from .core import client as dc_client
from .core import image as dc_image
from .core import login as dc_login
from .core import net
from .core.client import Player
from .core.export import DEFAULT_EXPORT_DIR, export_scores
from .core.profile_image import build_info
from .core.safe import safe_error

PLUGIN_NAME = "astrbot_plugin_dancecube"


def _harden_dir(path):
    """尽量把凭证目录收紧到 0700（Windows 上 chmod 只影响只读位，不报错）。"""
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _safe_qq(qq: str) -> str:
    """QQ/openid 只保留安全字符，避免拼路径时被 ../ 之类带出去。"""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(qq or "user"))


def _as_bool(value, default=False):
    """配置可能被手改成字符串，bool("false") 是 True，必须显式解析。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")

# 「/曲目 官谱全表」「/曲目 自制谱全表」：直接出 xlsx，不按曲名搜
CATALOG_KEYS = {
    "官谱全表": "official",
    "官谱表": "official",
    "官谱表格": "official",
    "自制谱全表": "user",
    "自制谱表": "user",
    "自制谱表格": "user",
}


def _file_send_hint(err):
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


class DanceCube(star.Star):
    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config
        # 安全：默认校验证书；只有显式打开 insecure_tls 才退回旧行为
        try:
            insecure = _as_bool(config.get("insecure_tls"), False)
        except Exception:  # noqa: BLE001
            insecure = False
        net.set_verify_tls(not insecure)
        if insecure:
            self.logger.warning(
                "[dancecube] 已关闭 TLS 证书校验（insecure_tls=true），token 可能被中间人截获"
            )
        base = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.tokens_dir = base / "tokens"
        self.tmp_dir = base / "tmp"
        self.tokens_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        _harden_dir(self.tokens_dir)
        # 让 Player 的曲库缓存直接用 plugin_data 的同一份快照，避免两套缓存
        dc_client.set_music_library_path(base / "music" / "music_library.json")
        self._pending: dict[str, asyncio.Task] = {}
        self._last_call: dict[str, float] = {}
        self._catalog_lock = asyncio.Lock()
        self._ensure_music_cache()
        groups = [str(g).strip() for g in (config.get("allowed_groups") or [])]
        if "*" in groups:
            self.logger.warning(
                "[dancecube] allowed_groups 含 *，所有群都能使用登录入口，请确认这是本意"
            )

    # ------------------------------------------------------------ 基础工具
    def _platform(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_platform_name() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _warn_openid_once(self) -> None:
        if getattr(self, "_warned_openid", False):
            return
        self._warned_openid = True
        self.logger.warning(
            "[dancecube] allowed_groups 里填的是数字群号，但 qq_official 平台拿到的是 "
            "group_openid，永远匹配不上；请改填 openid，或用 * 放开全部群"
        )

    def _allowed(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id() or "")
        owner = str(self.config.get("owner_qq") or "").strip()
        if owner and sender == owner:
            return True  # 机器人主人：不受群白名单限制
        message_type = event.get_message_type()
        if message_type == MessageType.FRIEND_MESSAGE:
            return _as_bool(self.config.get("allow_private"), True)
        if message_type == MessageType.GROUP_MESSAGE:
            groups = {
                str(g).strip()
                for g in (self.config.get("allowed_groups") or [])
                if str(g).strip()
            }
            if "*" in groups:
                return True  # 所有群放行
            gid = str(event.get_group_id() or "")
            if gid and gid in groups:
                return True
            # 官方平台的群标识是 openid，填真实群号永远匹配不上，提示一次
            if gid and self._platform(event) == "qq_official" and any(
                g.isdigit() for g in groups
            ):
                self._warn_openid_once()
            return False
        # 其它消息类型（官方机器人偶发的 OTHER_MESSAGE）按私聊处理
        if message_type == MessageType.OTHER_MESSAGE:
            return _as_bool(self.config.get("allow_private"), True)
        return False

    def _token_path(self, qq: str) -> Path:
        return self.tokens_dir / f"{_safe_qq(qq)}.json"

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
        path = self._token_path(qq)
        path.write_text(
            json.dumps(tok, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            os.chmod(path, 0o600)  # 只有运行账号能读
        except OSError:
            pass

    def _drop_token(self, qq: str) -> bool:
        path = self._token_path(qq)
        if path.exists():
            path.unlink()
            return True
        return False

    def _tmp_path(self, prefix: str, qq: str) -> Path:
        return self.tmp_dir / f"{prefix}_{_safe_qq(qq)}_{int(time.time() * 1000)}.png"

    def _clean_tmp(self, keep_seconds: int = 3600) -> None:
        deadline = time.time() - keep_seconds
        for p in self.tmp_dir.glob("*.png"):
            try:
                if p.stat().st_mtime < deadline:
                    p.unlink()
            except OSError:
                pass

    def _cooldown_wait(self, qq: str, seconds: int = 30, kind: str = "query") -> int:
        """同一用户短时间内的重复请求拦截，返回需要等待的秒数（0 表示放行）。

        登录与查询分开计时：扫码成功后立刻查一下不会被 30 秒窗口挡住。
        """
        key = "%s:%s" % (kind, qq)
        now = time.time()
        last = self._last_call.get(key, 0.0)
        if now - last < seconds:
            return max(1, int(seconds - (now - last)))
        self._last_call[key] = now
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
            status, info = await asyncio.to_thread(player.info_raw)
        except Exception as e:  # noqa: BLE001
            return None, "连接舞立方失败：%s" % safe_error(e)
        if isinstance(info, dict) and info.get("UserID"):
            return player, ""
        # 只有明确的认证失败才删 token；网络抖动保留凭证，免得用户重新扫码
        if status in (401, 403):
            self._drop_token(qq)
            return None, "你的登录凭证已失效，请重新发送 /舞立方登录 扫码。"
        if status == 0:
            return None, "连接舞立方失败（网络超时），请稍后再试。"
        return None, "舞立方接口返回异常（HTTP %s），请稍后再试。" % status

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
        if _as_bool(self.config.get("login_admin_only"), False):
            owner = str(self.config.get("owner_qq") or "").strip()
            if not owner or qq != owner:
                yield event.plain_result("管理员开启了「仅管理员可登录」，请联系管理员。")
                return
        wait = self._cooldown_wait(qq, seconds=10, kind="login")
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
            yield event.plain_result("获取二维码失败：%s" % safe_error(e))
            return

        self._clean_tmp()
        qr_path = self._tmp_path("qr", qq)
        qr_path.write_bytes(png)
        timeout = int(self.config.get("qr_timeout_seconds", 120) or 120)

        # 先把轮询任务挂上，再发消息：万一消费方提前结束迭代，登录流程也不会丢
        self._pending[qq] = asyncio.create_task(
            self._wait_login(event, qq, meta["id"], timeout)
        )

        yield event.chain_result(
            [
                Comp.Plain(
                    head
                    + "请使用微信扫描下方二维码完成舞立方登录。\n"
                    "⚠️ 非本人请勿扫码：扫码即授权本次指令的发起者使用你的账号。\n"
                    f"二维码只属于 {event.get_sender_id()}，有效期 {timeout} 秒。"
                ),
                Comp.Image.fromFileSystem(str(qr_path)),
            ]
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
            # 先让二维码消息发出去，避免极端“秒扫”时成功消息排在二维码之前
            await asyncio.sleep(1.0)
            tok = await asyncio.to_thread(dc_login.poll_token, qr_id, timeout)
            if not tok:
                await self._notify(event, "二维码已过期，请重新发送 /舞立方登录。")
                return
            self._save_token(qq, tok)

            try:
                player = Player(tok)
                info = await asyncio.to_thread(player.info)
                info = info if isinstance(info, dict) else {}
                name = info.get("UserName") or "舞立方玩家"
                role_id = info.get("UserID") or "-"
                # 审计日志：谁把哪个角色绑到了哪个 QQ/openid
                try:
                    self.logger.info(
                        "账号绑定：发起者 %s · 角色 %s（%s）· 平台 %s",
                        qq,
                        name,
                        role_id,
                        self._platform(event) or "unknown",
                    )
                except Exception:  # noqa: BLE001
                    pass
                img_path = await self._render(player, "info", qq)
                chain = (
                    MessageChain()
                    .message(
                        f"✅ 登录成功！已绑定角色：{name}（ID {role_id}）。\n"
                        "这是你的个人信息："
                    )
                    .file_image(str(img_path))
                )
            except Exception as e:  # noqa: BLE001
                chain = MessageChain().message(
                    "✅ 登录成功，但个人信息生成失败：%s" % safe_error(e)
                )
            await self._notify(event, chain)
        except Exception as e:  # noqa: BLE001
            await self._notify(
                event, MessageChain().message("登录流程出错：%s" % safe_error(e))
            )
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
            yield event.plain_result("查询失败：%s" % safe_error(e))

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
            yield event.plain_result(
                "用法：/曲目 <曲名或ID>\n例如：/曲目 Calavera\n"
                "拉全表：/曲目 官谱全表 、/曲目 自制谱全表"
            )
            return
        kind = CATALOG_KEYS.get(name)
        if kind:
            async for item in self._send_catalog(event, kind):
                yield item
            return
        try:
            lib = self._music_lib()
            hits = await asyncio.to_thread(lib.find, name, 50)
        except Exception as e:  # noqa: BLE001
            yield event.plain_result("曲库查询失败：%s" % safe_error(e))
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
        """缓存文件年龄（秒）；不存在或 0 字节返回 None。"""
        try:
            st = path.stat()
            if st.st_size > 0:  # 0 字节说明上次写失败，不能复用
                return time.time() - st.st_mtime
        except OSError:
            pass
        return None

    def _build_catalog(self, kind):
        """生成全表文件，返回 (路径, 行数, 曲目数)。"""
        lib = self._music_lib()
        path = self._catalog_dir() / ("%s.xlsx" % kind)
        if kind == "official":
            _, rows, songs = dc_catalog.build_official(lib, str(path))
            return path, rows, songs
        _, rows = dc_catalog.build_user(lib, str(path))
        return path, rows, None

    async def _send_catalog(self, event: AstrMessageEvent, kind: str):
        """出一份曲库全表：TTL 内直接重发上次生成的文件，避免反复拉库。"""
        label = "官谱全表" if kind == "official" else "自制谱全表"
        path = self._catalog_dir() / ("%s.xlsx" % kind)

        age_sec = self._catalog_age(path)
        if age_sec is not None and age_sec < self._catalog_ttl():
            yield event.plain_result(
                "%s（%d 分钟前生成，直接复用）" % (label, int(age_sec // 60))
            )
        else:
            yield event.plain_result("正在生成%s，请稍候…" % label)
            rows = songs = None
            err = None
            # 锁只包「检查 + 生成」，yield 全在锁外
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
                yield event.plain_result("生成%s失败：%s" % (label, safe_error(err)))
                return
            if rows is None:
                yield event.plain_result("%s（刚有人生成好，直接复用）" % label)
            else:
                extra = "，%d 首曲目" % songs if songs else ""
                yield event.plain_result("%s已生成：%d 行%s" % (label, rows, extra))

        shown = "%s_%s.xlsx" % (label, time.strftime("%Y%m%d"))
        try:
            await event.send(MessageChain([Comp.File(name=shown, file=str(path))]))
        except Exception as e:  # noqa: BLE001
            try:
                self.logger.warning("%s发送失败：%s", label, e)
            except Exception:  # noqa: BLE001
                pass
            yield event.plain_result(
                "文件发送失败：%s\n详情：%s\n"
                "官方接口不提供群文件容量查询，机器人发的文件也不占群文件空间，"
                "所以群文件满不是这里的原因；反复失败请让管理员从本机取。"
                % (_file_send_hint(e), safe_error(e))
            )

    async def terminate(self):
        """插件卸载/重载：取消还在轮询的登录任务，避免后台继续写 token。"""
        for task in list(self._pending.values()):
            try:
                task.cancel()
            except Exception:  # noqa: BLE001
                pass
        self._pending.clear()
        parent = getattr(super(), "terminate", None)
        if parent is not None:
            try:
                await parent()
            except Exception:  # noqa: BLE001
                pass

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
            yield event.plain_result("导出失败：%s" % safe_error(e))
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
            return event.plain_result("生成战力分析图失败：%s" % safe_error(e))
