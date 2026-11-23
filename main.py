"""舞立方（Dance Cube）QQ 机器人插件 —— AstrBot 入口。

命令：
  /舞立方登录  —— 生成二维码，仅触发者扫码；成功后记住该 QQ 的 token 并展示个人信息
  /mydc        —— 个人信息图（积分 / 全连率 / 全国排名 / 金币 / 游玩次数 / 天梯）
  /myrt        —— B15 + R15 战力分析图
  /myrtb30     —— B30 战力分析图
  /myrtap30    —— AP30 战力分析图
  /曲目        —— 曲库查询 / 官谱全表 / 自制谱全表
  /曲库补全    —— 批量探测缺时长/码率的自制谱（仅主控）；期间单曲查询会提示稍后再试
  /导出成绩    —— 导出全部成绩为 xlsx
  /舞立方退出  —— 清除当前 QQ 的 token

业务逻辑全在 `core/service.py`，这一层只做「AstrBot 事件 ↔ RequestContext/Reply」
的翻译。换其他 bot 框架时，照这个文件再写一个 adapter 即可，core 不用动。

权限：好友私聊可用（仅 aiocqhttp）；群聊需群标识在 allowed_groups 配置里，
      aiocqhttp 填真实群号，qq_official 填 group_openid。
"""
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core.platform.message_type import MessageType
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .core.service import (
    DanceCubeService,
    File,
    Image,
    RequestContext,
    Text,
    file_send_hint,
    safe_error,
)

PLUGIN_NAME = "astrbot_plugin_dancecube"


class DanceCube(star.Star):
    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        base = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.service = DanceCubeService(
            config=config,
            data_dir=base,
            bundled_data_dir=Path(__file__).resolve().parent / "data",
            notifier=self._notify,
            logger=self.logger,
        )
        self.service.start()

    # ------------------------------------------------------------ 翻译层
    def _platform(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_platform_name() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _ctx(self, event: AstrMessageEvent) -> RequestContext:
        try:
            message_type = event.get_message_type()
        except Exception:  # noqa: BLE001
            message_type = MessageType.OTHER_MESSAGE
        return RequestContext(
            user_id=str(event.get_sender_id() or ""),
            group_id=str(event.get_group_id() or ""),
            is_group=(message_type == MessageType.GROUP_MESSAGE),
            platform=self._platform(event),
            text=event.get_message_str() or "",
            origin=getattr(event, "unified_msg_origin", "") or "",
        )

    async def _notify(self, ctx: RequestContext, replies) -> None:
        """登录完成后的主动推送：按会话标识补发，不依赖原事件句柄。"""
        chain = MessageChain()
        for r in replies:
            if isinstance(r, Text):
                chain.message(r.text)
            elif isinstance(r, Image):
                chain.file_image(r.path)
        if not chain.chain or not ctx.origin:
            return
        await self.context.send_message(ctx.origin, chain)

    async def _emit(self, event: AstrMessageEvent, gen):
        """把 Service 的 Reply 流转成 AstrBot 的消息组件。"""
        pending = None  # 攒一条文本，和紧随其后的图片合并成一条消息
        async for r in gen:
            if isinstance(r, Text):
                if pending is not None:
                    yield event.plain_result(pending)
                pending = r.text
            elif isinstance(r, Image):
                if pending is not None:
                    yield event.chain_result(
                        [
                            Comp.Plain(pending),
                            Comp.Image.fromFileSystem(r.path),
                        ]
                    )
                    pending = None
                else:
                    yield event.image_result(r.path)
            elif isinstance(r, File):
                if pending is not None:
                    yield event.plain_result(pending)
                    pending = None
                try:
                    await event.send(
                        MessageChain([Comp.File(name=r.name, file=r.path)])
                    )
                except Exception as e:  # noqa: BLE001
                    self.logger.warning("文件发送失败：%s", e)
                    yield event.plain_result(
                        "文件发送失败：%s\n详情：%s\n"
                        "官方接口不提供群文件容量查询，机器人发的文件也不占群文件空间，"
                        "所以群文件满不是这里的原因；反复失败请让管理员从本机取。"
                        % (file_send_hint(e), safe_error(e))
                    )
        if pending is not None:
            yield event.plain_result(pending)

    async def terminate(self):
        self.service.terminate()
        parent = getattr(super(), "terminate", None)
        if parent is not None:
            try:
                await parent()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ 命令
    @filter.command("舞立方登录", alias={"dclogin"})
    async def dc_login(self, event: AstrMessageEvent):
        async for r in self._emit(event, self.service.login(self._ctx(event))):
            yield r

    @filter.command("舞立方退出", alias={"dcquit"})
    async def dc_quit(self, event: AstrMessageEvent):
        async for r in self._emit(event, self.service.quit(self._ctx(event))):
            yield r

    @filter.command("mydc", alias={"个人信息"})
    async def mydc(self, event: AstrMessageEvent):
        async for r in self._emit(event, self.service.info_card(self._ctx(event))):
            yield r

    @filter.command("myrt", alias={"战力分析"})
    async def myrt(self, event: AstrMessageEvent):
        async for r in self._emit(event, self.service.ratio(self._ctx(event), "both")):
            yield r

    @filter.command("myrtb30", alias={"战力分析b30"})
    async def myrtb30(self, event: AstrMessageEvent):
        async for r in self._emit(event, self.service.ratio(self._ctx(event), "b30")):
            yield r

    @filter.command("myrtap30", alias={"战力分析ap30"})
    async def myrtap30(self, event: AstrMessageEvent):
        async for r in self._emit(event, self.service.ratio(self._ctx(event), "ap30")):
            yield r

    @filter.command("曲目", alias={"查歌", "song", "音乐"})
    async def dc_song(self, event: AstrMessageEvent, name: str = ""):
        async for r in self._emit(event, self.service.song(self._ctx(event), name)):
            yield r

    @filter.command("导出成绩", alias={"dcxlsx", "成绩导出"})
    async def dc_xlsx(self, event: AstrMessageEvent):
        async for r in self._emit(event, self.service.export(self._ctx(event))):
            yield r

    @filter.command("曲目推送", alias={"谱面推送", "订阅谱面"})
    async def dc_push(self, event: AstrMessageEvent, arg: str = ""):
        async for r in self._emit(
            event, self.service.push_toggle(self._ctx(event), arg)
        ):
            yield r

    @filter.command("曲库补全", alias={"补全时长", "拉取音频"})
    async def dc_backfill(self, event: AstrMessageEvent, arg: str = ""):
        async for r in self._emit(
            event, self.service.backfill(self._ctx(event), arg)
        ):
            yield r
