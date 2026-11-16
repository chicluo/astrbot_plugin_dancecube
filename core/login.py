"""舞立方官方接口 —— 二维码登录（同步实现，由线程池调用）。

流程（已实测）：
  1. GET  /Dance/api/Common/GetQrCode?id=   -> {ID, QrcodeUrl, ExpireTime}
  2. 玩家用微信扫码 QrcodeUrl
  3. POST /Dance/token (form)               -> {access_token, userId, ...}
        client_type=qrcode & grant_type=client_credentials & client_id=<ID>
     未扫码时返回 400 {"error":"unauthorized_client"}
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import net

BASE = "https://dancedemo.shenghuayule.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _http(url, data=None, timeout=25):
    headers = {"User-Agent": UA}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with net.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001
        return 0, ("%s: %s" % (type(e).__name__, e)).encode()


def request_qr():
    """申请一个登录二维码，返回 (png_bytes, meta)。"""
    status, raw = _http(BASE + "/Dance/api/Common/GetQrCode?id=")
    if status != 200:
        raise RuntimeError("获取二维码失败：HTTP %s" % status)
    data = json.loads(raw.decode("utf-8", "replace"))
    qid = data.get("ID", "")
    qurl = data.get("QrcodeUrl", "")
    if not qid or not qurl:
        raise RuntimeError("二维码接口返回缺少 ID/QrcodeUrl")

    try:
        img = net.download(qurl, max_bytes=5 << 20, timeout=25)
    except ValueError as e:  # 域名不在白名单 / 超限
        raise RuntimeError("二维码图片下载失败：%s" % e)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("二维码图片下载失败：%s: %s" % (type(e).__name__, e))
    if len(img) < 500:
        raise RuntimeError("二维码图片下载失败：内容过短（%d 字节）" % len(img))

    meta = {
        "id": qid,
        "qrcode_url": qurl,
        "expire": data.get("ExpireTime", ""),
        "created": int(time.time()),
    }
    return img, meta


def poll_token(qid, timeout=120, interval=2.0):
    """轮询换取 token。成功返回 token dict，超时返回 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st, raw = _http(
            BASE + "/Dance/token",
            {
                "client_type": "qrcode",
                "grant_type": "client_credentials",
                "client_id": qid,
            },
        )
        if st == 200:
            try:
                tok = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                time.sleep(interval)
                continue
            if tok.get("access_token"):
                tok["_obtained_at"] = int(time.time())
                tok["_client_id"] = qid
                return tok
        time.sleep(interval)
    return None
