<div align="center">

<img src="logo.png" width="120" alt="舞小鲸" />

# 舞小鲸 · 舞立方查询

**查分、曲库、成绩导出，一条指令搞定。**

[![License](https://img.shields.io/badge/License-PolyForm%20Noncommercial-1f6feb)](https://polyformproject.org/licenses/noncommercial/1.0.0)
[![Version](https://img.shields.io/badge/version-1.0.3-2ea043)](https://github.com/chicluo/astrbot_plugin_dancecube)
[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.9.2-6f42c1)](https://github.com/AstrBotDevs/AstrBot)
[![Platform](https://img.shields.io/badge/QQ-aiocqhttp%20%7C%20qq__official-4f9cf9)]()
[![AstrBot Market](https://img.shields.io/badge/AstrBot-%E6%8F%92%E4%BB%B6%E5%B8%82%E5%9C%BA-6f42c1)](https://cloud.astrbot.app/plugin/chicluo/astrbot_plugin_dancecube)

</div>

> 「舞小鲸」＝舞立方 ＋ DeepSeek 的鲸：整个插件由 DeepSeek 编写，连这份说明也是——它只是换个名字游过来。
>
> 已入驻爱发电：<https://ifdian.net/a/chicluo> ｜ 体验群：**566567000**

## 功能

| 指令 | 作用 |
|---|---|
| `/舞立方登录` | 扫码绑定舞立方账号 |
| `/mydc` | 个人信息图（积分 / 全连率 / 排名 / 金币 / 天梯） |
| `/myrt` | B15 + R15 战力分析图 |
| `/myrtb30` / `/myrtap30` | B30 / AP30 战力分析图 |
| `/曲目 <曲名或ID>` | 曲库查询：音乐ID / 定数 / 物量 / 时长 / 比特率（简繁互搜） |
| `/曲目 官谱全表` | 导出官谱全表 xlsx（每个谱面一行） |
| `/曲目 自制谱全表` | 导出自制谱全表 xlsx（每个商品一行，含时长与码率） |
| `/导出成绩` | 导出全部成绩为 xlsx |
| `/曲库补全` | 批量探测缺时长/码率的自制谱（仅主控）；拉取期间单曲查询会提示稍后再试 |
| `/曲目推送 开` / `关` / `状态` | 谱面更新推送：每天 08:00 有新增/下架时通知（开关仅主控可用） |
| `/舞立方退出` | 清除登录凭证 |

## 快速开始

1. 安装 [AstrBot](https://github.com/AstrBotDevs/AstrBot)（建议 ≥ 4.9.2）；也可以直接从 AstrBot 插件市场搜索「舞小鲸」安装
2. 把本目录放进 `data/plugins/`
3. 安装依赖：`pip install -r requirements.txt`
4. 在面板里填好 `owner_qq`（`export_dir` 可留空），到群里发 `/舞立方登录`

> ⚠️ **初次部署请用不常用的小号**：插件会调用官方接口拉曲库与成绩，存在被风控甚至封禁的风险；测试期间没遇到，但不代表不会发生。

## 配置

| 配置项 | 说明 |
|---|---|
| `owner_qq` | 机器人主人 QQ |
| `allowed_groups` | 允许使用的群标识，填 `*` 表示全部。**aiocqhttp 填真实群号；qq_official 填 group_openid**（形如 `C0A...`） |
| `allow_private` | 允许好友私聊使用（仅 aiocqhttp 有效，官方个人开发者没有 C2C 私聊权限） |
| `export_dir` | 成绩导出目录，可留空：留空时用 `plugin_data/astrbot_plugin_dancecube/export/`；填自定义路径时必须在该数据目录内，否则忽略 |
| `catalog_ttl_minutes` | 曲库全表缓存分钟数，默认 30 |
| `qr_timeout_seconds` | 扫码登录超时秒数 |
| `insecure_tls` | 关闭 HTTPS 证书校验，默认 `false`。**默认不要开启**，只有官方证书链确实出问题的老环境才临时打开 |
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
- 难度标签与机型：`[初]` / `[中]` / `[高]` 经典三档，**二代机可玩**；`[S+]`（show+）和 `[秀]` 都**只能在秀机（舞立方秀）上玩**，二代机玩不了。
- 谱面更新推送的检测在凌晨 03:00–05:00 随定时刷新进行，**08:00 才发**，不会半夜扰人；状态存在 `plugin_data/astrbot_plugin_dancecube/push_targets.json`。

## 开发花销

只算插件功能本身（按对话轮次粗分，截至 2026-11-20 20:00）：

- 139 轮 / 1,285 次请求
- Token 合计约 **5.78 亿**（缓存命中 574,304,896 ／ 未命中 2,403,129 ／ 输出 820,634）
- 约 **¥18.58**（DeepSeek 峰谷价估算，不含部署运维与闲聊）
- 口径：按 `dsh-cost.js` 价目表估算，官方按缓存折扣实扣通常更低；每次发版前用 `node dsh-cost.js cost --session <id>` 重算

## 许可证

[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0) —— **禁止商业使用**。

- 允许：个人学习、研究、测试、业余项目，以及慈善、教育、公共研究、公共安全、环保、政府机构等非商业使用；可以修改和再分发，但必须保留许可证文本与署名。
- 不允许：任何以盈利为目的的使用，包括但不限于付费服务、商业产品内置、以商业运营的机器人部署。
- **不提供商业授权**：本项目只供免费使用，不接受商业授权申请；任何商业用途一律不被许可。

> 说明：2026-11-17 之前以 MIT 发布的版本仍按当时的 MIT 授权，从本次变更（commit「docs: 许可证改为 PolyForm Noncommercial」）之后的版本起适用上述许可证。

> 依赖许可：Pillow（MIT-CMU）、openpyxl（MIT）、tinytag（MIT），均为宽松许可，与本项目许可证不冲突，打包分发也不会引入传染性条款。（早期版本用的 mutagen 是 GPL-2.0-or-later，已替换。）

## 免责声明

- 本插件为**第三方非官方工具**，与舞立方游戏及其运营方无任何关联，未获得官方授权或认可。
- 本项目**仅允许非商业用途**，具体范围见上文「许可证」；通过本插件生成的图片、表格等输出内容同样不得用于商业用途。
- 数据来自游戏官方接口或公开 WIKI，接口可能随时变更，不保证可用性；插件内置的曲库快照、曲目信息等数据版权归原权利人所有，仅作本地缓存与学习研究使用，不代表授权。
- 使用本插件产生的一切后果由使用者自行承担。
