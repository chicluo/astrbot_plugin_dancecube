# -*- coding: utf-8 -*-
"""框架无关的业务内核。

这里只放跨模块共享的小工具，不 import 任何 bot 框架：core 下的模块要能脱离
AstrBot 单独跑（tests 里就是这么用的）。
"""


class NullLogger:
    """未注入 logger 时的占位实现：不依赖标准库 logging 模块。"""

    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass
