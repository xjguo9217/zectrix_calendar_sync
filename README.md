# Zectrix 墨水屏便利贴 ⇄ Apple 提醒事项 双向同步

把 [极趣 Zectrix](https://cloud.zectrix.com/) 墨水屏电子便利贴和 macOS 的「提醒事项」双向同步：
在哪边勾掉、改标题、改时间、删掉，另一边都会跟上。

> 本项目基于 [defia/zectrix_calendar_sync](https://github.com/defia/zectrix_calendar_sync)
> 二次开发（MIT）。原版是 CalDAV 日历 → 墨水屏的单向同步，
> 这个分支把它扩展成了 Apple 提醒事项 ⇄ 墨水屏的双向同步。
> 原作者：[defia](https://space.bilibili.com/13131424)

## 功能

**1）Apple 提醒事项 ⇄ Zectrix 待办（双向）**

| 你做的事 | 同步结果 |
|---|---|
| 提醒事项里新建一条 | 墨水屏出现同一条 |
| 墨水屏上新建一条 | 提醒事项里出现同一条 |
| 任意一边**划掉**（打勾） | 另一边也划掉 |
| 任意一边**取消划掉** | 另一边也取消 |
| 任意一边改标题 / 改时间 | 另一边跟着改 |
| 提醒事项里删掉 | 墨水屏上也删掉 |
| 墨水屏上删掉 | 提醒事项**保留**，只是不再同步（可配置） |
| 两边同时改了同一条 | 以**最后修改时间较新**的一方为准，并打印冲突详情 |

配对关系存在本地状态文件 `.sync_state.json`，**不靠标题匹配**，所以改了标题也不会重复创建。
万一状态文件丢了，还能从 Zectrix 待办备注里的 `UID: reminder:xxx` 自动恢复配对。

**2）日历事件 → Zectrix（单向，可选）**

日历上的会议推到墨水屏，标题带 `[日历]` 前缀。取事件的范围是**今天一整天（00:00 起）**，
不是「从现在起」—— 已经开始的会议要留在屏上，开始 `EXPIRE_HOURS` 小时后才自动划掉。
真的在日历里被取消/删除了才会同步删掉。已取消、以及你点过「拒绝」的会议会跳过。
带 `[日历]` 前缀的待办**不会**被推进提醒事项，两条链路互不干扰。

事件可以从两个来源取，默认两个都读并按 UID 去重：

| `CALENDAR_SOURCE` | 说明 |
|---|---|
| `both`（默认） | 两个都读，同一条事件不会在墨水屏上出现两遍 |
| `eventkit` | 读 macOS 日历 App 里的日历 —— iCloud / **Google（含 umd.edu 这类学校账号）** / Exchange 全都算，不需要任何密码 |
| `caldav` | 直连 CalDAV 服务器（见下方注意事项） |
| `none` | 不同步日历 |

## 一起同步工作日历（Gmail / Google Workspace / umd.edu）

**不需要 Google API、OAuth、也不需要 ics 密钥地址。** Google 早就关掉了 CalDAV 的密码登录，
但只要把账号加进 macOS，`eventkit` 来源就能直接读到：

1. **系统设置 → 互联网账户 → 添加 Google 账户**，登录你的 umd.edu，勾上「日历」
2. 确认脚本能看到它：

   ```bash
   ./run.sh --list-calendars
   ```

3. 名字会连账户一起列出来。只想同步其中几个的话，把名字填进 `CALENDAR_LISTS`：

   ```env
   CALENDAR_LISTS=Calendar,UMD Teaching
   ```

   留空 = 全部日历都同步。

工作日历通常事件多，建议顺手把范围收窄一点，别把整块墨水屏刷满：
`SYNC_DAYS_AHEAD=0` 只看今天（默认），或配合 `CALENDAR_LISTS` 只挑要紧的那个日历。

> 学校 IT 如果锁死了 Google 账号不让加进 macOS，退路是在 Google 日历网页版
> 「设置 → 某个日历 → 私密 iCal 地址」拿一个只读 ics 链接，然后在 macOS 日历里
> 「文件 → 新建日历订阅」订阅它 —— 订阅完照样走 `eventkit` 这条路，脚本不用改。

## 安装

```bash
pip install requests caldav icalendar python-dotenv pyobjc-framework-EventKit
```

## 桌面按钮（不用开终端）

```bash
./make_app.sh
```

桌面上会出现「**Zectrix 同步**」，双击就同步，完事弹一条通知告诉你同步了什么
（`提醒事项：新建 2 · 更新 1；日历：无变化`）。出错会弹对话框，权限问题那个还带
一个直接跳到隐私设置的按钮。日志在 `~/Library/Logs/zectrix-sync.log`。

app 本体生成在项目文件夹里，桌面上那个是**替身（alias）**。
app 靠 `path to me` 找项目目录，没写死任何绝对路径 —— 所以整个文件夹搬到哪都能用，
但别把 app 单独拖出去，它得和 `sync_calendar.py` 待在一起。

## 授权（必做，最容易卡住的一步）

EventKit 的权限是给**运行脚本的那个程序**的，不是给脚本本身：

| 你怎么跑 | 要在隐私设置里授权谁 |
|---|---|
| 双击桌面按钮 | 「Zectrix 同步」 |
| 终端里跑 `./run.sh` | 「终端」/ iTerm / 你的编辑器 |
| launchd 定时跑 | plist 里那个程序 |

**系统设置 → 隐私与安全性** 里有**两个独立开关**都要开：

- **提醒事项** —— 双向同步用
- **日历** —— `eventkit` 来源读日历事件用（只同步提醒事项的话可以不开）

第一次跑系统会弹窗，点「允许」。脚本打印 `没有提醒事项访问权限` /
`没有日历访问权限` 就是对应那个没开。两个是分开的，开了一个另一个照样会报。

## 配置

在脚本同目录建 `.env`（注意 `=` 后面**不要加空格**，否则 `run.sh` 里的 `source .env` 会报错）：

```env
API_KEY=你的-api-key
DEVICE_ID=你的-device-id

# 只同步提醒事项的话，下面这段可以整段删掉
CALDAV_URL=https://caldav.icloud.com/
CALDAV_USER=你的邮箱
CALDAV_PASS=你的密码或应用专用密码
```

### 全部环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `API_KEY` | — | **必填**，Zectrix API Key |
| `DEVICE_ID` | — | **必填**，设备 ID |
| `API_BASE` | `https://cloud.zectrix.com/open/v1` | Zectrix API 地址 |
| `SYNC_DAYS_BACK` | `0` | 往前几天的任务纳入同步（`1` = 也管昨天的） |
| `SYNC_DAYS_AHEAD` | `0` | 往后几天的任务纳入同步（`0` = 只同步今天） |
| `COMPLETED_LOOKBACK_DAYS` | `14` | 读取最近多少天内完成的提醒（用来同步「划掉」） |
| `DEFAULT_DUE_TIME` | `09:00` | 提醒只有日期没时间时用它 |
| `INCLUDE_UNDATED` | `0` | 没有截止日期的提醒是否当作今天同步过去 |
| `SYNC_NEW_COMPLETED` | `0` | 是否把**已完成**的新任务也建到对面（默认不建，避免刷屏） |
| `DELETE_POLICY` | `apple-master` | 见下 |
| `APPLE_LIST` | 系统默认列表 | 新建提醒放进哪个列表 |
| `APPLE_LISTS` | 全部 | 只同步这些列表，逗号分隔，如 `个人,工作` |
| `APPLE_ADD_ALARM` | `0` | 新建提醒时是否加一个到点通知 |
| `SYNC_STATE_FILE` | 脚本同目录 `.sync_state.json` | 配对状态文件路径 |
| `TOMBSTONE_DAYS` | `30` | 解绑记录保留多少天 |
| `CALENDAR_SOURCE` | `both` | `both` / `eventkit` / `caldav` / `none` |
| `CALENDAR_LISTS` | 全部 | 只同步这些日历，逗号分隔，如 `Calendar,UMD` |
| `CALDAV_URL` / `CALDAV_USER` / `CALDAV_PASS` | — | 留空则跳过 CalDAV 来源 |
| `EXPIRE_HOURS` | `1` | `[日历]` 日程过期多久后自动划掉 |

### `DELETE_POLICY`

| 值 | 提醒事项里删掉 | 墨水屏上删掉 |
|---|---|---|
| `apple-master`（默认） | 墨水屏同步删掉 | 提醒事项保留，只解绑 |
| `mirror` | 墨水屏同步删掉 | 提醒事项同步删掉 |
| `none` | 都不删，只解绑 | 都不删，只解绑 |

默认之所以不对称：墨水屏是「今天要做什么」的临时展示，提醒事项才是真正存东西的地方，
不该因为便利贴上擦掉一条就把你的提醒事项删了。解绑后会记一条 30 天的墓碑，
所以不会下次又给你建回来。

## 运行

```bash
python3 sync_calendar.py
```

先看看会发生什么、不实际写入：

```bash
python3 sync_calendar.py --dry-run
```

其他参数：

| 参数 | 作用 |
|---|---|
| `--dry-run` | 只打印将要执行的操作，不做任何写入（也不写状态文件） |
| `--no-reminders` | 跳过提醒事项双向同步 |
| `--no-calendar` | 跳过日历同步 |
| `--list-calendars` | 列出能看到的提醒事项列表和日历（含所属账户），然后退出 |
| `--reset-state` | 清空配对状态重新建立（不会删除任何任务） |

**第一次跑建议加 `--dry-run` 看一眼**，确认同步范围和要创建的条目符合预期再正式跑。

## 换一台电脑用

在新的 Mac 上：

```bash
git clone https://github.com/defia/zectrix_calendar_sync.git
cd zectrix_calendar_sync
./setup.sh
```

`setup.sh` 会建虚拟环境、装依赖、生成桌面按钮，最后触发系统授权弹窗。
重复跑是安全的。

中间它会停下来要 `.env` —— 这个文件**不在 git 里**（里面是你的 API Key），
从老电脑直接拷过去最省事：

```bash
scp 老电脑:~/zectrix_calendar_sync/.env .
```

或者照着 `.env.example` 填一份新的。填完再跑一次 `./setup.sh`。

### 三样不会自动跟过去的东西

**1. 隐私授权** —— 每台 Mac 各自记，不跟 iCloud 同步。新机器上「提醒事项」和
「日历」两个开关都要重开一次。`setup.sh` 最后会主动触发弹窗。

**2. 工作日历账号** —— umd.edu 之类要在新机器的「系统设置 → 互联网账户」里重新加。

**3. `.sync_state.json` —— 这个千万别拷。**

第 3 点值得展开说：`calendarItemIdentifier`（提醒的本机编号）是**每台 Mac
本地数据库自己的行号**，同一条 iCloud 提醒在另一台 Mac 上编号完全不同，
拷过去的状态文件在新机器上全是无效编号，反而会添乱。

那新机器怎么知道哪条待办对应哪条提醒、而不是全部重建一遍？靠写在 Zectrix
待办备注里的 `UID: reminder:xxx` —— 存的是 `calendarItemExternalIdentifier`，
这个是**跨设备稳定**的。新机器第一次跑会打印
「从待办备注里认回了 N 条已有配对」，然后接着同步，不会产生重复条目。
这条路径有测试盯着（`TestSecondMac`）。

### 两台一起用

没问题，各跑各的，最终收敛到一致。唯一要避开的是**同一秒钟两台同时跑** ——
那一瞬间两边都还没看到对方建的东西，可能各建一条重复的。
日常手点按钮撞不上；如果两台都配了 launchd 定时，把间隔错开几分钟。

### 不是 Mac 的话

`setup.sh` 在 Windows / Linux 上会明确告诉你：**提醒事项同步用不了**，
EventKit 是 macOS 独有的，没有等价的东西能读写 Apple 提醒事项。
能跑的只剩 CalDAV 日历 → 墨水屏这条单向链路（`CALENDAR_SOURCE=caldav`），
而且桌面按钮是 `.app`，也只在 macOS 上有。

真要在 PC 上用，比较现实的做法是：找一台常开的 Mac 定时跑同步，
PC 那边该在提醒事项 App / iCloud 网页版上改就在那改，改动会通过 iCloud
传到那台 Mac，再由它推到墨水屏。

## 定时运行

macOS 上建议用 launchd（cron 拿提醒事项权限比较麻烦）。
新建 `~/Library/LaunchAgents/com.zectrix.sync.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.zectrix.sync</string>
  <key>ProgramArguments</key>
  <array>
    <string>/绝对路径/zectrix_calendar_sync/.venv/bin/python3</string>
    <string>/绝对路径/zectrix_calendar_sync/sync_calendar.py</string>
  </array>
  <key>WorkingDirectory</key><string>/绝对路径/zectrix_calendar_sync</string>
  <key>StartInterval</key><integer>300</integer>
  <key>StandardOutPath</key><string>/tmp/zectrix_sync.log</string>
  <key>StandardErrorPath</key><string>/tmp/zectrix_sync.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.zectrix.sync.plist
```

第一次执行时系统会弹提醒事项授权窗，允许一次即可。

## 排查：同步"成功"但什么都没发生

日志在 `~/Library/Logs/zectrix-sync.log`（终端跑和点按钮都会写）。按这个顺序看：

**1. 提醒读到 0 条?**

```
Apple: 未完成 0 条（有日期）+ 0 条（没日期），最近 14 天内完成 0 条
```

列表名读到了、内容却是 0 —— 依次查这三个（**踩过的坑就是第一个**）：

- **iCloud 同步没开**。「系统设置 → Apple 账户 → iCloud」里确认「提醒事项」是打开的。
  关着的时候列表名照样读得到，内容一律为空，和"你真的没有提醒"长得一模一样，
  EventKit 不会给任何提示。
- **权限档位是「仅添加」不是「完全访问」**。脚本会主动打印这一条警告。
- **列表是「我的 Mac」本地列表**。脚本也会警告；这种列表不跨设备同步。

**2. 提醒读到了，但没同步过去?**

看跳过原因，脚本会告诉你调哪个开关：

```
    跳过 34 条：没有截止日期（想同步的话设 INCLUDE_UNDATED=1）
  跳过 8 条提醒：不在同步范围 2026-08-08 ~ 2026-08-08 内（想扩大就调 SYNC_DAYS_AHEAD）
  跳过 3 条提醒：已完成且还没配过对（想同步的话设 SYNC_NEW_COMPLETED=1）
```

**3. 报"没有访问权限"?**

报错里会点名是**哪个程序**缺权限。三份授权互相独立，别开错对象：

| 你怎么跑 | 要授权谁 |
|---|---|
| 双击桌面按钮 | `Zectrix 同步` |
| 终端 `./run.sh` | 终端 / iTerm |
| 别的 App 调起来 | 那个 App |

而且「提醒事项」和「日历」是**两个开关**，开了一个另一个照样会报。

**4. 日历里删了，墨水屏上没删?**

看这行读到了哪些日历：

```
    合计 3 个日程，读到的日历: Home, Work, xjguo@umd.edu
```

日程所属的日历不在这个列表里就不会被删（避免另一台没配好账号的电脑误删）。
日志会明确说 `保留 N 条日程：这台电脑读不到它们所在的日历（xxx）`。

**5. 墨水屏上还是旧内容?**

先看云端对不对。云端已经变了就是设备还没轮询，等它下次刷新。

## 测试

```bash
python3 -m pytest test_sync_calendar.py -v
```

EventKit 需要系统授权，没法在测试里真读写提醒事项，
所以用内存替身把双向同步引擎的每条分支都跑了一遍：完成/取消完成传播、冲突裁决、
三种删除策略、多来源日历去重、「换第二台 Mac 不能建重复」，
以及「跑第二遍必须是空操作」的收敛性检查（这组当初直接抓出过一个死循环 bug）。

## 说明与注意

- 时区自动处理；只有日期没有时间的提醒按 `DEFAULT_DUE_TIME` 处理。
- 已配对的任务**即使超出同步窗口也会继续同步完成状态** —— 昨天的事今天勾掉，一样传得过去。
  窗口只用来决定「哪些新任务值得建到对面」。
- `[日历]` 前缀的待办是日历链路专用的，别手动改它的标题前缀。
- 重复提醒（每天/每周）目前只按单条任务同步，不会同步重复规则。

## License

MIT
