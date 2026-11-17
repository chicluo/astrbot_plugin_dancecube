(本插件全程由Deepseek-V4.1-Flash编译，就连这个说明也是)

# 舞立方查询

查询舞立方玩家信息、B15/R15 战力分析图、导出全部成绩、扫码登录。

## 功能

- `/舞立方登录 —— 扫码绑定舞立方账号`
- `/mydc —— 个人信息图（积分 / 全连率 / 排名 / 金币 / 天梯）`
- `/myrt —— B15 + R15 战力分析图`
- `/myrtb30 / /myrtap30 —— B30 / AP30 战力分析图`
- `/曲目 <曲名或ID> —— 曲库查询：音乐ID / 定数 / 物量 / 时长 / 比特率`
- `/曲目 官谱全表 —— 导出官谱全表 xlsx（每个谱面一行）`
- `/曲目 自制谱全表 —— 导出自制谱全表 xlsx（每个商品一行）`
- `/导出成绩 —— 导出全部成绩为 xlsx`
- `/舞立方退出 —— 清除登录凭证`

## 安装

1. 安装 [AstrBot](https://github.com/AstrBotDevs/AstrBot)
2. 把本目录放到 `data/plugins/` 下
3. 安装依赖：`pip install -r requirements.txt`（如果有）
4. 在 AstrBot 面板里填写配置

## 配置

| 配置项 | 说明 |
|---|---|
| `owner_qq` | 机器人主人 QQ |
| `allowed_groups` | 允许使用的群标识，填 `*` 表示全部。**aiocqhttp 填真实群号；qq_official 填 group_openid**（形如 `C0A...`） |
| `allow_private` | 允许好友私聊使用（仅 aiocqhttp 有效，官方个人开发者没有 C2C 私聊权限） |
| `export_dir` | 成绩导出的本机目录（必须填） |
| `catalog_ttl_minutes` | 曲库全表缓存分钟数，默认 30 |
| `qr_timeout_seconds` | 扫码登录超时秒数 |
| `insecure_tls` | 关闭 HTTPS 证书校验，默认 `false`（不推荐） |
| `login_admin_only` | 只允许主人发起登录，默认 `false` |

## 换到别的 bot 框架

业务逻辑全在 `core/service.py`，**不依赖 AstrBot**（`core/` 里 `import astrbot` 为 0）。
接入其他框架只需要写一层翻译：

1. 构造 `DanceCubeService(config, data_dir, bundled_data_dir, notifier, logger)`；
2. 把消息整理成 `RequestContext(user_id, group_id, is_group, platform, text, origin)`；
3. `async for reply in service.song(ctx): ...`，把 `Text` / `Image` / `File` 自己发出去；
4. 登录完成后的通知走 `notifier(ctx, replies)`（没有就只记日志）。

`examples/nonebot2_adapter.py` 是 NoneBot2（OneBot v11）的完整示例。
Koishi / Yunzai 这类非 Python 框架，建议把 core 包一层 FastAPI 当 HTTP 服务调，而不是重写。

两个要注意的差异：

- 群标识：`qq_official` 是 `group_openid`，OneBot 是数字群号，`allowed_groups` 要按平台填；
- 发文件：OneBot 有 `upload_group_file`（真群文件），官方 API 只有聊天富媒体。

## 注意事项

- **Linux 需要中文字体**。插件按 `msyh.ttc` → `wqy-zenhei.ttc` → `NotoSansCJK` → `PingFang` 顺序找字体，一个都没有时图中中文会变成方块（日志会警告）。Debian/Ubuntu：`apt install fonts-wqy-zenhei`。
- 扫码登录需要用户在机器上操作，二维码会直接发到聊天里。**非本人请勿扫码**：扫码即授权该条指令的发起者使用你的账号，登录成功消息会回显绑定的角色，异常容易被发现；每次绑定都会写一条审计日志。
- 登录凭证以**明文 JSON** 存在 `plugin_data/astrbot_plugin_dancecube/tokens/` 下，插件会把目录收紧到 `0700`、文件收紧到 `0600`；部署在共享机器上时建议再确认一次权限。
- 同一用户 30 秒内只处理一次请求（登录单独 10 秒冷却、单独计时，扫码成功后立刻查询不会被拦）。
- 成绩导出目录必须存在，否则导出失败。
- **HTTPS 默认校验证书**。只有确实撞上官方证书链问题的老环境，才建议打开 `insecure_tls`——打开后 token 可能被同一网络里的中间人截获，启动日志会警告。
- 曲风统一取 `MusicCategoryNew`：`1 国语 / 3 韩文 / 4 欧美 / 6 其他`，导出成绩与官谱全表口径一致；自制谱不在曲库里，曲风显示 `-`。
- `/曲目` 是强制指令，不会被自然语言触发。
- 曲库全表生成后会缓存在 `plugin_data/astrbot_plugin_dancecube/tmp/catalog/`，`catalog_ttl_minutes` 内再有人要就直接重发上次的文件，不重新拉库。
- **群文件容量查不了**：QQ 官方机器人 API 只有群信息（`/v2/groups/{group_openid}/info`）和群聊富媒体上传（`/v2/groups/{group_openid}/files`），没有群文件列表或容量接口；机器人发的文件走聊天富媒体、不落群文件空间，所以群文件满不会导致发送失败。发送失败时插件会把常见原因（超 200MB、限流、凭证过期）翻成人话提示。
- 插件自带一份曲库快照（`data/*.json`，约 3.7 MB），首次部署时自动复制到 `plugin_data`，不用等全量拉取；数据过期后会在 24 小时内自动刷新。
- **曲库快照也可以不用**：删掉 `data/` 目录后，插件会按原流程从官方接口拉取（官谱约 1 秒、自制谱约 1 分钟），前提是已经有人 `/舞立方登录` 过——拉取要借用一个已登录用户的 token。
- 难度标签：`[初]` / `[中]` / `[高]` 经典三档，`[秀]` 舞立方秀专属，`[S+]` show+ 谱。

---

> ## ⚠️ 重要：请务必用一个不常用的号登录
>
> 插件会调用官方接口拉取全曲库、成绩等数据，**存在账号被风控甚至封禁的风险**。
> 测试期间没有遇到，但不代表不会发生。
>
> **初次部署请用不常用的小号，不要用主力账号。**


## 许可证

[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0) —— **禁止商业使用**。

- 允许：个人学习、研究、测试、业余项目，以及慈善、教育、公共研究、公共安全、环保、政府机构等非商业使用；可以修改和再分发，但必须保留许可证文本与署名。
- 不允许：任何以盈利为目的的使用，包括但不限于付费服务、商业产品内置、以商业运营的机器人部署。
- **不提供商业授权**：本项目只供免费使用，不接受商业授权申请；任何商业用途一律不被许可。

> 说明：2026-11-17 之前以 MIT 发布的版本仍按当时的 MIT 授权，从本次变更（commit「docs: 许可证改为 PolyForm Noncommercial」）之后的版本起适用上述许可证。

## 免责声明

- 本插件仅供学习交流，不得用于商业用途。
- 数据来自游戏官方接口或公开 WIKI，接口可能随时变更，不保证可用性。
- 使用本插件产生的一切后果由使用者自行承担。
