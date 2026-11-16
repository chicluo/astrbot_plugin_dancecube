# -*- coding: utf-8 -*-
"""NoneBot2 适配示例：把 astrbot_plugin_dancecube 的 core 接到 NoneBot2。

这份文件不参与 AstrBot 插件加载，只是给你照抄的模板。

用法：
1. 把整个 `astrbot_plugin_dancecube` 目录放到 NoneBot2 项目能找到的位置
   （比如 `bot_project/vendor/astrbot_plugin_dancecube/`）；
2. 安装依赖：`pip install pillow openpyxl mutagen`；
3. 把本文件复制成 `nonebot_plugin_dancecube/__init__.py`，按注释改
   `REPO_PARENT` 和 `DATA_DIR`；
4. 启动 NoneBot2。

`core/` 里没有任何 AstrBot 依赖，所以除了这个适配文件，其余逻辑一行都不用改。
"""
import os
import sys
from pathlib import Path

# ---- 1. 让 Python 找到 core 包 -------------------------------------------
# 假设本文件在  <项目>/nonebot_plugin_dancecube/__init__.py，
# 仓库在        <项目>/vendor/astrbot_plugin_dancecube/
REPO_PARENT = Path(__file__).resolve().parents[1] / "vendor"
sys.path.insert(0, str(REPO_PARENT))

from astrbot_plugin_dancecube.core.service import (  # noqa: E402
    DanceCubeService,
    File,
    Image,
    RequestContext,
    Text,
    file_send_hint,
    safe_error,
)

# ---- 2. NoneBot2 依赖 -----------------------------------------------------
from nonebot import on_command  # noqa: E402
from nonebot.adapters import Message  # noqa: E402
from nonebot.adapters.onebot.v11 import (  # noqa: E402
    Bot,
    GroupMessageEvent,
    MessageEvent,
    MessageSegment,
)
from nonebot.params import CommandArg  # noqa: E402

# ---- 3. 配置（换成你自己的 nonebot config 也行）--------------------------
DATA_DIR = Path(os.environ.get("DANCECUBE_DATA_DIR", "data/dancecube"))
OWNER = os.environ.get("DANCECUBE_OWNER", "")          # 主人 QQ
GROUPS = [
    g for g in os.environ.get("DANCECUBE_GROUPS", "").split(",") if g.strip()
] or ["*"]
EXPORT_DIR = os.environ.get("DANCECUBE_EXPORT_DIR", str(DATA_DIR / "grades"))

CONFIG = {
    "owner_qq": OWNER,
    "allowed_groups": GROUPS,
    "allow_private": True,
    "export_dir": EXPORT_DIR,
    "catalog_ttl_minutes": 30,
    "qr_timeout_seconds": 120,
}

_service: DanceCubeService | None = None


def _ctx(event: MessageEvent, text: str) -> RequestContext:
    """NoneBot2 事件 → 框架无关的 RequestContext。"""
    is_group = isinstance(event, GroupMessageEvent)
    return RequestContext(
        user_id=str(event.get_user_id()),
        group_id=str(event.group_id) if is_group else "",
        is_group=is_group,
        platform="onebot",
        text=text,
        origin=str(event.get_session_id()),
    )


def get_service(bot: Bot) -> DanceCubeService:
    """懒加载单例；notifier 用 bot 主动发送登录结果。"""
    global _service
    if _service is None:
        async def notifier(ctx: RequestContext, replies):
            for r in replies:
                if isinstance(r, Text):
                    await bot.send_private_msg(
                        user_id=int(ctx.user_id), message=r.text
                    )
                elif isinstance(r, Image):
                    await bot.send_private_msg(
                        user_id=int(ctx.user_id),
                        message=MessageSegment.image(Path(r.path)),
                    )

        _service = DanceCubeService(
            config=CONFIG,
            data_dir=DATA_DIR,
            bundled_data_dir=REPO_PARENT / "astrbot_plugin_dancecube" / "data",
            notifier=notifier,
        )
    return _service


async def _emit(bot: Bot, event: MessageEvent, gen):
    """把 Text / Image / File 发出去。"""
    async for r in gen:
        if isinstance(r, Text):
            await bot.send(event, r.text)
        elif isinstance(r, Image):
            await bot.send(event, MessageSegment.image(Path(r.path)))
        elif isinstance(r, File):
            if isinstance(event, GroupMessageEvent):
                try:
                    await bot.call_api(
                        "upload_group_file",
                        group_id=event.group_id,
                        file=r.path,
                        name=r.name,
                    )
                except Exception as e:  # noqa: BLE001
                    await bot.send(
                        event, "文件发送失败：%s（%s）" % (file_send_hint(e), safe_error(e))
                    )
            else:
                await bot.send(event, "私聊发不了文件，请让管理员从本机取：%s" % r.name)


# ---- 4. 命令注册 ----------------------------------------------------------
login = on_command("舞立方登录", aliases={"dclogin"}, priority=5)
quit_ = on_command("舞立方退出", aliases={"dcquit"}, priority=5)
mydc = on_command("mydc", aliases={"个人信息"}, priority=5)
myrt = on_command("myrt", aliases={"战力分析"}, priority=5)
myrtb30 = on_command("myrtb30", aliases={"战力分析b30"}, priority=5)
myrtap30 = on_command("myrtap30", aliases={"战力分析ap30"}, priority=5)
song = on_command("曲目", aliases={"查歌", "song", "音乐"}, priority=5)
export = on_command("导出成绩", aliases={"dcxlsx", "成绩导出"}, priority=5)


@login.handle()
async def _(bot: Bot, event: MessageEvent):
    await _emit(bot, event, get_service(bot).login(_ctx(event, event.get_plaintext())))


@quit_.handle()
async def _(bot: Bot, event: MessageEvent):
    await _emit(bot, event, get_service(bot).quit(_ctx(event, event.get_plaintext())))


@mydc.handle()
async def _(bot: Bot, event: MessageEvent):
    await _emit(bot, event, get_service(bot).info_card(_ctx(event, event.get_plaintext())))


@myrt.handle()
async def _(bot: Bot, event: MessageEvent):
    await _emit(bot, event, get_service(bot).ratio(_ctx(event, event.get_plaintext()), "both"))


@myrtb30.handle()
async def _(bot: Bot, event: MessageEvent):
    await _emit(bot, event, get_service(bot).ratio(_ctx(event, event.get_plaintext()), "b30"))


@myrtap30.handle()
async def _(bot: Bot, event: MessageEvent):
    await _emit(bot, event, get_service(bot).ratio(_ctx(event, event.get_plaintext()), "ap30"))


@song.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    text = event.get_plaintext()
    await _emit(bot, event, get_service(bot).song(_ctx(event, text), str(arg).strip()))


@export.handle()
async def _(bot: Bot, event: MessageEvent):
    await _emit(bot, event, get_service(bot).export(_ctx(event, event.get_plaintext())))
