# -*- coding: utf-8 -*-
"""统一 HTTPS 连接与受限下载。

默认开启证书校验；个别部署环境若真的撞上证书链问题，可把配置项
`insecure_tls` 打开，退回旧行为（关闭校验），日志会提示风险。
"""
import ssl
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 只允许从这些域下载（官方接口、微信二维码、OSS 头像/音频）
_ALLOWED_HOSTS = (
    "shenghuayule.com",
    "mp.weixin.qq.com",
    "weixin.qq.com",
    "aliyuncs.com",
    "myqcloud.com",
    "qpic.cn",
)

_VERIFY = True


def set_verify_tls(verify):
    global _VERIFY
    _VERIFY = bool(verify)


def verify_tls():
    return _VERIFY


def ctx():
    """返回 SSLContext；关闭校验时等价于插件旧行为。"""
    c = ssl.create_default_context()
    if not _VERIFY:
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
    return c


def urlopen(req, timeout=30, context=None):
    return urllib.request.urlopen(req, timeout=timeout, context=context or ctx())


def host_allowed(url):
    """只放行 https + 白名单域名，挡住 file://、内网地址与任意跳转。"""
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if p.scheme != "https" or not p.hostname:
        return False
    host = p.hostname.lower()
    return any(host == h or host.endswith("." + h) for h in _ALLOWED_HOSTS)


def _encode(url):
    """路径里的空格等字符做百分号编码，但保留 +（OSS 的 key 里 + 是字面量）。"""
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (
            p.scheme,
            p.netloc,
            urllib.parse.quote(p.path, safe="/%+"),
            p.query,
            p.fragment,
        )
    )


def download(url, max_bytes=8 << 20, timeout=30, headers=None):
    """下载到内存；超限或非白名单抛 ValueError。"""
    if not host_allowed(url):
        raise ValueError("不支持的下载地址：%s" % url[:80])
    req = urllib.request.Request(_encode(url), headers=headers or {"User-Agent": UA})
    with urlopen(req, timeout=timeout) as r:
        data = r.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("文件超过上限 %d MB" % (max_bytes // 1024 // 1024))
    return data


def download_file(url, dest, max_bytes=64 << 20, timeout=60, headers=None):
    """流式下载到本地文件，返回字节数；超限或非白名单抛 ValueError。"""
    if not host_allowed(url):
        raise ValueError("不支持的下载地址：%s" % url[:80])
    req = urllib.request.Request(_encode(url), headers=headers or {"User-Agent": UA})
    total = 0
    with urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("文件超过上限 %d MB" % (max_bytes // 1024 // 1024))
            f.write(chunk)
    return total
