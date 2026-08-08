#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Zectrix 墨水屏便利贴 <-> Apple 提醒事项 双向同步。

两条相互独立的同步链路：

1) Apple 提醒事项 <-> Zectrix 待办（双向）
   新建 / 改标题 / 改时间 / 完成 / 取消完成 / 删除 都会同步。
   两边都改过时，以「最后修改时间」较新的一方为准。
   配对关系保存在本地状态文件里（默认 .sync_state.json），
   所以不依赖标题匹配，改了标题也不会重复创建。

2) CalDAV 日历事件 -> Zectrix（单向，可选）
   标题带 [日历] 前缀，过期后自动划掉。带前缀的待办不会被推到提醒事项，
   两条链路互不干扰。

用法：
    python3 sync_calendar.py                 # 正常同步
    python3 sync_calendar.py --dry-run       # 只打印将要执行的操作，不写入
    python3 sync_calendar.py --no-calendar   # 只同步 Apple 提醒事项
    python3 sync_calendar.py --no-reminders  # 只同步 CalDAV 日历
"""

import argparse
import datetime
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests
from icalendar import Calendar

try:
    from dotenv import load_dotenv
    # 默认不覆盖已存在的环境变量，所以 FOO=1 ./run.sh 能临时改配置
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
        print("⚠️  有 .env 但没装 python-dotenv，配置读不到："
              "pip install python-dotenv", flush=True)


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    val = _env(name, "1" if default else "0").lower()
    return val in ("1", "true", "yes", "y", "on")


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Zectrix API
API_BASE = _env("API_BASE", "https://cloud.zectrix.com/open/v1")
API_KEY = _env("API_KEY")
DEVICE_ID = _env("DEVICE_ID")

# 日历事件（单向推到墨水屏）
#   eventkit  读 macOS 日历 —— iCloud / Google(含 umd.edu) / Exchange 全都在这儿，
#             前提是账号加进了「系统设置 → 互联网账户」，不需要任何密码
#   caldav    直连 CalDAV 服务器（Google 已经不支持密码登录，只有 iCloud 这类能用）
#   both      两个都读，按 UID 去重（默认）
#   none      不同步日历
CALENDAR_SOURCE = _env("CALENDAR_SOURCE", "both")
CALENDAR_LISTS = _env("CALENDAR_LISTS")      # 只读这些日历，逗号分隔，留空 = 全部
CALDAV_URL = _env("CALDAV_URL", "https://caldav.icloud.com/")
CALDAV_USER = _env("CALDAV_USER")
CALDAV_PASS = _env("CALDAV_PASS")
EXPIRE_HOURS = _env_int("EXPIRE_HOURS", 1)   # [日历] 日程过期多久后划掉

# 双向同步范围
STATE_FILE = _env("SYNC_STATE_FILE", os.path.join(SCRIPT_DIR, ".sync_state.json"))
SYNC_DAYS_BACK = _env_int("SYNC_DAYS_BACK", 0)          # 往前几天的任务也纳入
SYNC_DAYS_AHEAD = _env_int("SYNC_DAYS_AHEAD", 0)        # 往后几天的任务也纳入
COMPLETED_LOOKBACK_DAYS = _env_int("COMPLETED_LOOKBACK_DAYS", 14)  # 读取多久内完成的提醒
DEFAULT_DUE_TIME = _env("DEFAULT_DUE_TIME", "09:00")    # 提醒只有日期没有时间时用它
INCLUDE_UNDATED = _env_bool("INCLUDE_UNDATED", False)   # 没有截止日期的提醒是否当作今天
SYNC_NEW_COMPLETED = _env_bool("SYNC_NEW_COMPLETED", False)  # 是否把「已完成」的新任务也建到对面
TOMBSTONE_DAYS = _env_int("TOMBSTONE_DAYS", 30)

# Apple 提醒事项列表
APPLE_LIST = _env("APPLE_LIST")            # 新建提醒放进哪个列表，留空 = 系统默认列表
APPLE_LISTS = _env("APPLE_LISTS")          # 只读取这些列表（逗号分隔），留空 = 全部
APPLE_ADD_ALARM = _env_bool("APPLE_ADD_ALARM", False)   # 新建提醒时是否加一个到点提醒

# 删除策略：
#   apple-master（默认）提醒事项删了 -> 墨水屏也删；墨水屏删了 -> 只解绑，保留提醒事项
#   mirror          任意一边删了 -> 另一边也删
#   none            两边都不删，只解绑
DELETE_POLICY = _env("DELETE_POLICY", "apple-master")

CALENDAR_PREFIX = "[日历]"
MAX_RETRIES = 3
EK_TIMEOUT = 60  # EventKit 回调等待秒数


def log(msg: str = "") -> None:
    print(msg, flush=True)


def retry(func, *args, **kwargs):
    """指数退避重试。func 返回 False 视为失败，返回 None 表示彻底失败。"""
    for attempt in range(MAX_RETRIES):
        try:
            result = func(*args, **kwargs)
            if result is not False:
                return result
            reason = "返回失败"
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
        if attempt < MAX_RETRIES - 1:
            delay = 2 ** attempt
            log(f"    第 {attempt + 1}/{MAX_RETRIES} 次失败（{reason}），{delay} 秒后重试...")
            time.sleep(delay)
        else:
            log(f"    已重试 {MAX_RETRIES} 次仍失败（{reason}），放弃")
    return None


# --------------------------------------------------------------------------
# 统一的任务模型
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Task:
    """两边共有的字段，用来比较「有没有变化」。"""
    title: str
    due_date: str        # YYYY-MM-DD
    due_time: str        # HH:MM
    completed: bool

    def to_dict(self) -> Dict:
        return {
            "title": self.title,
            "dueDate": self.due_date,
            "dueTime": self.due_time,
            "completed": self.completed,
        }

    @staticmethod
    def from_dict(d: Dict) -> "Task":
        return Task(
            title=d.get("title", ""),
            due_date=d.get("dueDate", ""),
            due_time=d.get("dueTime", ""),
            completed=bool(d.get("completed", False)),
        )

    def describe(self) -> str:
        mark = "✓" if self.completed else "○"
        return f"{mark} {self.title} @ {self.due_date} {self.due_time}"


@dataclass
class ZTodo:
    todo_id: int
    task: Task
    description: str
    updated_at: float


@dataclass
class ARem:
    apple_id: str
    ext_id: str
    task: Task
    updated_at: float
    list_name: str
    obj: object = field(repr=False, default=None)


def _normalize_time(value: str) -> str:
    """把 9:5 / 09:05:00 之类统一成 HH:MM。"""
    if not value:
        return DEFAULT_DUE_TIME
    parts = value.split(":")
    try:
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    except (ValueError, IndexError):
        return DEFAULT_DUE_TIME


# --------------------------------------------------------------------------
# 同步状态（配对关系 + 上次同步时的快照）
# --------------------------------------------------------------------------

class SyncState:
    """记录 Apple 提醒 <-> Zectrix 待办的配对，以及上一次同步成功时两边的内容。

    有了快照才能区分「这一边改了」和「那一边改了」，而不是无脑覆盖。
    """

    VERSION = 2

    def __init__(self, path: str):
        self.path = path
        self.links: Dict[str, Dict] = {}      # apple_id -> {zectrix_id, ext_id, snapshot, updated_at}
        self.tomb_apple: Dict[str, str] = {}  # apple_id -> 时间戳，不再导入 Zectrix
        self.tomb_zectrix: Dict[str, str] = {}  # str(zectrix_id) -> 时间戳，不再导出到 Apple
        self.dirty = False
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            log(f"未找到状态文件，按首次同步处理: {self.path}")
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            log(f"⚠️  状态文件读取失败（{exc}），按首次同步处理: {self.path}")
            return
        if data.get("version") != self.VERSION:
            log(f"状态文件版本不符（{data.get('version')} != {self.VERSION}），忽略旧状态")
            return
        self.links = data.get("links", {}) or {}
        self.tomb_apple = data.get("tombstonesApple", {}) or {}
        self.tomb_zectrix = data.get("tombstonesZectrix", {}) or {}
        log(f"已加载 {len(self.links)} 条配对关系")

    def save(self, dry_run: bool) -> None:
        if dry_run:
            log("[DRY RUN] 不写入状态文件")
            return
        if not self.dirty:
            return
        payload = {
            "version": self.VERSION,
            "updatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
            "links": self.links,
            "tombstonesApple": self.tomb_apple,
            "tombstonesZectrix": self.tomb_zectrix,
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # -- 配对操作 ---------------------------------------------------------

    def link(self, apple_id: str, ext_id: str, zectrix_id: int, snapshot: Task,
             list_name: str = "") -> None:
        # listName 用来判断「这台电脑看不到这个列表」，避免跨机器误删
        previous = self.links.get(apple_id, {})
        self.links[apple_id] = {
            "zectrixId": zectrix_id,
            "extId": ext_id,
            "listName": list_name or previous.get("listName", ""),
            "snapshot": snapshot.to_dict(),
            "updatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        self.tomb_apple.pop(apple_id, None)
        self.tomb_zectrix.pop(str(zectrix_id), None)
        self.dirty = True

    def unlink(self, apple_id: str) -> None:
        if self.links.pop(apple_id, None) is not None:
            self.dirty = True

    def tombstone_apple(self, apple_id: str) -> None:
        self.tomb_apple[apple_id] = datetime.datetime.now().isoformat(timespec="seconds")
        self.dirty = True

    def tombstone_zectrix(self, zectrix_id: int) -> None:
        self.tomb_zectrix[str(zectrix_id)] = datetime.datetime.now().isoformat(timespec="seconds")
        self.dirty = True

    def zectrix_ids(self) -> Dict[int, str]:
        """zectrix_id -> apple_id"""
        return {link["zectrixId"]: apple_id for apple_id, link in self.links.items()}

    def prune_tombstones(self) -> None:
        cutoff = datetime.datetime.now() - datetime.timedelta(days=TOMBSTONE_DAYS)
        for store in (self.tomb_apple, self.tomb_zectrix):
            for key, stamp in list(store.items()):
                try:
                    if datetime.datetime.fromisoformat(stamp) < cutoff:
                        del store[key]
                        self.dirty = True
                except ValueError:
                    del store[key]
                    self.dirty = True


# --------------------------------------------------------------------------
# Zectrix 云端
# --------------------------------------------------------------------------

class ZectrixClient:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}

    # -- 读 ---------------------------------------------------------------

    def list_todos(self) -> Optional[List[ZTodo]]:
        """拉取该设备下全部待办（含已完成）。失败返回 None，而不是空列表。"""

        def _get():
            resp = requests.get(
                f"{API_BASE}/todos",
                headers=self.headers,
                params={"deviceId": DEVICE_ID},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                log(f"  获取待办列表失败: {data.get('msg')}")
                return False
            return data.get("data") or []

        raw = retry(_get)
        if raw is None:
            return None

        todos = []
        for item in raw:
            todos.append(ZTodo(
                todo_id=item["id"],
                task=Task(
                    title=(item.get("title") or "").strip(),
                    due_date=item.get("dueDate") or "",
                    due_time=_normalize_time(item.get("dueTime") or ""),
                    completed=bool(item.get("completed", item.get("status") == 1)),
                ),
                description=item.get("description") or "",
                updated_at=float(item.get("updateDate") or 0),
            ))
        log(f"Zectrix: 读取到 {len(todos)} 条待办"
            f"（未完成 {sum(1 for t in todos if not t.task.completed)}）")
        return todos

    # -- 写 ---------------------------------------------------------------

    def create(self, task: Task, description: str, priority: int = 0) -> Optional[int]:
        """创建待办，返回新 id。"""
        if self.dry_run:
            log(f"    [DRY RUN] Zectrix 新建: {task.describe()}")
            return None

        def _create():
            payload = {
                "title": task.title,
                "description": description,
                "dueDate": task.due_date,
                "dueTime": task.due_time,
                "repeatType": "none",
                "priority": priority,
                "deviceId": DEVICE_ID,
            }
            resp = requests.post(f"{API_BASE}/todos", headers=self.headers,
                                 json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                log(f"    Zectrix 新建失败: {data.get('msg')}")
                return False
            return (data.get("data") or {}).get("id")

        todo_id = retry(_create)
        if todo_id:
            log(f"    Zectrix 新建成功 (id={todo_id}): {task.describe()}")
            if task.completed:
                self.set_completed(todo_id, task, description, True)
        return todo_id

    def update_fields(self, todo_id: int, task: Task, description: str) -> bool:
        """更新标题/时间/备注（不含完成状态）。"""
        if self.dry_run:
            log(f"    [DRY RUN] Zectrix 更新 id={todo_id}: {task.describe()}")
            return True

        def _update():
            payload = {
                "title": task.title,
                "description": description,
                "dueDate": task.due_date,
                "dueTime": task.due_time,
            }
            resp = requests.put(f"{API_BASE}/todos/{todo_id}", headers=self.headers,
                                json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                log(f"    Zectrix 更新失败 id={todo_id}: {data.get('msg')}")
                return False
            return True

        return retry(_update) is True

    def set_completed(self, todo_id: int, task: Task, description: str,
                      completed: bool) -> bool:
        """划掉 / 取消划掉。

        完成走 PUT /todos/{id}/complete；取消完成该接口没有对应端点，
        改用 PUT /todos/{id} 带 status=0。
        """
        verb = "划掉" if completed else "取消划掉"
        if self.dry_run:
            log(f"    [DRY RUN] Zectrix {verb} id={todo_id}: {task.title}")
            return True

        def _complete():
            resp = requests.put(f"{API_BASE}/todos/{todo_id}/complete",
                                headers=self.headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                log(f"    Zectrix 划掉失败 id={todo_id}: {data.get('msg')}")
                return False
            return True

        def _uncomplete():
            payload = {
                "title": task.title,
                "description": description,
                "dueDate": task.due_date,
                "dueTime": task.due_time,
                "status": 0,
                "completed": False,
            }
            resp = requests.put(f"{API_BASE}/todos/{todo_id}", headers=self.headers,
                                json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                log(f"    Zectrix 取消划掉失败 id={todo_id}: {data.get('msg')}")
                return False
            return True

        ok = retry(_complete if completed else _uncomplete) is True
        if ok:
            log(f"    Zectrix 已{verb} id={todo_id}: {task.title}")
        return ok

    def delete(self, todo_id: int) -> bool:
        if self.dry_run:
            log(f"    [DRY RUN] Zectrix 删除 id={todo_id}")
            return True

        def _delete():
            resp = requests.delete(f"{API_BASE}/todos/{todo_id}",
                                   headers=self.headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                log(f"    Zectrix 删除失败 id={todo_id}: {data.get('msg')}")
                return False
            return True

        ok = retry(_delete) is True
        if ok:
            log(f"    Zectrix 已删除 id={todo_id}")
        return ok


# --------------------------------------------------------------------------
# Apple 提醒事项（EventKit）
# --------------------------------------------------------------------------

class AppleRemindersError(RuntimeError):
    pass


def request_eventkit_access(store, entity: str) -> None:
    """申请「提醒事项」或「日历」权限，没批就抛异常。

    entity: "reminders" 或 "events"。两者在系统设置里是两个独立开关。
    """
    from EventKit import EKEntityTypeEvent, EKEntityTypeReminder

    is_reminders = entity == "reminders"
    label = "提醒事项" if is_reminders else "日历"
    pane = "Privacy_Reminders" if is_reminders else "Privacy_Calendars"

    done = threading.Event()
    result: Dict = {}

    def callback(granted, error):
        result["granted"] = bool(granted)
        result["error"] = error
        done.set()

    full_access = ("requestFullAccessToRemindersWithCompletion_" if is_reminders
                   else "requestFullAccessToEventsWithCompletion_")
    if hasattr(store, full_access):
        getattr(store, full_access)(callback)
    else:  # macOS 13 及更早
        store.requestAccessToEntityType_completion_(
            EKEntityTypeReminder if is_reminders else EKEntityTypeEvent, callback)

    if not done.wait(EK_TIMEOUT):
        raise AppleRemindersError(f"等待{label}授权超时")
    if not result.get("granted"):
        raise AppleRemindersError(
            f"没有{label}访问权限。请到「系统设置 → 隐私与安全性 → {label}」，"
            "为运行这个脚本的程序（Zectrix 同步.app / 终端）打开开关。"
            f"（设置直达: x-apple.systempreferences:com.apple.preference.security?{pane}）"
        )

    store.refreshSourcesIfNecessary()


class AppleReminders:
    """EventKit 提醒事项封装。所有异步回调都在这里收敛成同步调用。"""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.store = None
        self._calendars = None

    # -- 连接 -------------------------------------------------------------

    def connect(self) -> None:
        from EventKit import EKEventStore, EKEntityTypeReminder

        self.store = EKEventStore.alloc().init()
        request_eventkit_access(self.store, "reminders")

        all_lists = list(self.store.calendarsForEntityType_(EKEntityTypeReminder) or [])
        if APPLE_LISTS:
            wanted = {n.strip() for n in APPLE_LISTS.split(",") if n.strip()}
            self._calendars = [c for c in all_lists if str(c.title()) in wanted]
            missing = wanted - {str(c.title()) for c in all_lists}
            if missing:
                log(f"⚠️  APPLE_LISTS 里这些列表不存在: {', '.join(sorted(missing))}")
        else:
            self._calendars = all_lists

        if not self._calendars:
            raise AppleRemindersError("没有可用的提醒事项列表")

        # 带上所属账户：如果是「我的 Mac」这类本地列表，它根本不跨设备同步，
        # 换台电脑就看不到 —— 这是多机使用时最容易踩的坑
        described = []
        local = []
        for cal in self._calendars:
            source = cal.source()
            account = str(source.title()) if source else "?"
            described.append(f"{str(cal.title())}（{account}）")
            if account in ("Local", "On My Mac", "我的 Mac"):
                local.append(str(cal.title()))
        log(f"Apple: 读取列表 {', '.join(described)}")
        if local:
            log(f"  ⚠️  这些是本机列表，不会同步到你的其他 Mac: {', '.join(local)}")
            log("      多台电脑一起用的话，把提醒挪到 iCloud 账户下的列表里。")

    def list_names(self) -> List[str]:
        """这次真正读到的提醒列表名，用于删除判断。"""
        return [str(c.title()) for c in (self._calendars or [])]

    def _target_calendar(self):
        """新建提醒放哪个列表。"""
        if APPLE_LIST:
            for cal in self._calendars:
                if str(cal.title()) == APPLE_LIST:
                    return cal
            log(f"⚠️  找不到列表 {APPLE_LIST}，改用系统默认列表")
        cal = self.store.defaultCalendarForNewReminders()
        if cal is None and self._calendars:
            cal = self._calendars[0]
        return cal

    # -- 读 ---------------------------------------------------------------

    def _fetch(self, predicate) -> List:
        done = threading.Event()
        box: Dict = {}

        def callback(items):
            box["items"] = list(items or [])
            done.set()

        self.store.fetchRemindersMatchingPredicate_completion_(predicate, callback)
        if not done.wait(EK_TIMEOUT):
            raise AppleRemindersError("读取提醒事项超时")
        return box.get("items", [])

    def fetch_all(self) -> List[ARem]:
        """未完成提醒 + 最近 COMPLETED_LOOKBACK_DAYS 天内完成的提醒。

        注意 predicateForIncompleteReminders...(nil, nil, ...) 只返回**设了截止
        日期**的提醒，没日期的一条都拿不到。所以再用 predicateForRemindersInCalendars_
        兜一次底，把没日期的捞回来 —— 不然「我明明有一堆提醒，怎么同步说 0 条」
        根本无从查起。
        """
        from Foundation import NSDate

        dated = self._fetch(
            self.store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
                None, None, self._calendars)
        )

        now = datetime.datetime.now()
        start = now - datetime.timedelta(days=COMPLETED_LOOKBACK_DAYS)
        completed = self._fetch(
            self.store.predicateForCompletedRemindersWithCompletionDateStarting_ending_calendars_(
                NSDate.dateWithTimeIntervalSince1970_(start.timestamp()),
                NSDate.dateWithTimeIntervalSince1970_(now.timestamp()),
                self._calendars)
        )

        seen_ids = {str(i.calendarItemIdentifier()) for i in dated + completed}
        undated = [
            item for item in self._fetch(
                self.store.predicateForRemindersInCalendars_(self._calendars))
            if not item.isCompleted()
            and item.dueDateComponents() is None
            and str(item.calendarItemIdentifier()) not in seen_ids
        ]

        log(f"Apple: 未完成 {len(dated)} 条（有日期）+ {len(undated)} 条（没日期）"
            f"，最近 {COMPLETED_LOOKBACK_DAYS} 天内完成 {len(completed)} 条")

        out, skipped = [], {"没有截止日期": 0, "日期无法解析": 0, "没有标题": 0}
        for item in dated + completed + undated:
            parsed = self._to_arem(item, skipped)
            if parsed is not None:
                out.append(parsed)

        for reason, count in skipped.items():
            if count:
                hint = "（想同步的话设 INCLUDE_UNDATED=1）" if reason == "没有截止日期" else ""
                log(f"    跳过 {count} 条：{reason}{hint}")
        return out

    def lookup(self, apple_id: str, ext_id: str = "") -> Optional[ARem]:
        """按标识符直接查一条提醒，用来区分「被删了」和「不在读取范围内」。"""
        item = self.store.calendarItemWithIdentifier_(apple_id) if apple_id else None
        if item is None and ext_id:
            items = self.store.calendarItemsWithExternalIdentifier_(ext_id) or []
            item = items[0] if items else None
        return self._to_arem(item) if item is not None else None

    def _to_arem(self, item, skipped: Optional[Dict[str, int]] = None) -> Optional[ARem]:
        def skip(reason: str):
            if skipped is not None:
                skipped[reason] = skipped.get(reason, 0) + 1
            return None

        title = str(item.title() or "").strip()
        if not title:
            return skip("没有标题")

        due = item.dueDateComponents()
        if due is None:
            if not INCLUDE_UNDATED:
                return skip("没有截止日期")
            today = datetime.date.today()
            due_date, due_time = today.strftime("%Y-%m-%d"), DEFAULT_DUE_TIME
        else:
            year, month, day = due.year(), due.month(), due.day()
            # NSDateComponents 里没设置的字段是 NSUndefinedDateComponent (一个很大的负数)
            if not (1 <= (month or 0) <= 12 and 1 <= (day or 0) <= 31 and (year or 0) > 1970):
                return skip("日期无法解析")
            hour, minute = due.hour(), due.minute()
            if not (0 <= (hour or 0) <= 23):
                hour = None
            if hour is None or not (0 <= (minute or 0) <= 59):
                hour, minute = [int(x) for x in DEFAULT_DUE_TIME.split(":")]
            due_date = f"{year:04d}-{month:02d}-{day:02d}"
            due_time = f"{hour:02d}:{minute:02d}"

        modified = item.lastModifiedDate() or item.creationDate()
        return ARem(
            apple_id=str(item.calendarItemIdentifier() or ""),
            ext_id=str(item.calendarItemExternalIdentifier() or ""),
            task=Task(title=title, due_date=due_date, due_time=due_time,
                      completed=bool(item.isCompleted())),
            updated_at=float(modified.timeIntervalSince1970()) if modified else 0.0,
            list_name=str(item.calendar().title()) if item.calendar() else "",
            obj=item,
        )

    # -- 写 ---------------------------------------------------------------

    def _save(self, reminder) -> bool:
        ok, error = self.store.saveReminder_commit_error_(reminder, True, None)
        if not ok:
            log(f"    Apple 保存失败: {error}")
        return bool(ok)

    def create(self, task: Task) -> Optional[ARem]:
        from EventKit import EKReminder

        if self.dry_run:
            log(f"    [DRY RUN] Apple 新建提醒: {task.describe()}")
            return None

        calendar = self._target_calendar()
        if calendar is None:
            log("    找不到可写入的提醒事项列表，跳过")
            return None

        reminder = EKReminder.reminderWithEventStore_(self.store)
        reminder.setCalendar_(calendar)
        self._apply(reminder, task, is_new=True)

        if not self._save(reminder):
            return None

        created = self._to_arem(reminder)
        log(f"    Apple 新建成功: {task.describe()}（列表: {str(calendar.title())}）")
        return created

    def update(self, rem: ARem, task: Task) -> bool:
        if self.dry_run:
            log(f"    [DRY RUN] Apple 更新提醒: {rem.task.describe()} -> {task.describe()}")
            return True
        self._apply(rem.obj, task, is_new=False)
        if self._save(rem.obj):
            log(f"    Apple 已更新: {task.describe()}")
            return True
        return False

    def delete(self, rem: ARem) -> bool:
        if self.dry_run:
            log(f"    [DRY RUN] Apple 删除提醒: {rem.task.describe()}")
            return True
        ok, error = self.store.removeReminder_commit_error_(rem.obj, True, None)
        if not ok:
            log(f"    Apple 删除失败: {error}")
            return False
        log(f"    Apple 已删除: {rem.task.title}")
        return True

    def _apply(self, reminder, task: Task, is_new: bool) -> None:
        from Foundation import NSDateComponents

        reminder.setTitle_(task.title)

        year, month, day = (int(x) for x in task.due_date.split("-"))
        hour, minute = (int(x) for x in task.due_time.split(":"))
        comps = NSDateComponents.alloc().init()
        comps.setYear_(year)
        comps.setMonth_(month)
        comps.setDay_(day)
        comps.setHour_(hour)
        comps.setMinute_(minute)
        reminder.setDueDateComponents_(comps)

        # setCompleted_ 会顺带写好 completionDate
        if bool(reminder.isCompleted()) != task.completed:
            reminder.setCompleted_(task.completed)

        if is_new and APPLE_ADD_ALARM and not task.completed:
            from EventKit import EKAlarm
            from Foundation import NSDate
            due = datetime.datetime(year, month, day, hour, minute)
            reminder.addAlarm_(EKAlarm.alarmWithAbsoluteDate_(
                NSDate.dateWithTimeIntervalSince1970_(due.timestamp())))


# --------------------------------------------------------------------------
# 双向同步引擎
# --------------------------------------------------------------------------

class ReminderSync:
    def __init__(self, zectrix: ZectrixClient, apple: AppleReminders,
                 state: SyncState, dry_run: bool = False):
        self.zectrix = zectrix
        self.apple = apple
        self.state = state
        self.dry_run = dry_run
        self.stats = {"created_z": 0, "created_a": 0, "updated_z": 0, "updated_a": 0,
                      "deleted_z": 0, "deleted_a": 0, "conflicts": 0, "unlinked": 0}
        self.summary = "提醒事项：未运行"
        self.visible_lists: set = set()

    # -- 辅助 -------------------------------------------------------------

    @staticmethod
    def in_window(due_date: str) -> bool:
        try:
            date = datetime.datetime.strptime(due_date, "%Y-%m-%d").date()
        except ValueError:
            return False
        today = datetime.date.today()
        return (today - datetime.timedelta(days=SYNC_DAYS_BACK)
                <= date <=
                today + datetime.timedelta(days=SYNC_DAYS_AHEAD))

    @staticmethod
    def description_for(rem: ARem) -> str:
        """写进 Zectrix 备注的配对锚点。

        优先用 calendarItemExternalIdentifier：calendarItemIdentifier 是每台 Mac
        本地数据库自己的编号，同一条 iCloud 提醒在另一台 Mac 上编号不一样，
        写它的话换台电脑就认不出来、会建出重复条目。
        """
        return f"SOURCE: apple\nUID: reminder:{rem.ext_id or rem.apple_id}"

    @staticmethod
    def _uid_from_description(description: str) -> str:
        for line in (description or "").split("\n"):
            line = line.strip()
            if line.startswith("UID:"):
                return line[4:].strip()
        return ""

    def _migrate_from_descriptions(self, z_todos: List[ZTodo], a_by_id: Dict[str, ARem],
                                   a_by_ext: Dict[str, ARem]) -> None:
        """状态文件没有这条配对时，靠 Zectrix 备注里的 UID 把两边重新认回去。

        用在两种场景：状态文件丢了，或者换了另一台 Mac（那台的状态文件是空的）。
        备注里的 UID 可能是跨设备的 external id，也可能是老版本写的本地 id，两个都试。
        """
        known = self.state.zectrix_ids()
        recovered = 0
        for todo in z_todos:
            if todo.todo_id in known:
                continue
            uid = self._uid_from_description(todo.description)
            if not uid.startswith("reminder:"):
                continue
            anchor = uid[len("reminder:"):]
            if anchor in self.state.links:
                continue
            rem = (a_by_ext.get(anchor) or a_by_id.get(anchor)
                   or self.apple.lookup(anchor, anchor))
            if rem is None or rem.apple_id in self.state.links:
                continue
            # 用当前 Zectrix 内容当快照：无法判断谁改过，先以 Apple 为准把两边对齐
            self.state.link(rem.apple_id, rem.ext_id, todo.todo_id, todo.task, rem.list_name)
            recovered += 1
        if recovered:
            log(f"从待办备注里认回了 {recovered} 条已有配对")

    # -- 主流程 -----------------------------------------------------------

    def run(self, z_todos: List[ZTodo]) -> None:
        reminders = self.apple.fetch_all()
        a_by_id = {r.apple_id: r for r in reminders}
        a_by_ext = {r.ext_id: r for r in reminders if r.ext_id}
        z_by_id = {t.todo_id: t for t in z_todos}

        # 删除判断和日历同理：只有当这条提醒原本所在的列表「这次确实读到了」，
        # 它的消失才算真的被删。配对时记下了列表名，这里核对一遍。
        # 不能用「读到 0 条」判断 —— 那台可能读到了别的列表、结果非空，照样误删。
        self.visible_lists = set(self.apple.list_names())

        self.state.prune_tombstones()
        self._migrate_from_descriptions(z_todos, a_by_id, a_by_ext)

        # 1. 处理已配对的
        #    这一轮碰过的 id 全部记下来：配对处理里可能刚刚删掉了某一边，
        #    后面两步不能再把它当成「新任务」又建回去。
        handled_apple = set()
        handled_ext = set()
        handled_zectrix = set()

        for apple_id in list(self.state.links.keys()):
            link = self.state.links[apple_id]
            zectrix_id = link["zectrixId"]
            snapshot = Task.from_dict(link.get("snapshot", {}))

            rem = a_by_id.get(apple_id) or a_by_ext.get(link.get("extId", ""))
            if rem is None:
                rem = self.apple.lookup(apple_id, link.get("extId", ""))
            todo = z_by_id.get(zectrix_id)

            # Apple 那边标识符变了（iCloud 首次同步会改），顺手修正
            if rem is not None and rem.apple_id != apple_id:
                self.state.unlink(apple_id)
                self.state.link(rem.apple_id, rem.ext_id, zectrix_id, snapshot, rem.list_name)
                apple_id = rem.apple_id

            handled_apple.add(apple_id)
            handled_zectrix.add(zectrix_id)
            for ext in (link.get("extId"), rem.ext_id if rem else None):
                if ext:
                    handled_ext.add(ext)

            self._sync_pair(apple_id, zectrix_id, snapshot, todo, rem)

        # 2. Apple 有、Zectrix 没有 —— 新建到 Zectrix
        linked_apple = set(self.state.links.keys()) | handled_apple
        linked_ext = {l.get("extId") for l in self.state.links.values()} | handled_ext
        out_of_window = already_done = 0
        for rem in reminders:
            if rem.apple_id in linked_apple or (rem.ext_id and rem.ext_id in linked_ext):
                continue
            if rem.apple_id in self.state.tomb_apple:
                continue
            if not self.in_window(rem.task.due_date):
                out_of_window += 1
                continue
            if rem.task.completed and not SYNC_NEW_COMPLETED:
                already_done += 1
                continue
            log(f"  新提醒 -> 墨水屏: {rem.task.describe()}")
            description = self.description_for(rem)
            todo_id = self.zectrix.create(rem.task, description)
            if todo_id:
                self.state.link(rem.apple_id, rem.ext_id, todo_id, rem.task, rem.list_name)
                self.stats["created_z"] += 1
            elif self.dry_run:
                self.stats["created_z"] += 1

        if out_of_window or already_done:
            today = datetime.date.today()
            window = (f"{today - datetime.timedelta(days=SYNC_DAYS_BACK)} ~ "
                      f"{today + datetime.timedelta(days=SYNC_DAYS_AHEAD)}")
            if out_of_window:
                log(f"  跳过 {out_of_window} 条提醒：不在同步范围 {window} 内"
                    f"（想扩大就调 SYNC_DAYS_AHEAD / SYNC_DAYS_BACK）")
            if already_done:
                log(f"  跳过 {already_done} 条提醒：已完成且还没配过对"
                    f"（想同步的话设 SYNC_NEW_COMPLETED=1）")

        # 3. Zectrix 有、Apple 没有 —— 新建到提醒事项
        linked_zectrix = set(self.state.zectrix_ids().keys()) | handled_zectrix
        for todo in z_todos:
            if todo.todo_id in linked_zectrix:
                continue
            if str(todo.todo_id) in self.state.tomb_zectrix:
                continue
            if todo.task.title.startswith(CALENDAR_PREFIX):
                continue   # 日历事件不往提醒事项里塞
            if not todo.task.due_date or not self.in_window(todo.task.due_date):
                continue
            if todo.task.completed and not SYNC_NEW_COMPLETED:
                continue
            log(f"  新待办 -> 提醒事项: {todo.task.describe()}")
            rem = self.apple.create(todo.task)
            if rem is not None:
                self.state.link(rem.apple_id, rem.ext_id, todo.todo_id, todo.task, rem.list_name)
                # 把 Apple 锚点回写到备注：状态文件丢了、或换台电脑跑，都能靠它认回来
                self.zectrix.update_fields(todo.todo_id, todo.task,
                                           self.description_for(rem))
                self.stats["created_a"] += 1
            elif self.dry_run:
                self.stats["created_a"] += 1

        self._report()

    # -- 单个配对 ---------------------------------------------------------

    def _sync_pair(self, apple_id: str, zectrix_id: int, snapshot: Task,
                   todo: Optional[ZTodo], rem: Optional[ARem]) -> None:
        if todo is None and rem is None:
            self.state.unlink(apple_id)
            self.state.tomb_apple.pop(apple_id, None)
            return

        if rem is None:                      # 提醒事项被删了
            self._handle_apple_deleted(apple_id, zectrix_id, todo)
            return

        if todo is None:                     # 墨水屏上的待办被删了
            self._handle_zectrix_deleted(apple_id, rem)
            return

        z_changed = todo.task != snapshot
        a_changed = rem.task != snapshot

        if not z_changed and not a_changed:
            return

        if todo.task == rem.task:            # 两边改成了一样的
            self.state.link(apple_id, rem.ext_id, zectrix_id, rem.task, rem.list_name)
            return

        if z_changed and a_changed:
            self.stats["conflicts"] += 1
            winner = "apple" if rem.updated_at >= todo.updated_at else "zectrix"
            log(f"  ⚠️  两边都改过，以{'提醒事项' if winner == 'apple' else '墨水屏'}为准: "
                f"{snapshot.title}")
            log(f"      提醒事项: {rem.task.describe()}  ({self._ts(rem.updated_at)})")
            log(f"      墨水屏  : {todo.task.describe()}  ({self._ts(todo.updated_at)})")
        else:
            winner = "zectrix" if z_changed else "apple"

        if winner == "apple":
            log(f"  提醒事项 -> 墨水屏: {todo.task.describe()} => {rem.task.describe()}")
            if self._push_to_zectrix(todo, rem.task, rem):
                self.state.link(apple_id, rem.ext_id, zectrix_id, rem.task, rem.list_name)
                self.stats["updated_z"] += 1
        else:
            log(f"  墨水屏 -> 提醒事项: {rem.task.describe()} => {todo.task.describe()}")
            if self.apple.update(rem, todo.task):
                self.state.link(apple_id, rem.ext_id, zectrix_id, todo.task, rem.list_name)
                self.stats["updated_a"] += 1

    def _push_to_zectrix(self, todo: ZTodo, task: Task, rem: ARem) -> bool:
        description = self.description_for(rem)
        ok = True
        if (task.title, task.due_date, task.due_time) != \
           (todo.task.title, todo.task.due_date, todo.task.due_time) or \
           not todo.description:
            ok = self.zectrix.update_fields(todo.todo_id, task, description)
        if ok and task.completed != todo.task.completed:
            ok = self.zectrix.set_completed(todo.todo_id, task, description, task.completed)
        return ok

    def _handle_apple_deleted(self, apple_id: str, zectrix_id: int,
                              todo: Optional[ZTodo]) -> None:
        if todo is None:
            self.state.unlink(apple_id)
            return
        source_list = self.state.links.get(apple_id, {}).get("listName", "")
        if source_list and source_list not in self.visible_lists:
            log(f"  ⚠️  保留「{todo.task.title}」：这台电脑读不到它所在的提醒列表"
                f"（{source_list}）—— 可能是本地列表或 iCloud 还没同步")
            return
        if DELETE_POLICY in ("apple-master", "mirror"):
            log(f"  提醒事项已删除 -> 墨水屏同步删除: {todo.task.title}")
            if self.zectrix.delete(zectrix_id):
                self.state.unlink(apple_id)
                self.stats["deleted_z"] += 1
        else:
            log(f"  提醒事项已删除，按 DELETE_POLICY=none 只解绑: {todo.task.title}")
            self.state.unlink(apple_id)
            self.state.tombstone_zectrix(zectrix_id)
            self.stats["unlinked"] += 1

    def _handle_zectrix_deleted(self, apple_id: str, rem: ARem) -> None:
        if DELETE_POLICY == "mirror":
            log(f"  墨水屏待办已删除 -> 提醒事项同步删除: {rem.task.title}")
            if self.apple.delete(rem):
                self.state.unlink(apple_id)
                self.stats["deleted_a"] += 1
        else:
            log(f"  墨水屏待办已删除，保留提醒事项并解绑: {rem.task.title}")
            self.state.unlink(apple_id)
            self.state.tombstone_apple(apple_id)
            self.stats["unlinked"] += 1

    @staticmethod
    def _ts(value: float) -> str:
        if not value:
            return "时间未知"
        return datetime.datetime.fromtimestamp(value).strftime("%m-%d %H:%M:%S")

    def _report(self) -> None:
        s = self.stats
        log(f"\n提醒事项同步结果: "
            f"墨水屏 新建 {s['created_z']} / 更新 {s['updated_z']} / 删除 {s['deleted_z']}；"
            f"提醒事项 新建 {s['created_a']} / 更新 {s['updated_a']} / 删除 {s['deleted_a']}；"
            f"冲突 {s['conflicts']}，解绑 {s['unlinked']}")

        changes = sum(s[k] for k in ("created_z", "created_a", "updated_z",
                                     "updated_a", "deleted_z", "deleted_a"))
        if changes == 0:
            self.summary = "提醒事项：无变化"
        else:
            parts = []
            if s["created_z"] or s["created_a"]:
                parts.append(f"新建 {s['created_z'] + s['created_a']}")
            if s["updated_z"] or s["updated_a"]:
                parts.append(f"更新 {s['updated_z'] + s['updated_a']}")
            if s["deleted_z"] or s["deleted_a"]:
                parts.append(f"删除 {s['deleted_z'] + s['deleted_a']}")
            if s["conflicts"]:
                parts.append(f"冲突 {s['conflicts']}")
            self.summary = "提醒事项：" + " · ".join(parts)


# --------------------------------------------------------------------------
# CalDAV 日历事件 -> Zectrix（单向，保持原有行为）
# --------------------------------------------------------------------------

class AppleCalendar:
    """读 macOS 日历里的事件 —— iCloud / Google(umd.edu) / Exchange 都在这儿。

    只要账号加进了「系统设置 → 互联网账户」，事件就会出现在这里，
    不需要密码、OAuth 或者 ics 密钥地址。日历是只读的：会议不是待办，
    不存在「勾掉」，所以这条链路是单向的。
    """

    def __init__(self):
        self.store = None
        self._calendars = []

    def connect(self) -> None:
        from EventKit import EKEventStore, EKEntityTypeEvent

        self.store = EKEventStore.alloc().init()
        request_eventkit_access(self.store, "events")

        all_cals = list(self.store.calendarsForEntityType_(EKEntityTypeEvent) or [])
        if CALENDAR_LISTS:
            wanted = {n.strip() for n in CALENDAR_LISTS.split(",") if n.strip()}
            self._calendars = [c for c in all_cals if str(c.title()) in wanted]
            missing = wanted - {str(c.title()) for c in all_cals}
            if missing:
                log(f"    ⚠️  CALENDAR_LISTS 里这些日历不存在: {', '.join(sorted(missing))}")
                log(f"        可用的有: {', '.join(sorted(str(c.title()) for c in all_cals))}")
        else:
            self._calendars = all_cals

        if not self._calendars:
            raise AppleRemindersError("没有可用的日历")
        log(f"    macOS 日历: {', '.join(self.describe_calendars())}")

    def describe_calendars(self) -> List[str]:
        out = []
        for cal in self._calendars:
            source = cal.source()
            account = str(source.title()) if source else "?"
            out.append(f"{str(cal.title())}（{account}）")
        return out

    def fetch_events(self, start: datetime.datetime,
                     end: datetime.datetime) -> List[Dict]:
        from EventKit import EKEventStatusCanceled
        from EventKit import EKParticipantStatusDeclined
        from Foundation import NSDate

        predicate = self.store.predicateForEventsWithStartDate_endDate_calendars_(
            NSDate.dateWithTimeIntervalSince1970_(start.timestamp()),
            NSDate.dateWithTimeIntervalSince1970_(end.timestamp()),
            self._calendars)
        # 这个是同步的，不像提醒事项要走回调
        found = list(self.store.eventsMatchingPredicate_(predicate) or [])

        events = []
        for item in found:
            title = str(item.title() or "").strip()
            if not title:
                continue

            if item.status() == EKEventStatusCanceled:
                log(f"    跳过已取消: {title}")
                continue

            # 自己已经拒绝的会议不用上墨水屏
            attendee = item.attendees() and next(
                (a for a in item.attendees() if a.isCurrentUser()), None)
            if attendee is not None and attendee.participantStatus() == EKParticipantStatusDeclined:
                log(f"    跳过已拒绝: {title}")
                continue

            lowered = title.lower()
            if "已取消" in title or "cancelled" in lowered or "canceled" in lowered:
                continue

            start_date = item.startDate()
            if start_date is None:
                continue
            dt = datetime.datetime.fromtimestamp(start_date.timeIntervalSince1970())
            # 谓词返回的是「与窗口有重叠」的事件，跨天的长事件开始时间可能在窗口之前
            if dt < start or dt > end:
                continue
            due_time = "09:00" if item.isAllDay() else dt.strftime("%H:%M")

            # 重复事件的每一次展开共用同一个 external id，得带上日期才唯一
            uid = str(item.calendarItemExternalIdentifier() or
                      item.calendarItemIdentifier() or "")
            if not uid:
                continue

            events.append({
                "uid": f"{uid}@{dt:%Y-%m-%d}",
                "title": title,
                "dueDate": dt.strftime("%Y-%m-%d"),
                "dueTime": due_time,
                "calendar": str(item.calendar().title()) if item.calendar() else "",
            })
        return events

    def calendar_names(self) -> List[str]:
        return [str(c.title()) for c in self._calendars]


class CalendarSync:
    def __init__(self, zectrix: ZectrixClient, dry_run: bool = False):
        self.zectrix = zectrix
        self.dry_run = dry_run
        self.summary = "日历：未运行"

    def run(self, z_todos: List[ZTodo]) -> None:
        calendar_todos = [t for t in z_todos if t.task.title.startswith(CALENDAR_PREFIX)]

        log("\n  [1/2] 划掉已过期的日历日程...")
        self._complete_expired([t for t in calendar_todos if not t.task.completed])

        log("\n  [2/2] 拉取日历事件...")
        collected = self._collect_events()
        if collected is None:
            log("  所有日历来源都拉取失败，跳过本次日历同步（不做删除，避免误删）")
            self.summary = "日历：拉取失败"
            return
        events, visible = collected
        self._sync(events, calendar_todos, visible)

    def _collect_events(self) -> Optional[Tuple[List[Dict], set]]:
        """按 CALENDAR_SOURCE 从各来源取事件，按 UID 去重。

        除了事件本身，还要返回「这次真正读到了哪些日历」。删除判断靠它：
        只有当某条日程所属的日历这次确实读到了，它的消失才算「日历里删了」。
        另一台没配 UMD 账号的电脑读不到那个日历，就不会去删它的日程 ——
        用「事件数是不是 0」判断是不行的，那台可能读到了别的日历、结果非空，
        照样会误删。

        任何一个来源挂了都只是少一部分事件；只有全挂了才返回 None。
        """
        sources_ok = 0
        merged: Dict[str, Dict] = {}
        visible: set = set()

        if CALENDAR_SOURCE in ("eventkit", "both"):
            log("    来源: macOS 日历 (EventKit)")
            try:
                events, names = self._fetch_from_eventkit()
                for event in events:
                    merged.setdefault(event["uid"], event)
                visible.update(names)
                log(f"    macOS 日历取到 {len(events)} 个事件")
                sources_ok += 1
            except ImportError:
                log("    ⚠️  缺少 pyobjc，跳过 macOS 日历")
            except AppleRemindersError as exc:
                log(f"    ⚠️  {exc}")

        if CALENDAR_SOURCE in ("caldav", "both"):
            if not CALDAV_PASS:
                log("    未配置 CALDAV_PASS，跳过 CalDAV")
            else:
                log("    来源: CalDAV")
                fetched = self._fetch_from_caldav()
                if fetched is None:
                    log("    ⚠️  CalDAV 拉取失败")
                else:
                    events, names = fetched
                    added = 0
                    for event in events:
                        if event["uid"] not in merged:
                            merged[event["uid"]] = event
                            added += 1
                    visible.update(names)
                    log(f"    CalDAV 取到 {len(events)} 个事件（去重后新增 {added}）")
                    sources_ok += 1

        if sources_ok == 0:
            return None
        log(f"    合计 {len(merged)} 个日程，读到的日历: "
            f"{', '.join(sorted(visible)) if visible else '（无）'}")
        return list(merged.values()), visible

    def _fetch_from_eventkit(self) -> Tuple[List[Dict], List[str]]:
        calendar = AppleCalendar()
        calendar.connect()
        start, end = self.event_window()
        return calendar.fetch_events(start, end), calendar.calendar_names()

    @staticmethod
    def event_window() -> Tuple[datetime.datetime, datetime.datetime]:
        """取事件的时间范围。

        起点是「今天 00:00」而不是「现在」：已经开始的会议必须留在结果里，
        否则匹配不上现有待办，会被当成「日历里删掉了」而删除 —— 结果就是
        会议一开始就从墨水屏上消失，EXPIRE_HOURS 那个自动划掉也永远轮不到。
        留着它们，_complete_expired 才有机会在过期后把它们划掉。
        """
        now = datetime.datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return (midnight - datetime.timedelta(days=SYNC_DAYS_BACK),
                midnight + datetime.timedelta(days=1 + SYNC_DAYS_AHEAD))

    def _complete_expired(self, todos: List[ZTodo]) -> None:
        count = 0
        now = datetime.datetime.now()
        for todo in todos:
            try:
                due = datetime.datetime.strptime(
                    f"{todo.task.due_date} {todo.task.due_time}", "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            if (now - due).total_seconds() >= EXPIRE_HOURS * 3600:
                log(f"    过期日程: {todo.task.title} {todo.task.due_date} {todo.task.due_time}")
                if self.zectrix.set_completed(todo.todo_id, todo.task,
                                              todo.description, True):
                    count += 1
        log(f"    共划掉 {count} 个过期日程")

    def _fetch_from_caldav(self) -> Optional[Tuple[List[Dict], List[str]]]:
        def _fetch():
            import caldav

            client = caldav.DAVClient(url=CALDAV_URL, username=CALDAV_USER,
                                      password=CALDAV_PASS)
            calendars = client.principal().calendars()
            if not calendars:
                log("    未找到任何日历")
                return [], []

            start, end = self.event_window()
            start = start.astimezone()
            end = end.astimezone()

            events: List[Dict] = []
            names: List[str] = []
            for calendar in calendars:
                try:
                    name = str(calendar.get_display_name() or calendar.url)
                except Exception:
                    name = str(calendar.url)
                names.append(name)
                found = calendar.search(start=start, end=end, event=True, expand=True)
                for item in found:
                    for event in self._parse(item, start, end):
                        event["calendar"] = name
                        events.append(event)
            return events, names

        return retry(_fetch)

    @staticmethod
    def _parse(item, start: datetime.datetime, end: datetime.datetime) -> List[Dict]:
        out = []
        try:
            cal = Calendar.from_ical(item.data)
        except Exception as exc:
            log(f"    解析日历事件失败: {exc}")
            return out

        for component in cal.walk():
            if component.name != "VEVENT":
                continue
            summary = str(component.get("SUMMARY", "")).strip()
            dtstart = component.get("DTSTART")
            uid = str(component.get("UID", ""))
            if not summary or not dtstart or not uid:
                continue

            lowered = summary.lower()
            if "已取消" in summary or "cancelled" in lowered or "canceled" in lowered:
                continue

            dt = dtstart.dt
            if isinstance(dt, datetime.datetime):
                dt = dt.astimezone()
                due_time = dt.strftime("%H:%M")
            elif isinstance(dt, datetime.date):
                dt = datetime.datetime.combine(dt, datetime.time(9, 0)).astimezone()
                due_time = "09:00"
            else:
                continue

            # 已经开始的会议照样留着（见 event_window 的说明），只按窗口过滤
            if dt < start or dt > end:
                continue

            out.append({
                # 和 EventKit 那边用同一种 key：重复日程的每一次展开共用一个 UID，
                # 不带日期的话同一个系列只会剩一条，两个来源之间也对不上、去不了重
                "uid": f"{uid}@{dt:%Y-%m-%d}",
                "title": summary,
                "dueDate": dt.strftime("%Y-%m-%d"),
                "dueTime": due_time,
            })
        return out

    @staticmethod
    def _field(description: str, prefix: str) -> str:
        for line in (description or "").split("\n"):
            line = line.strip()
            if line.startswith(prefix):
                return line[len(prefix):].strip()
        return ""

    def _sync(self, events: List[Dict], calendar_todos: List[ZTodo],
              visible: Optional[set] = None) -> None:
        remaining: Dict[str, ZTodo] = {}
        for todo in calendar_todos:
            stored = ReminderSync._uid_from_description(todo.description)
            if stored:
                remaining[stored] = todo

        created = updated = deleted = 0
        matched = set()

        for event in events:
            uid = event["uid"]
            task = Task(title=f"{CALENDAR_PREFIX} {event['title']}",
                        due_date=event["dueDate"],
                        due_time=event["dueTime"],
                        completed=False)
            # CAL 记住来自哪个日历：换台电脑看不到这个日历时，就不会误删它
            description = f"SOURCE: calendar\nUID: {uid}"
            if event.get("calendar"):
                description += f"\nCAL: {event['calendar']}"

            # 老版本备注里存的是不带日期的裸 UID，也认，
            # 免得升级之后所有日程被当成「日历里没了」删掉再重建一遍
            existing = None
            for key in (uid, uid.split("@")[0]):
                if key in remaining and key not in matched:
                    existing = remaining[key]
                    matched.add(key)
                    break

            if existing is None:
                if self.zectrix.create(task, description, priority=1):
                    created += 1
                elif self.dry_run:
                    created += 1
            elif (existing.task.title, existing.task.due_date, existing.task.due_time) != \
                 (task.title, task.due_date, task.due_time) or \
                 existing.description != description:
                if self.zectrix.update_fields(existing.todo_id, task, description):
                    updated += 1

        # 删除判断：只有当这条日程所属的日历「这次确实读到了」，
        # 它的消失才算真的被删。看不到那个日历就不动它 —— 那是另一台电脑
        # 同步上来的内容，不是这台有权处置的。
        # 注意不能用「事件数是不是 0」来判断：今天一个会都没有是完全正常的，
        # 而另一台读到了别的日历、结果非空，照样会误删。
        unmatched = [t for k, t in remaining.items()
                     if k not in matched and not t.task.completed]
        blocked = []
        for todo in unmatched:
            source_cal = self._field(todo.description, "CAL:")
            # 没有 CAL 的是老数据，这次读到了任何日历就按老规矩处理
            if source_cal and visible is not None and source_cal not in visible:
                blocked.append((todo, source_cal))
                continue
            log(f"    日历里已删除，同步删除: {todo.task.title}")
            if self.zectrix.delete(todo.todo_id):
                deleted += 1

        if blocked:
            cals = sorted({c for _, c in blocked})
            log(f"    ⚠️  保留 {len(blocked)} 条日程：这台电脑读不到它们所在的日历"
                f"（{', '.join(cals)}）")
            log("        如果确实想在这台管它们，去「系统设置 → 互联网账户」"
                "把对应账号加上，或检查 CALENDAR_LISTS。")

        log(f"\n日历同步结果: 新建 {created}，更新 {updated}，删除 {deleted}")

        if created or updated or deleted:
            self.summary = f"日历：新建 {created} · 更新 {updated} · 删除 {deleted}"
        else:
            self.summary = "日历：无变化"


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def list_calendars() -> int:
    """把能看到的提醒事项列表和日历都打出来，方便填 APPLE_LISTS / CALENDAR_LISTS。"""
    try:
        from EventKit import EKEntityTypeEvent, EKEntityTypeReminder, EKEventStore
    except ImportError:
        log("缺少 pyobjc: pip install pyobjc-framework-EventKit")
        return 1

    def dump(entity: str, entity_type, header: str) -> None:
        log(f"\n{header}")
        store = EKEventStore.alloc().init()
        try:
            request_eventkit_access(store, entity)
        except AppleRemindersError as exc:
            log(f"  ⚠️  {exc}")
            return
        for cal in store.calendarsForEntityType_(entity_type) or []:
            source = cal.source()
            log(f"  · {str(cal.title())}"
                f"    （账户: {str(source.title()) if source else '?'}）")

    dump("reminders", EKEntityTypeReminder, "提醒事项列表  → 填进 APPLE_LISTS / APPLE_LIST")
    dump("events", EKEntityTypeEvent, "日历  → 填进 CALENDAR_LISTS")
    log("\n名字直接抄上面的，多个用英文逗号分隔；留空表示全部。")
    log("看不到工作日历的话，先到「系统设置 → 互联网账户」把那个账号加进来。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Zectrix 墨水屏 <-> Apple 提醒事项 双向同步")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将要执行的操作，不做任何写入")
    parser.add_argument("--no-reminders", action="store_true",
                        help="跳过 Apple 提醒事项双向同步")
    parser.add_argument("--no-calendar", action="store_true",
                        help="跳过 CalDAV 日历事件同步")
    parser.add_argument("--reset-state", action="store_true",
                        help="清空配对状态后重新建立（不会删除任何任务）")
    parser.add_argument("--list-calendars", action="store_true",
                        help="列出能看到的提醒事项列表和日历，然后退出")
    args = parser.parse_args()

    if args.list_calendars:
        return list_calendars()

    if not API_KEY or not DEVICE_ID:
        log("请先配置 API_KEY 和 DEVICE_ID（环境变量或 .env）")
        return 1
    if DELETE_POLICY not in ("apple-master", "mirror", "none"):
        log(f"DELETE_POLICY 只能是 apple-master / mirror / none，当前: {DELETE_POLICY}")
        return 1

    log("=" * 60)
    log(f"开始同步  {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    if args.dry_run:
        log("***** DRY RUN 模式：不会执行任何写入操作 *****")
    today = datetime.date.today()
    log(f"同步范围: {today - datetime.timedelta(days=SYNC_DAYS_BACK)} ~ "
        f"{today + datetime.timedelta(days=SYNC_DAYS_AHEAD)}   删除策略: {DELETE_POLICY}")
    log("=" * 60)

    if args.reset_state and not args.dry_run and os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
        log(f"已清空状态文件: {STATE_FILE}")

    zectrix = ZectrixClient(dry_run=args.dry_run)
    z_todos = zectrix.list_todos()
    if z_todos is None:
        log("\n无法读取 Zectrix 待办，本次同步中止（不做任何写入，避免误删）")
        log("SUMMARY: 连不上 Zectrix，本次没同步")
        return 1

    exit_code = 0
    summaries = []

    if not args.no_reminders:
        log("\n" + "-" * 60)
        log("Apple 提醒事项 <-> 墨水屏 双向同步")
        log("-" * 60)
        state = SyncState(STATE_FILE)
        apple = AppleReminders(dry_run=args.dry_run)
        engine = ReminderSync(zectrix, apple, state, dry_run=args.dry_run)
        try:
            apple.connect()
            engine.run(z_todos)
            summaries.append(engine.summary)
        except ImportError:
            log("⚠️  缺少 pyobjc，跳过提醒事项同步：pip install pyobjc-framework-EventKit")
            summaries.append("提醒事项：缺少 pyobjc")
            exit_code = 1
        except AppleRemindersError as exc:
            log(f"⚠️  {exc}")
            summaries.append("提醒事项：没有访问权限")
            exit_code = 1
        finally:
            state.save(args.dry_run)

        # 提醒事项这边可能改过待办，日历同步前重新拉一次
        if not args.no_calendar and not args.dry_run:
            refreshed = zectrix.list_todos()
            if refreshed is not None:
                z_todos = refreshed

    if not args.no_calendar and CALENDAR_SOURCE != "none":
        log("\n" + "-" * 60)
        log("日历事件 -> 墨水屏（单向）")
        log("-" * 60)
        calendar = CalendarSync(zectrix, dry_run=args.dry_run)
        calendar.run(z_todos)
        summaries.append(calendar.summary)

    log("\n" + "=" * 60)
    log("同步结束")
    log("=" * 60)
    # 桌面按钮读这一行做通知，格式别乱改
    log("SUMMARY: " + ("；".join(summaries) if summaries else "没有启用任何同步"))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
