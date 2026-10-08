# Syncaind Discord Bot

用 `discord.py` 写的模块化 Discord 机器人：**服务器管理**（处罚、警告累计、案件记录、消息清理、频道与角色）、**服务器信息与权限诊断**、**GitHub 账号映射与 release 推送**，以及 **SMAS 激活码发放与权限节点同步**。

* 模块化：功能以 `bot/modules/<name>/` 下的自包含包形式挂载，可运行时启停与热重载
* 命令：纯斜杠命令；界面语言按客户端语言在中文/英文之间自动切换
* 数据：SQLite（`data/bot.db`），单文件、备份即拷贝

## 快速开始（本地开发）

```powershell
# 1. Python 3.10+
python --version

# 2. 建虚拟环境并装依赖
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt

# 3. 配置
copy .env.example .env
#    然后填写 .env 里的 DISCORD_TOKEN / GUILD_ID / OWNER_ID

# 4. 跑起来
python -m bot
```

没有 `.env` 或必填项为空时，程序会打印出具体缺哪几项并以退出码 `2` 结束，不会抛一堆堆栈。

启动时取 `data/bot.lock` 的单实例锁；连接 Discord 失败会**指数退避重试**（1s → 2s → … 最长 5 分钟），只有 token 无效或缺特权 intent 才直接退出 —— 网络抖动不会让机器人静静地死掉。

Linux / macOS 上把 `.venv\Scripts\` 换成 `.venv/bin/` 即可。

## Discord 侧准备

1. 在 [Developer Portal](https://discord.com/developers/applications) 新建 Application → **Bot** → Reset Token，把 token 填进 `DISCORD_TOKEN`。
2. 同一个页面的 **Privileged Gateway Intents** 里开启 **SERVER MEMBERS INTENT**。
   机器人需要成员数据做角色层级比对与成员信息；不开的话启动会直接报错并退出（退出码 `4`）。
3. **OAuth2 → URL Generator** 勾选 `bot` 与 `applications.commands`，权限至少给：
   `Kick Members`、`Ban Members`、`Moderate Members`、`Manage Messages`、`Embed Links`、`View Channels`。
   生成的链接把机器人邀请进你的服务器。
4. 拿三个 ID（Discord 客户端需开启「开发者模式」，右键即可复制）：
   * 服务器 ID → `GUILD_ID`
   * 你自己的用户 ID → `OWNER_ID`
   * 想把处罚日志发到哪个频道 → `MOD_LOG_CHANNEL_ID`（可选）

> 机器人默认只出站连接 Discord；只有启用 GitHub web flow 时才会在本地监听一个端口（见「怎么让回调地址可达」）。`/purge` 按数量、成员与时间范围过滤，不需要 `MESSAGE_CONTENT` intent。

## 配置项

| 变量                           | 必填 | 默认            | 说明                                                                                                    |
|------------------------------|----|---------------|-------------------------------------------------------------------------------------------------------|
| `DISCORD_TOKEN`              | ✅  | —             | 机器人 token                                                                                             |
| `GUILD_ID`                   | ✅  | —             | 目标服务器。缺了会直接启动失败，避免命令被同步到全局                                                                            |
| `OWNER_ID`                   | ✅  | —             | 你的用户 ID，用于 `/module` 等框架级命令                                                                           |
| `MOD_LOG_CHANNEL_ID`         |    | 空             | 处罚日志频道；也可以在数据库里按服务器改                                                                                  |
| `DATABASE_PATH`              |    | `data/bot.db` | SQLite 文件位置                                                                                           |
| `LOG_DIR`                    |    | `data/logs`   | 日志目录，按天轮转，保留 30 天                                                                                     |
| `LOG_LEVEL`                  |    | `INFO`        | `DEBUG` / `INFO` / `WARNING` / `ERROR`                                                                |
| `DEFAULT_LOCALE`             |    | `en-US`       | 非中文客户端的回退语言；**私信通知（反应角色、处罚）也用它的语言**（私信里拿不到对方客户端语言），中文服务器建议填 `zh-CN`                                   |
| `GITHUB_OAUTH_CLIENT_ID`     |    | 空             | OAuth App 的 `client_id`。留空不影响启动，只是 `/link github` 会提示未配置                                              |
| `GITHUB_OAUTH_CLIENT_SECRET` |    | 空             | **只有 web flow 需要**；务必保密（等价于应用身份）                                                                      |
| `GITHUB_OAUTH_REDIRECT_URI`  |    | 空             | web flow 的公网回调地址；**必须与 OAuth App 里填的完全一致**                                                            |
| `GITHUB_TOKEN`               |    | 空             | 读 release 用的令牌。建 classic PAT 时**一个 scope 都别勾**：公开仓库只读就够，泄露也改不了仓库，额度还从 60 次/小时提到 5000。留空也能用，仓库多了会撞匿名限流 |
| `WATCHER_BASE_URL`           |    | 空             | 你的 gh-webhook-watcher 地址。配了才会常驻 SSE 订阅                                                                |
| `WATCHER_API_TOKEN`          |    | 空             | 监听服务对 `/api/*` 的令牌（`GHW_API_TOKEN`）。没带令牌的请求会被拒                                                        |
| `WEB_HOST`                   |    | `127.0.0.1`   | 入站端点绑哪里。默认只绑本地，对外暴露交给隧道/反向代理                                                                          |
| `WEB_PORT`                   |    | `0`           | 入站端点端口。**0 = 不启动**；web flow 需要它 > 0                                                                   |
| `ACCESS_BASE_URL`            |    | 空             | SMAS 地址。与下面两项一起填才会启用 `/key` 与 `/access`；服务号在服务器侧要**被授权**（见 `.env.example` 的注释）                        |
| `ACCESS_SERVICE_ID`          |    | `discord-bot` | 服务号 id，与服务器侧 `SMAS_SERVICE_ACCOUNTS` 的键一致                                                             |
| `ACCESS_SERVICE_SECRET`      |    | 空             | 服务号密钥；务必保密                                                                                            |

## 命令

机器人里 `/help` 是权威清单（它直接读命令树生成，不会和实际部署脱节）。这里按模块列一份人读的：

| 模块               | 命令                                                                                                                          | 需要的 Discord 权限                                     |
|------------------|-----------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------|
| 框架               | `/help`、`/help <命令>`                                                                                                        | 无                                                  |
| 框架               | `/module list`、`/module enable\|disable\|reload <module>`                                                                   | 机器人 owner                                          |
| `moderation`     | `/kick`、`/ban`、`/unban`、`/timeout`、`/warn`、`/purge`                                                                         | Kick / Ban / Moderate Members、Manage Messages      |
| `cases`          | `/history [member] [limit]`、`/case view <编号>`、`/case revoke <编号> [理由]`                                                      | Moderate Members；**撤销按动作鉴权**：撤销 ban 另需 Ban Members |
| `channels`       | `/channel create`、`delete`、`rename`、`lock`、`unlock`、`slowmode`、`overwrite`、`overwrite-category`                             | Manage Channels                                    |
| `roles`          | `/role create`、`delete`、`rename`、`color`、`permissions`、`grant`、`revoke`、`info`                                              | Manage Roles                                       |
| `reaction_roles` | `/reactionrole add`、`remove`、`list`、`clear`                                                                                 | Manage Roles                                       |
| `github_bridge`  | `/link github [method]`、`/link show [member]`、`/link remove [member]`                                                       | 无（看/改**别人**的绑定需要 Manage Roles）                     |
| `github_feed`    | `/feed add <仓库> [目标]`、`/feed list`、`/feed remove <仓库>`、`/feed sync <仓库>`                                                    | Manage Server                                      |
| `access_keys`    | `/key drop <批次> <数量> <模式> [角色] [排除角色] [分钟] [team]`、`/key close <编号>`、`/key list <批次>`、`/key deny-role <角色>`、`/key allow-role <角色>` | Manage Server（点按钮参与不需要）                     |
| `access_roles`   | `/access bind <节点> <角色>`、`/access unbind <节点> [角色]`、`/access list`、`/access sync [dry_run]`、`/access status <成员>`           | Manage Roles                                       |
| `tools`          | `/serverinfo`、`/userinfo [member]`、`/permissions [member]`                                                                  | 无                                                  |

命令名一律是英文且**不做本地化**（`/kick` 不会变成 `/踢出`）；描述与参数说明会按客户端语言切换。理由：命令名是肌肉记忆和外部脚本的锚点，而且本地化名称一旦不合 Discord 的命名规则会让整次同步失败。

几条值得知道的行为：

* **处罚动作**都会：落库（获得 case 编号）、尽力 DM 当事人、发一条 Embed 到 mod-log 频道。
* **警告升级**：有效警告数达到 `warn_threshold`（默认 3）的整数倍时触发升级，动作为 `warn_action`（默认 `timeout`，可设 `kick` / `ban`），禁言时长取 `warn_timeout_minutes`（默认 60）。阈值设为 `0` 表示关闭自动升级。
* **撤销会真的回滚**：ban → 解封，timeout → 解除禁言，warn → 该条警告不再计入累计。kick / purge 无法回滚，只做记录。Discord 侧回滚失败时**不会**标记成已撤销 —— 记录说撤销了、实际还封着，这种状态不能出现。
* **锁定/解锁**：锁定是给 @everyone 加 `send_messages=False`；解锁是把这一项**恢复成继承**（而不是强制允许），所以上层规则继续生效。
* **权限名**用 Discord 的权限位名（`send_messages`、`view_channel`、`manage_messages`…），多个用逗号或空格分隔。敲错名字会被明确拒绝，不会静默忽略。`/role permissions` 与频道覆盖**只动你点名的那几项**，没提到的权限保持原样。
* **反应角色**绑定在消息上：成员点表情给角色、取消表情收角色。映射落库，重启后照样有效；角色被删掉时，那条规则会在下次有人点它时自动清理。
  由于点表情本身没有任何界面回应，机器人会给当事人**发一条私信**说明拿到/失去了哪个角色（给不上时也会说明原因）。私信的语言取服务器配置的语言，没配就用 `DEFAULT_LOCALE` —— **反应事件里拿不到对方的客户端语言**（那是交互才有的字段），所以想要中文提示就把 `.env` 里的 `DEFAULT_LOCALE` 改成 `zh-CN`。对方关了私信就静默跳过（记在日志里）。

## 部署

### A. systemd

```bash
sudo mkdir -p /opt/syncaind-discord-bot
# 把代码放进去（git clone 或 rsync），然后：
cd /opt/syncaind-discord-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env          # 填三个必填项

sudo useradd --system --home /opt/syncaind-discord-bot syncaind
sudo chown -R syncaind:syncaind /opt/syncaind-discord-bot
sudo cp deploy/systemd/syncaind-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now syncaind-bot

journalctl -u syncaind-bot -f              # 看日志
```

### B. Docker

```bash
cp .env.example .env && nano .env
docker compose -f deploy/docker/compose.yaml up -d --build
docker compose -f deploy/docker/compose.yaml logs -f
```

数据库与日志通过 `data/` 挂载卷持久化；容器以非 root 用户运行；默认不映射任何端口。

> **要在容器里用 web flow**，除了设 `WEB_PORT=8080`、放开下面的 `ports` 注释，还必须**把 `WEB_HOST` 改成 `0.0.0.0`** —— 容器里的 `127.0.0.1` 只有容器自己看得见，映射出去的端口连不上它。这是容器化时最容易踩的一个坑。

> **同一个 token 上只能有一个进程。** 两个进程抢 gateway 会话会让交互时好时坏，而且两边都不留日志。启动时会用 `data/bot.lock` 取一把 OS 级排他锁（进程怎么死都会自动释放），第二个启动的会以退出码 `5` 直接报出来。

## 加一个新模块

一个模块 = `bot/modules/<name>/` 下一个包。最小形态：

```python
# bot/modules/greeting/__init__.py
from __future__ import annotations

from typing import Any

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(  # id 必须与目录名一致
    id="greeting",
    default_enabled=True,
    required_permissions=("manage_messages",),
)


async def setup(bot: Any) -> None:
    from .cog import GreetingCog

    await bot.add_cog(GreetingCog(bot))
```

```python
# bot/modules/greeting/cog.py
from __future__ import annotations

from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.translator import localized


@app_commands.guild_only()
class GreetingCog(commands.Cog):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @app_commands.command(
        name="hello",  # 命令名保持英文，不做本地化
        description=localized("Say hello", "commands.hello.description"),
    )
    async def hello(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            self.bot.t(interaction, "greeting.hello", user=interaction.user.display_name)
        )
```

```jsonc
// bot/modules/greeting/locales/zh-CN.json —— 模块自己的文案，放在模块目录里
{
  "commands.hello.description": "打个招呼",
  "greeting.hello": "你好，{user}！",
  "modules.greeting.name": "打招呼",
  "modules.greeting.description": "一个最小的示范模块。"
}
```

要点：

1. **文案归模块自己**：放在 `bot/modules/<name>/locales/{zh-CN,en-US}.json`，启动时自动合并进全局目录
   （核心目录只放框架与跨模块共用的词汇）。**加模块不用改任何全局文件**，删掉模块就带走了它的文案。
   `tests/test_locales.py` 会检查两份目录键集一致、代码里用到的键都存在、跨文件没有重复键。
2. **命令名英文、描述本地化**：`name="hello"` 保持英文；描述与参数说明用
   `localized("英文原文", "commands.<命令>.description")` 包一层，中文写在文案文件里。
3. **声明所需权限**：带权限检查的命令要写 `extras={"permissions": ("manage_messages",)}`，
   `/help` 靠它显示「需要什么权限」；测试会核对声明与真正生效的检查是否一致。
   只有 owner 能用的命令写 `extras={"owner_only": True}`。
4. **错误**：面向用户的失败直接 `raise UserError("errors.some_key", **placeholders)`，框架会翻成 ephemeral 提示；
   未预期异常会被记录完整堆栈并回一句通用提示。
5. **危险操作**：用 `bot.core.ui.ask_confirmation` 走确认流程（它已经处理好「仅发起者本人可点」与超时）。
   **不要直接用 `discord.ui.View.wait()`** —— `stop()` 早于 `wait()` 时会永久挂起，`ConfirmView` 用自持的 `asyncio.Event` 绕开了这个坑。
6. **不要引用其它模块的内部实现**：跨模块共享的东西放 `bot/core/`，或经 `bot` 实例暴露。
7. **随时可停可换**：`/module disable <name>` 会摘掉这个模块注册的全部 Cog；`/module reload <name>` 会清掉它的
   `sys.modules` 条目与 `__pycache__` 后重新导入，并重读它的文案，改完代码不用重启进程。
8. **别让假对象骗过测试**：替身只允许出现真实 discord.py 类上存在的属性（`tests/test_fakes.py` 会强制）。
   假对象一旦「发明」了真实类没有的方法，调用点抛出的属性错误就会被测试掩护过去。

## 开发

```powershell
.venv\Scripts\python.exe -m pytest        # 离线测试
.venv\Scripts\python.exe -m ruff check .  # lint
.venv\Scripts\python.exe -m ruff format . # format
```

测试**完全离线**：用伪造的 discord 对象和真实 SQLite 临时库，不连 Discord、不需要 token。覆盖：

* 模块加载、失败隔离（导入错误 / 元数据非法 / `setup` 抛错）、依赖缺失降级、运行时启停、状态落库
* 热重载真的换掉了代码（改文件后重新加载，跑的是新版本）
* i18n 的 locale 解析、缺键回退、占位符容错；多来源合并的先后顺序与冲突上报；单个模块文案文件损坏时不影响核心
* 两份文案目录的键集、占位符、空值完全对齐；代码里用到的键都存在；跨文件没有重复键
* 命令元数据的翻译链路（`get_translated_payload` 产出的就是会发给 Discord 的那份 payload），
  并且**断言命令名没有被本地化**
* 权限判定：owner 白名单、角色层级比对的全部拒绝分支、角色可改性
* 确认按钮：非发起者被拒、超时视为取消、「先点击再等待」不会挂起
* 数据层：迁移幂等（新装一次建全）、事务回滚、设置与模块开关读写、case 编号自增、警告累计与升级判定
* 真装配：用真正的 `SyncaindBot` 跑完整的 `setup_hook`（连库、迁移、注册错误处理与翻译器、挂框架命令、加载模块，
  只把联网的 `sync_commands` 换成桩），检查命令树、`/module` 子命令、停用后命令消失
* moderation 命令处理器的完整链路：层级校验 → 确认（含拒绝）→ 调用 Discord API → case 落库 → mod-log → DM → 回复；
  警告累计到阈值触发自动升级（timeout 与 kick 都测），阈值设 `0` 时关闭
* tools 命令处理器的真实调用（`/serverinfo` `/userinfo` `/permissions`，含「没有可处罚对象」的提示）
* `/help`：按模块分组、owner 命令标注、详情页参数与权限、中文/英文两套文案、停用模块后命令消失
* `/help` 展示的权限要求与真正生效的检查**逐条对齐**（声明少一项就该失败，多一项就该通过）
* `cases`：处罚史的顺序与归属、撤销标记、**撤销警告后不再计入累计**、解封与解除禁言真的调到了 Discord、
  撤销按动作鉴权（有 moderate_members 但没 ban_members 不能解封）、回滚失败时不标记撤销
* `channels`：权限名解析（含中英标点、去重、错别字报错）、私有频道带上 @everyone 的隐藏覆盖、
  删除需确认、锁定置 False / 解锁置 None、慢速模式归零、按分类批量应用
* `roles`：颜色与成员列表解析、**权限位只改点名的那几项**、高于机器人的角色被拒、集成管理的角色被拒、
  批量授予计入「已变更 / 无需变更 / 找不到」、移除角色
* `reaction_roles`：映射落库与「同表情覆盖」、`fetch_message` 校验、事件真的给/收角色、
  机器人自己的反应与其他服务器的反应被忽略、角色被删时自动清理规则、角色高于机器人时不发放
* 假对象不许发明真实 discord.py 类上不存在的属性
* 单实例守卫：同进程第二次获取被拒、释放后可重来、锁被占用时仍读得到持有者 pid（Windows 上字节区间锁必须显式加锁）
* GitHub 客户端：设备流的开始/等待/放慢/致命错误/成功五条路径，全部对着假 transport 跑；断言设备流请求里没有 `client_secret`
* GitHub 映射表：绑定、覆盖、一个 GitHub 账号只能属于一个人、按登录名大小写不敏感反查、按服务器隔离
* `/link` 三条命令的完整流程：未配置时的提示、拿到码之后就地更新、后台轮询真的绑上了、冲突与致命错误各自的说法、看/改别人的绑定需要 Manage Roles
* release 推送：绑定即回填且**按时间正序**、游标只前进（SQL 层钉住）、草稿被丢弃、缺字段不炸、分页到满页才翻页、404/403 各自的说法
* release 推送的**外部文本处理**：每条消息都带 `allowed_mentions=none`（release notes 里写 `@everyone` 也不会生效）、超长正文截断、空正文有说法、预发布有标记
* **机器标记清理**：`<!-- sp-compat {…} -->` 这类 HTML 注释被清掉（含多行、多条、行内、CRLF）；**没闭合的注释留着不动**；删掉独占一行的标记时**不合并上下两行**（免得破掉列表）；整条只有标记时走「没有填写说明」
* `/feed sync` 只推游标之后的、没新的时说清楚；权限不足时指名是哪个频道
* 监听服务消费面：**SSE 帧解析**（`retry:` / `id:` / 多行 `data:` / 心跳注释 / CRLF / 尾帧无空行 / **aiohttp 给的 bytes 行**）、订阅头带 `Last-Event-ID` 续传、服务端 `retry` 被采用并夹上限、非 JSON 帧只跳过不致命、**订阅按请求关掉总时限**（aiohttp 默认的 5 分钟总时限会把长连接掐断，而同一个 session 还在跑普通请求）
* 日志层：访问日志里的 query string 被抹掉（OAuth 放在 query 里的一次性凭据不落盘），重复安装过滤器不会叠加
* 监听服务语义：进流前的**区间检查**（被挤掉就转全量对账）、事件里没有 payload 时按链接里的 tag 回 GitHub 取正文、release 游标去重（重放不重推）、单条推失败不卡住整条订阅
* access server 客户端：服务号换会话、错误码映射（**密钥不进错误文本**）、信封 RPC 的 `request_id` 关联、**跳过服务端推送**、`invalid_session` 自愈重试、超时、逐请求 Team 头
* 有效节点拼接：`permissions`（系统作用域）∪ `assignments`（跨 Team），以及**读失败必须抛异常**（绝不退化成「他没有权限」）
* 发码链路：**私信失败 → `release` 退码 + 撤登记 + 还名额**、并发抢名额不超发（一条 SQL 自增封顶）、**没绑 GitHub 一律拒绝**（命令与按钮两条路都拦）、一人一批一枚、开奖公告里不出现码、没人报名时一枚都不取、到点的活动会被后台开奖、**服务器黑名单与单次活动的排除角色都压过白名单**
* 角色同步：**只动绑定表里出现过的角色**（别人手动发的角色永不被撤）、**读不到权限的人跳过而不是撤角色**（一次网络抖动不能把所有人角色撤光）、`dry_run` 只报告不动手、层级不够跳过并计数、不在服务器里的绑定对象跳过、来源要按 GitHub 数字 id 查（那张映射表就是桥梁）
* 配置层：必填缺失、取值非法各自给出可读提示（不吐 pydantic 堆栈）；`.env.example` 与字段登记表必须覆盖每一个设置项
* 加载器：能解析的依赖**不告警**（那一轮只是还没轮到），真的缺依赖才告警；`load_all` 顺序与字母序无关
* 入站端点：路由按 `(method, path)` 分发、只服务已注册的路由、**绑定失败只是端点起不来**（不会把整个机器人拖下水）
* **检查覆盖的完整性**：模块目录集合必须等于「Cog 登记表」派生的集合，且每个模块的文案键都要被遍历到 —— 手写登记表漏一个模块，一串一致性检查就会静默跳过它

不覆盖（需要真实 token，由人工在测试服务器上验收）：真实登录、命令同步、Discord API 实际调用效果、DM 是否送达（只验证「发起了 DM」）、真实 GitHub 授权往返。

## 目录结构

```
bot/
├── __main__.py            # python -m bot
├── core/
│   ├── bot.py             # Bot 子类与装配（prepare / setup_hook）
│   ├── run_loop.py        # 启动循环：首次连接失败按指数退避重试
│   ├── config.py          # pydantic-settings 读 .env
│   ├── logging.py         # 控制台 + 按天轮转
│   ├── i18n.py            # t(locale, key, **kw)，多来源合并
│   ├── translator.py      # 命令描述/参数说明的本地化
│   ├── database.py        # aiosqlite + 迁移 + 事务
│   ├── migrations/        # 001_init.sql（完整 schema，一次建到位）
│   ├── store.py           # 设置与模块状态读写
│   ├── loader.py          # 模块扫描/加载/热重载/失败隔离/依赖顺序
│   ├── framework.py       # /module 命令
│   ├── help.py            # /help：读命令树生成列表与详情
│   ├── checks.py          # 权限与角色层级
│   ├── permission_flags.py# 权限位名字的解析（频道与角色共用）
│   ├── audit.py           # mod-log 投递 + 审计 reason（处罚与撤销共用）
│   ├── ui.py              # Embed 工厂 + 确认按钮
│   ├── errors.py          # 全局错误处理
│   ├── clock.py           # 统一时间与 ISO 解析
│   ├── web.py             # 入站 HTTP 端点
│   ├── instance_lock.py   # 单实例守卫（OS 级排他锁）
│   └── module.py          # ModuleMeta 契约
├── integrations/                  # 不依赖 Discord 的外部集成
│   ├── github/                    # 设备流/Web flow 客户端、映射表、release 来源、监听服务
│   └── smas/                      # access server 客户端、节点匹配、绑定表、发码
├── locales/{zh-CN,en-US}.json     # 核心文案（框架与跨模块词汇）
└── modules/                       # 每个模块 = 代码 + 自己的 locales/
    ├── moderation/  ├── cases/      ├── channels/
    ├── roles/       ├── reaction_roles/  ├── tools/
    ├── github_bridge/  ├── github_feed/
    └── access_keys/    └── access_roles/
deploy/{systemd,docker}/
tests/
```

## GitHub 集成

核心是**账号映射**（`/link github`）：把 Discord 用户对应到 GitHub 账号，其余功能都建立在这张映射表上。

### 两种授权方式

| | web flow（推荐） | 设备流（兜底） |
| --- | --- | --- |
| 用户操作 | 点按钮 → 浏览器点一下授权 | 手输一次性码 |
| 需要 `client_secret` | **要** | 不要 |
| 需要公网回调地址 | **要** | 不要 |

`/link github` 默认 `method:auto`：**配置齐了走 web flow，否则自动退回设备流**（不会因为没配就报错）。想强制某一种就写 `method:web` 或 `method:device`。

### 前置准备（一次性）

1. GitHub → Settings → Developer settings → **OAuth Apps** → New OAuth App。
2. **Authorization callback URL** 填你准备用的回调地址（要和 `.env` 里的 `GITHUB_OAUTH_REDIRECT_URI` **一字不差**）。
3. 只想要设备流：进应用设置勾上 **Enable device flow**，把 **Client ID** 填进 `.env` 即可。
4. 想要 web flow：再生成一个 **Client Secret**，并把 `GITHUB_OAUTH_CLIENT_SECRET`、`GITHUB_OAUTH_REDIRECT_URI`、`WEB_PORT` 一起填好（见下）。

### release 推送到帖子（`github_feed`）

把某个仓库的 release notes 推到指定的**帖子或频道**，一个仓库对一个目标。

1. 在**目标帖子/频道里**运行 `/feed add example/repo` —— 默认就绑在你运行命令的地方（也可以用 `target` 指定别处）。
2. 绑定时会**把该仓库已有的 release 按时间顺序回填**进去，然后把游标停在新处。
3. 之后**两个入口都能推新的**：常驻订阅你的监听服务（推荐，见下），或手动 `/feed sync <仓库>`。

管理：`/feed list` 看已订阅的仓库、游标与订阅状态，`/feed remove <仓库>` 取消订阅（已推的消息不动）。都需要 **Manage Server**。

#### 实时推送：接 gh-webhook-watcher（SSE）

配好这两项，机器人就用 **SSE 常驻订阅**监听服务，有新 release 立刻推：

```env
WATCHER_BASE_URL=http://127.0.0.1:25056
WATCHER_API_TOKEN=<GHW_API_TOKEN>
```

* 走 `/api/stream`，断开自动重连（退避基准取服务端帧里的 `retry:`，上限 60 秒），重连时带 `Last-Event-ID` 续传。
* **进流之前先做一次区间检查**：SSE 不会告诉你「事件缓冲把游标挤掉了」，所以先用一次 `/api/events`（只取一个字段）确认没被挤掉；被挤掉就转去**全量对账**（按 GitHub API 把每个已订阅仓库的新 release 补齐）。
* 事件里带 `payload` 时（监听服务开了 `GHW_INCLUDE_PAYLOAD`）直接用它的 release 正文；**没带就按事件链接里的 tag 去 GitHub 取** —— 两条路都能用，所以你不必为了这个改监听服务的配置。
* 去重靠**每个目标的 release 游标**（不是事件游标）：事件重放、断线续传、服务重启都不会重复推送；一条推失败也不会推进游标，后面 `/feed sync` 还能补上。
* 我们自己那几句包装文案（「这个版本没有填写说明」「已截断」）**取服务器语言，没配就用 `DEFAULT_LOCALE`** —— 推送的消息没有交互对象，拿不到客户端语言。
* **历史永远来自 GitHub API**（`ReleaseSource` 接口）：监听服务只负责「刚刚发生了什么」，它不存历史、也不补发停机期间的投递。换成别的实现只动一个类。

#### 三点要知道的

* **release notes 是外部文本**：里面的 `@everyone` / 角色提及**一律不会生效**（发送时强制 `allowed_mentions=none`），超长正文会截断并留标题链接。
* **机器标记不会漏到帖子里**：发布说明里的 HTML 注释（如 `<!-- sp-compat {…} -->`）在 GitHub 上看不见，而 Discord 不渲染 HTML、会当成明文，所以推送前会清掉**所有** HTML 注释。清空后如果正文只剩标记，就显示「这个版本没有填写说明」。

## SMAS 集成

机器人以**自己的服务号**身份连 access server（不代替用户行事），配置见 `.env.example` 的 `ACCESS_*`。**所有 SMAS 操作都要求先 `/link github`**（包括管理命令）—— SMAS 的身份就是 GitHub 账号，没绑就不知道该把码或角色记到谁头上。因此 `access_keys` 与 `access_roles` 都依赖 `github_bridge`：后者被停用时它们也不能假装能用。

### 发激活码（两种模式，都从按钮进）

```
/key drop <批次> <数量> mode:抽奖      [role:角色] [deny_role:角色] [minutes:10] [team:id]
/key drop <批次> <数量> mode:先到先得  [role:角色] [deny_role:角色] [team:id]
/key close <编号>     # 立刻结束（抽奖会立即开奖）
/key list <批次>      # 看这批发给了谁（只有前缀）
/key deny-role <角色> # 持有该角色的人不能领取，也不能参与抽奖
/key allow-role <角色>
```

* **抽奖**：发一条带「参与抽奖」按钮的消息 → 大家点 → 截止后随机抽 N 人 → 中奖者私信收到码，频道里只公布名单。
* **先到先得**：发一条带「领取」按钮的消息 → 前 N 个点的人各得一枚；发满自动关掉按钮。
* `role` 限定参与资格（不填=所有人），**每次由命令指定**；`deny_role` 是**这一次活动自己**的排除角色（只对这次生效，会写在活动消息的「参与资格」那行）；`/key deny-role` 维护的是**服务器级**黑名单。两层都在资格之前生效 —— **先排除，再看资格**。

设计上的几条：

* **码只走私信**。频道消息、开奖公告、回执里**永远不出现完整的码**（只有前缀/人数），测试里有专门断言。
* **取码是服务端原子的**（`take`），**先到先得的名额是一条 SQL 自增封顶**，所以并发点击既不会超发也不会发重。
* **发不出去就还原**：对方关了私信时 → `release` 退码 + 撤登记 + **把名额还回去**，那枚码不会悬在「已发放但没人拿到」。
* **开奖靠库里的状态驱动**（后台每 30 秒扫一次到点的活动），所以重启也不会漏掉该开奖的活动。
* 一个 GitHub 账号只能绑一个人，所以「谁领到了哪一枚」永远有唯一的归属。

### 权限节点 → 角色（`access_roles`）

```
/access bind <节点模式> <角色>   # 持有该节点的人给这个角色
/access unbind <节点模式> [角色]
/access list
/access sync [dry_run]          # 立刻对齐；dry_run 只报告不动手
/access status <成员>           # 他为什么有这些角色
```

粒度是**具体节点 → 角色**（节点末段可以写 `*`，例如 `team.acme.*`）。**SMAS 是唯一权威**：只读它、只往 Discord 写，绝不反过来改服务器上的授权；角色由服务器预先建好，机器人只做绑定。

两条安全边界：

* **只动绑定表里出现过的角色** —— 别人手动发的角色永远不会被这次同步撤掉。
* **读失败不等于「他没有权限」** —— 某个成员的有效节点读不到（网络、权限、服务端拒绝）时跳过并如实计数，绝不按空集合去撤他的角色。

## License

本项目采用 GNU Affero General Public License v3.0（AGPL-3.0），详见 [LICENSE](LICENSE)。
