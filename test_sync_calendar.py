#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync_calendar 单元测试。

运行: .venv/bin/python -m pytest test_sync_calendar.py -v

EventKit 需要 macOS 授权，没法在测试里真的读写提醒事项，
所以这里用 FakeApple / FakeZectrix 替身把双向同步引擎的每条分支都跑一遍。
"""
import datetime
import json
import os

import pytest

import sync_calendar as sc
from sync_calendar import ARem, ReminderSync, SyncState, Task, ZTodo


TODAY = datetime.date.today().strftime("%Y-%m-%d")
YESTERDAY = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
TOMORROW = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")


def task(title="喝水", date=None, time="10:00", completed=False) -> Task:
    return Task(title=title, due_date=date or TODAY, due_time=time, completed=completed)


# --------------------------------------------------------------------------
# 替身
# --------------------------------------------------------------------------

class FakeZectrix:
    """在内存里模拟 Zectrix 云端。"""

    def __init__(self, todos=None, dry_run=False):
        self.dry_run = dry_run
        self.todos = {t.todo_id: t for t in (todos or [])}
        self._next_id = max(self.todos, default=1000) + 1
        self.calls = []

    def create(self, task, description, priority=0):
        self.calls.append(("create", task, description))
        if self.dry_run:
            return None
        todo_id = self._next_id
        self._next_id += 1
        self.todos[todo_id] = ZTodo(todo_id=todo_id, task=task,
                                    description=description, updated_at=0)
        return todo_id

    def update_fields(self, todo_id, task, description):
        self.calls.append(("update", todo_id, task, description))
        old = self.todos[todo_id]
        merged = Task(task.title, task.due_date, task.due_time, old.task.completed)
        self.todos[todo_id] = ZTodo(todo_id, merged, description, old.updated_at)
        return True

    def set_completed(self, todo_id, task, description, completed):
        self.calls.append(("set_completed", todo_id, completed))
        old = self.todos[todo_id]
        merged = Task(old.task.title, old.task.due_date, old.task.due_time, completed)
        self.todos[todo_id] = ZTodo(todo_id, merged, old.description, old.updated_at)
        return True

    def delete(self, todo_id):
        self.calls.append(("delete", todo_id))
        self.todos.pop(todo_id, None)
        return True

    def kinds(self):
        return [c[0] for c in self.calls]


class FakeApple:
    """在内存里模拟 Apple 提醒事项。"""

    def __init__(self, reminders=None, dry_run=False, lists=None):
        self.dry_run = dry_run
        self.items = {r.apple_id: r for r in (reminders or [])}
        self._next = 1
        self.calls = []
        # 这台机器「读到了哪些列表」——删除判断靠它，和列表里有没有内容无关
        self._lists = lists

    def list_names(self):
        if self._lists is not None:
            return list(self._lists)
        names = {r.list_name for r in self.items.values() if r.list_name}
        return sorted(names) or ["提醒事项"]

    def fetch_all(self):
        return list(self.items.values())

    def lookup(self, apple_id, ext_id=""):
        if apple_id in self.items:
            return self.items[apple_id]
        for rem in self.items.values():
            if ext_id and rem.ext_id == ext_id:
                return rem
        return None

    def create(self, task):
        self.calls.append(("create", task))
        if self.dry_run:
            return None
        apple_id = f"new-{self._next}"
        self._next += 1
        rem = ARem(apple_id=apple_id, ext_id=f"ext-{apple_id}", task=task,
                   updated_at=0, list_name="提醒事项")
        self.items[apple_id] = rem
        return rem

    def update(self, rem, task):
        self.calls.append(("update", rem.apple_id, task))
        self.items[rem.apple_id] = ARem(rem.apple_id, rem.ext_id, task,
                                        rem.updated_at, rem.list_name)
        return True

    def delete(self, rem):
        self.calls.append(("delete", rem.apple_id))
        self.items.pop(rem.apple_id, None)
        return True

    def kinds(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def state(tmp_path):
    return SyncState(str(tmp_path / "state.json"))


def build(state, z_todos=(), reminders=(), dry_run=False, lists=None):
    zectrix = FakeZectrix(z_todos, dry_run=dry_run)
    apple = FakeApple(reminders, dry_run=dry_run, lists=lists)
    engine = ReminderSync(zectrix, apple, state, dry_run=dry_run)
    return zectrix, apple, engine


@pytest.fixture(autouse=True)
def default_policy(monkeypatch):
    monkeypatch.setattr(sc, "DELETE_POLICY", "apple-master")
    monkeypatch.setattr(sc, "SYNC_DAYS_BACK", 0)
    monkeypatch.setattr(sc, "SYNC_DAYS_AHEAD", 0)
    monkeypatch.setattr(sc, "SYNC_NEW_COMPLETED", False)


# --------------------------------------------------------------------------
# Task / 时间归一化
# --------------------------------------------------------------------------

class TestTask:
    def test_roundtrip(self):
        t = task(completed=True)
        assert Task.from_dict(t.to_dict()) == t

    def test_equality_is_by_value(self):
        assert task() == task()
        assert task() != task(completed=True)
        assert task() != task(time="11:00")

    @pytest.mark.parametrize("raw, expected", [
        ("9:5", "09:05"),
        ("09:05:00", "09:05"),
        ("23:59", "23:59"),
        ("", sc.DEFAULT_DUE_TIME),
        ("垃圾", sc.DEFAULT_DUE_TIME),
    ])
    def test_normalize_time(self, raw, expected):
        assert sc._normalize_time(raw) == expected


class TestWindow:
    def test_today_in_window(self):
        assert ReminderSync.in_window(TODAY)

    def test_tomorrow_out_of_window_by_default(self):
        assert not ReminderSync.in_window(TOMORROW)

    def test_days_ahead_extends_window(self, monkeypatch):
        monkeypatch.setattr(sc, "SYNC_DAYS_AHEAD", 1)
        assert ReminderSync.in_window(TOMORROW)

    def test_bad_date(self):
        assert not ReminderSync.in_window("")


# --------------------------------------------------------------------------
# 新建
# --------------------------------------------------------------------------

class TestCreate:
    def test_new_reminder_creates_todo(self, state):
        rem = ARem("a1", "e1", task("买牛奶"), 100, "提醒事项")
        zectrix, apple, engine = build(state, reminders=[rem])
        engine.run([])

        assert len(zectrix.todos) == 1
        created = next(iter(zectrix.todos.values()))
        assert created.task == task("买牛奶")
        # 备注里存跨设备稳定的 external id，不是本机的 calendarItemIdentifier
        assert created.description == "SOURCE: apple\nUID: reminder:e1"
        assert state.links["a1"]["zectrixId"] == created.todo_id

    def test_new_todo_creates_reminder_and_writes_back_uid(self, state):
        todo = ZTodo(1, task("买菜"), "", 100)
        zectrix, apple, engine = build(state, z_todos=[todo])
        engine.run([todo])

        assert len(apple.items) == 1
        rem = next(iter(apple.items.values()))
        assert rem.task == task("买菜")
        # Apple 锚点要回写到 Zectrix 备注，状态文件丢了 / 换台电脑都能认回来
        assert zectrix.todos[1].description == f"SOURCE: apple\nUID: reminder:{rem.ext_id}"

    def test_calendar_prefixed_todo_never_goes_to_apple(self, state):
        todo = ZTodo(1, task(f"{sc.CALENDAR_PREFIX} 周会"), "SOURCE: caldav\nUID: x", 100)
        zectrix, apple, engine = build(state, z_todos=[todo])
        engine.run([todo])
        assert apple.items == {}

    def test_out_of_window_is_not_created(self, state):
        rem = ARem("a1", "e1", task("明天的事", date=TOMORROW), 100, "提醒事项")
        zectrix, apple, engine = build(state, reminders=[rem])
        engine.run([])
        assert zectrix.todos == {}

    def test_completed_new_item_skipped_by_default(self, state):
        rem = ARem("a1", "e1", task(completed=True), 100, "提醒事项")
        zectrix, apple, engine = build(state, reminders=[rem])
        engine.run([])
        assert zectrix.todos == {}

    def test_out_of_window_is_reported_not_silent(self, state, capsys):
        """回归：被过滤掉的提醒必须说出来。

        之前全是静默丢弃，日志只显示「新建 0」，用户看到「同步成功但什么都没发生」
        完全无从查起 —— 实际原因是窗口只有今天、提醒都不是今天到期。
        """
        rems = [ARem(f"a{i}", f"e{i}", task(f"任务{i}", date=TOMORROW), 100, "提醒事项")
                for i in range(3)]
        zectrix, apple, engine = build(state, reminders=rems)
        engine.run([])

        out = capsys.readouterr().out
        assert "跳过 3 条提醒" in out
        assert "不在同步范围" in out
        assert "SYNC_DAYS_AHEAD" in out          # 得告诉用户怎么改

    def test_skipped_completed_is_reported(self, state, capsys):
        rem = ARem("a1", "e1", task(completed=True), 100, "提醒事项")
        zectrix, apple, engine = build(state, reminders=[rem])
        engine.run([])

        out = capsys.readouterr().out
        assert "跳过 1 条提醒" in out
        assert "SYNC_NEW_COMPLETED" in out

    def test_nothing_skipped_stays_quiet(self, state, capsys):
        rem = ARem("a1", "e1", task("今天的事"), 100, "提醒事项")
        zectrix, apple, engine = build(state, reminders=[rem])
        engine.run([])
        assert "跳过" not in capsys.readouterr().out

    def test_completed_new_item_synced_when_enabled(self, state, monkeypatch):
        monkeypatch.setattr(sc, "SYNC_NEW_COMPLETED", True)
        rem = ARem("a1", "e1", task(completed=True), 100, "提醒事项")
        zectrix, apple, engine = build(state, reminders=[rem])
        engine.run([])
        assert len(zectrix.todos) == 1
        assert next(iter(zectrix.todos.values())).task.completed

    def test_no_duplicate_on_second_run(self, state):
        rem = ARem("a1", "e1", task("买牛奶"), 100, "提醒事项")
        zectrix, apple, engine = build(state, reminders=[rem])
        engine.run([])
        assert len(zectrix.todos) == 1

        # 第二次运行：内容没变，什么都不该发生
        zectrix2 = FakeZectrix(list(zectrix.todos.values()))
        engine2 = ReminderSync(zectrix2, apple, state)
        engine2.run(list(zectrix2.todos.values()))
        assert len(zectrix2.todos) == 1
        assert zectrix2.calls == []
        assert apple.calls == []


# --------------------------------------------------------------------------
# 完成状态（划掉）双向传播
# --------------------------------------------------------------------------

class TestCompletion:
    def _linked(self, state, snapshot=None):
        snapshot = snapshot or task()
        state.link("a1", "e1", 1, snapshot)
        return snapshot

    def test_completed_in_apple_crosses_off_zectrix(self, state):
        snap = self._linked(state)
        todo = ZTodo(1, snap, "SOURCE: apple\nUID: reminder:a1", 100)
        rem = ARem("a1", "e1", task(completed=True), 200, "提醒事项")
        zectrix, apple, engine = build(state, [todo], [rem])
        engine.run([todo])

        assert zectrix.todos[1].task.completed is True
        assert ("set_completed", 1, True) in zectrix.calls
        assert state.links["a1"]["snapshot"]["completed"] is True

    def test_completed_in_zectrix_crosses_off_apple(self, state):
        snap = self._linked(state)
        todo = ZTodo(1, task(completed=True), "SOURCE: apple\nUID: reminder:a1", 200)
        rem = ARem("a1", "e1", snap, 100, "提醒事项")
        zectrix, apple, engine = build(state, [todo], [rem])
        engine.run([todo])

        assert apple.items["a1"].task.completed is True
        assert apple.kinds() == ["update"]

    def test_uncomplete_propagates_from_apple(self, state):
        snap = self._linked(state, task(completed=True))
        todo = ZTodo(1, snap, "SOURCE: apple\nUID: reminder:a1", 100)
        rem = ARem("a1", "e1", task(completed=False), 200, "提醒事项")
        zectrix, apple, engine = build(state, [todo], [rem])
        engine.run([todo])

        assert zectrix.todos[1].task.completed is False
        assert ("set_completed", 1, False) in zectrix.calls

    def test_uncomplete_propagates_from_zectrix(self, state):
        snap = self._linked(state, task(completed=True))
        todo = ZTodo(1, task(completed=False), "SOURCE: apple\nUID: reminder:a1", 200)
        rem = ARem("a1", "e1", snap, 100, "提醒事项")
        zectrix, apple, engine = build(state, [todo], [rem])
        engine.run([todo])
        assert apple.items["a1"].task.completed is False

    def test_completion_survives_out_of_window(self, state, monkeypatch):
        """已配对的任务即使超出同步窗口，划掉也要继续同步。"""
        old = task("昨天的事", date=YESTERDAY)
        state.link("a1", "e1", 1, old)
        todo = ZTodo(1, old, "SOURCE: apple\nUID: reminder:a1", 100)
        rem = ARem("a1", "e1", Task("昨天的事", YESTERDAY, "10:00", True), 200, "提醒事项")
        zectrix, apple, engine = build(state, [todo], [rem])
        engine.run([todo])
        assert zectrix.todos[1].task.completed is True


# --------------------------------------------------------------------------
# 内容修改与冲突
# --------------------------------------------------------------------------

class TestUpdateAndConflict:
    def test_title_change_in_apple(self, state):
        snap = task("旧标题")
        state.link("a1", "e1", 1, snap)
        todo = ZTodo(1, snap, "SOURCE: apple\nUID: reminder:a1", 100)
        rem = ARem("a1", "e1", task("新标题"), 200, "提醒事项")
        zectrix, apple, engine = build(state, [todo], [rem])
        engine.run([todo])
        assert zectrix.todos[1].task.title == "新标题"

    def test_time_change_in_zectrix(self, state):
        snap = task(time="10:00")
        state.link("a1", "e1", 1, snap)
        todo = ZTodo(1, task(time="15:30"), "SOURCE: apple\nUID: reminder:a1", 200)
        rem = ARem("a1", "e1", snap, 100, "提醒事项")
        zectrix, apple, engine = build(state, [todo], [rem])
        engine.run([todo])
        assert apple.items["a1"].task.due_time == "15:30"

    def test_conflict_newer_apple_wins(self, state):
        snap = task("原始")
        state.link("a1", "e1", 1, snap)
        todo = ZTodo(1, task("墨水屏改的"), "SOURCE: apple\nUID: reminder:a1", 100)
        rem = ARem("a1", "e1", task("提醒事项改的"), 999, "提醒事项")
        zectrix, apple, engine = build(state, [todo], [rem])
        engine.run([todo])

        assert zectrix.todos[1].task.title == "提醒事项改的"
        assert engine.stats["conflicts"] == 1

    def test_conflict_newer_zectrix_wins(self, state):
        snap = task("原始")
        state.link("a1", "e1", 1, snap)
        todo = ZTodo(1, task("墨水屏改的"), "SOURCE: apple\nUID: reminder:a1", 999)
        rem = ARem("a1", "e1", task("提醒事项改的"), 100, "提醒事项")
        zectrix, apple, engine = build(state, [todo], [rem])
        engine.run([todo])

        assert apple.items["a1"].task.title == "墨水屏改的"
        assert engine.stats["conflicts"] == 1

    def test_both_sides_changed_identically_is_not_a_conflict(self, state):
        state.link("a1", "e1", 1, task("原始"))
        todo = ZTodo(1, task("一样的新标题"), "SOURCE: apple\nUID: reminder:a1", 100)
        rem = ARem("a1", "e1", task("一样的新标题"), 200, "提醒事项")
        zectrix, apple, engine = build(state, [todo], [rem])
        engine.run([todo])

        assert zectrix.calls == []
        assert apple.calls == []
        assert engine.stats["conflicts"] == 0
        assert state.links["a1"]["snapshot"]["title"] == "一样的新标题"

    def test_no_change_does_nothing(self, state):
        snap = task()
        state.link("a1", "e1", 1, snap)
        todo = ZTodo(1, snap, "SOURCE: apple\nUID: reminder:a1", 100)
        rem = ARem("a1", "e1", snap, 100, "提醒事项")
        zectrix, apple, engine = build(state, [todo], [rem])
        engine.run([todo])
        assert zectrix.calls == []
        assert apple.calls == []


# --------------------------------------------------------------------------
# 删除策略
# --------------------------------------------------------------------------

class TestDeletion:
    def test_apple_deleted_removes_zectrix_todo(self, state):
        snap = task()
        state.link("a1", "e1", 1, snap)
        todo = ZTodo(1, snap, "SOURCE: apple\nUID: reminder:a1", 100)
        # 留一条别的提醒：证明这次「读到了」，a1 是真的被删了，
        # 而不是整个列表都看不见（那种情况有专门的保护，见 TestEmptyReadGuard）
        other = ARem("a2", "e2", task("别的事", date=TOMORROW), 100, "提醒事项")
        zectrix, apple, engine = build(state, [todo], [other])
        engine.run([todo])

        assert zectrix.todos == {}
        assert "a1" not in state.links
        # 回归：刚删掉的待办不能在「新建」阶段又被推回提醒事项
        assert apple.calls == []
        assert zectrix.kinds() == ["delete"]

    def test_zectrix_deleted_keeps_reminder_by_default(self, state):
        snap = task()
        state.link("a1", "e1", 1, snap)
        rem = ARem("a1", "e1", snap, 100, "提醒事项")
        zectrix, apple, engine = build(state, [], [rem])    # Zectrix 里没有了
        engine.run([])

        assert "a1" in apple.items          # 提醒事项保留
        assert "a1" not in state.links
        assert "a1" in state.tomb_apple     # 打墓碑，下次不会又建回去

    def test_tombstone_prevents_resurrection(self, state):
        state.link("a1", "e1", 1, task())
        rem = ARem("a1", "e1", task(), 100, "提醒事项")
        zectrix, apple, engine = build(state, [], [rem])
        engine.run([])

        zectrix2, _, engine2 = build(state, [], [rem])
        engine2.run([])
        assert zectrix2.todos == {}

    def test_mirror_policy_deletes_reminder(self, state, monkeypatch):
        monkeypatch.setattr(sc, "DELETE_POLICY", "mirror")
        state.link("a1", "e1", 1, task())
        rem = ARem("a1", "e1", task(), 100, "提醒事项")
        zectrix, apple, engine = build(state, [], [rem])
        engine.run([])

        assert apple.items == {}
        assert "a1" not in state.links
        # 回归：刚删掉的提醒不能在「新建」阶段又被推回墨水屏
        assert zectrix.calls == []
        assert apple.kinds() == ["delete"]

    def test_none_policy_deletes_nothing(self, state, monkeypatch):
        monkeypatch.setattr(sc, "DELETE_POLICY", "none")
        snap = task()
        state.link("a1", "e1", 1, snap)
        todo = ZTodo(1, snap, "SOURCE: apple\nUID: reminder:a1", 100)
        other = ARem("a2", "e2", task("别的事", date=TOMORROW), 100, "提醒事项")
        zectrix, apple, engine = build(state, [todo], [other])
        engine.run([todo])

        assert zectrix.todos == {1: todo}
        assert "1" in state.tomb_zectrix

    def test_both_gone_just_drops_link(self, state):
        state.link("a1", "e1", 1, task())
        zectrix, apple, engine = build(state, [], [])
        engine.run([])
        assert state.links == {}
        assert state.tomb_apple == {}


# --------------------------------------------------------------------------
# 「读到空」不授权删除
# --------------------------------------------------------------------------

class TestCrossMachineDeleteGuard:
    """一台看不到某个日历/列表的机器，不许删那里来的东西。

    实际踩到的两个坑，方向相反，必须同时成立：
      A) 新装的第二台没加 UMD 账号 -> 不能把第一台同步上去的 UMD 日程删掉
      B) 但「今天一个会都没有」是完全正常的 -> 该删的还是要删

    所以判据是「这个日历这次读到了没有」，不是「读到的事件数是不是 0」——
    后者既挡不住 A（那台可能读到别的日历、结果非空），又会误伤 B。
    """

    # -- 日历 -------------------------------------------------------------

    def _cal_todo(self, todo_id, uid, cal, title="[日历] 组会"):
        desc = f"SOURCE: calendar\nUID: {uid}"
        if cal:
            desc += f"\nCAL: {cal}"
        return ZTodo(todo_id, Task(title, TODAY, "10:00", False), desc, 100)

    def test_invisible_calendar_is_not_deleted(self, capsys):
        """A：读不到 UMD 那个日历，就不能删它的日程。"""
        todo = self._cal_todo(1, f"evt-1@{TODAY}", "xjguo@umd.edu")
        zectrix = FakeZectrix([todo])
        sc.CalendarSync(zectrix)._sync([], [todo], visible={"Home", "Work"})

        assert zectrix.calls == []
        out = capsys.readouterr().out
        assert "保留 1 条日程" in out
        assert "xjguo@umd.edu" in out

    def test_empty_day_on_visible_calendar_still_deletes(self, capsys):
        """B：日历读到了、今天就是没有会 —— 该删。这条是用户实际报的 bug。"""
        todo = self._cal_todo(1, f"evt-1@{TODAY}", "Home")
        zectrix = FakeZectrix([todo])
        sc.CalendarSync(zectrix)._sync([], [todo], visible={"Home", "Work"})

        assert ("delete", 1) in zectrix.calls
        assert "保留" not in capsys.readouterr().out

    def test_legacy_todo_without_cal_field_still_deletes(self):
        """老数据没有 CAL 字段，按老规矩处理，不然永远删不掉。"""
        todo = self._cal_todo(1, f"evt-1@{TODAY}", None)
        zectrix = FakeZectrix([todo])
        sc.CalendarSync(zectrix)._sync([], [todo], visible={"Home"})
        assert ("delete", 1) in zectrix.calls

    def test_mixed_visibility_deletes_only_the_visible_one(self):
        mine = self._cal_todo(1, f"evt-1@{TODAY}", "Home", "[日历] 本机的会")
        theirs = self._cal_todo(2, f"evt-2@{TODAY}", "xjguo@umd.edu", "[日历] UMD 的会")
        zectrix = FakeZectrix([mine, theirs])
        sc.CalendarSync(zectrix)._sync([], [mine, theirs], visible={"Home"})

        assert ("delete", 1) in zectrix.calls
        assert ("delete", 2) not in zectrix.calls

    def test_cal_field_is_written_on_create(self):
        zectrix = FakeZectrix()
        sc.CalendarSync(zectrix)._sync(
            [{"uid": f"evt-1@{TODAY}", "title": "组会", "dueDate": TODAY,
              "dueTime": "10:00", "calendar": "xjguo@umd.edu"}], [], visible={"xjguo@umd.edu"})
        _, _, description = zectrix.calls[0]
        assert "CAL: xjguo@umd.edu" in description

    # -- 提醒事项 ---------------------------------------------------------

    def test_invisible_reminder_list_is_not_deleted(self, state, capsys):
        """配对时记下了列表名；这台读不到那个列表，就不能删。"""
        snap = task()
        state.link("a1", "e1", 1, snap, list_name="Daddy to do list")
        todo = ZTodo(1, snap, "SOURCE: apple\nUID: reminder:e1", 100)
        # 这台只读到 Work，提醒 a1 读不到
        zectrix, apple, engine = build(state, [todo], [], lists=["Work"])
        engine.run([todo])

        assert zectrix.calls == []
        assert "a1" in state.links
        out = capsys.readouterr().out
        assert "Daddy to do list" in out

    def test_visible_list_with_no_reminders_still_deletes(self, state):
        """列表读到了、里面确实空了 —— 该删。"""
        snap = task()
        state.link("a1", "e1", 1, snap, list_name="Work")
        todo = ZTodo(1, snap, "SOURCE: apple\nUID: reminder:e1", 100)
        zectrix, apple, engine = build(state, [todo], [], lists=["Work"])
        engine.run([todo])
        assert zectrix.todos == {}

    def test_legacy_link_without_list_name_still_deletes(self, state):
        snap = task()
        state.link("a1", "e1", 1, snap)          # 老状态文件没有 listName
        todo = ZTodo(1, snap, "SOURCE: apple\nUID: reminder:e1", 100)
        zectrix, apple, engine = build(state, [todo], [], lists=["Work"])
        engine.run([todo])
        assert zectrix.todos == {}

    def test_list_name_is_recorded_on_link(self, state):
        rem = ARem("a1", "e1", task("买牛奶"), 100, "shopping")
        zectrix, apple, engine = build(state, reminders=[rem])
        engine.run([])
        assert state.links["a1"]["listName"] == "shopping"


# --------------------------------------------------------------------------
# 第二台 Mac
# --------------------------------------------------------------------------

class TestSecondMac:
    """同一条 iCloud 提醒，在另一台 Mac 上 calendarItemIdentifier 是不一样的。

    第二台机器的状态文件是空的，只能靠 Zectrix 备注里的 external id 认回配对，
    否则就会把每条任务在两边都建一份重复的。
    """

    def _mac_b(self, state_b, z_todos, reminders):
        """B 机器：同样的提醒，但本地 id 全变了，external id 不变。"""
        relocated = [ARem(f"B-{r.apple_id}", r.ext_id, r.task, r.updated_at, r.list_name)
                     for r in reminders]
        return build(state_b, z_todos, relocated)

    def test_second_mac_adopts_instead_of_duplicating(self, state, tmp_path):
        # A 机器：从提醒事项建出墨水屏待办
        rem = ARem("A-1", "icloud-uid-1", task("买牛奶"), 100, "提醒事项")
        zectrix_a, apple_a, engine_a = build(state, reminders=[rem])
        engine_a.run([])
        assert len(zectrix_a.todos) == 1

        # B 机器：全新状态文件，看到同样的数据
        state_b = SyncState(str(tmp_path / "mac_b.json"))
        z_todos = list(zectrix_a.todos.values())
        zectrix_b, apple_b, engine_b = self._mac_b(state_b, z_todos, [rem])
        engine_b.run(z_todos)

        assert len(zectrix_b.todos) == 1, "B 机器不该再建一条墨水屏待办"
        assert len(apple_b.items) == 1, "B 机器不该再建一条提醒事项"
        assert state_b.links["B-A-1"]["zectrixId"] == z_todos[0].todo_id
        assert zectrix_b.calls == []
        assert apple_b.calls == []

    def test_second_mac_propagates_completion(self, state, tmp_path):
        rem = ARem("A-1", "icloud-uid-1", task("买牛奶"), 100, "提醒事项")
        zectrix_a, apple_a, engine_a = build(state, reminders=[rem])
        engine_a.run([])

        # 在 B 机器上把这条提醒勾掉
        done = ARem("B-A-1", "icloud-uid-1", task("买牛奶", completed=True), 500, "提醒事项")
        state_b = SyncState(str(tmp_path / "mac_b.json"))
        z_todos = list(zectrix_a.todos.values())
        zectrix_b, apple_b, engine_b = build(state_b, z_todos, [done])
        engine_b.run(z_todos)

        todo = next(iter(zectrix_b.todos.values()))
        assert todo.task.completed is True

    def test_old_local_id_anchor_still_resolves(self, state, tmp_path):
        """老版本备注里写的是本地 id，升级后不能突然认不出来。"""
        rem = ARem("a1", "e1", task("旧数据"), 100, "提醒事项")
        todo = ZTodo(7, task("旧数据"), "SOURCE: apple\nUID: reminder:a1", 100)
        zectrix, apple, engine = build(state, [todo], [rem])
        engine.run([todo])

        assert state.links["a1"]["zectrixId"] == 7
        assert len(zectrix.todos) == 1
        assert len(apple.items) == 1


# --------------------------------------------------------------------------
# 收敛性：跑第二遍必须是空操作
# --------------------------------------------------------------------------

class TestConvergence:
    """每个场景都跑两遍，第二遍不能再产生任何读写 —— 否则就是来回打架的死循环。"""

    def _second_run_is_noop(self, state, zectrix, apple):
        z2 = FakeZectrix(list(zectrix.todos.values()))
        a2 = FakeApple(list(apple.items.values()))
        ReminderSync(z2, a2, state).run(list(z2.todos.values()))
        assert z2.calls == [], f"第二遍还在写 Zectrix: {z2.calls}"
        assert a2.calls == [], f"第二遍还在写 Apple: {a2.calls}"

    def test_after_creating_from_apple(self, state):
        rem = ARem("a1", "e1", task("买牛奶"), 100, "提醒事项")
        zectrix, apple, engine = build(state, reminders=[rem])
        engine.run([])
        self._second_run_is_noop(state, zectrix, apple)

    def test_after_creating_from_zectrix(self, state):
        todo = ZTodo(1, task("买菜"), "", 100)
        zectrix, apple, engine = build(state, z_todos=[todo])
        engine.run([todo])
        self._second_run_is_noop(state, zectrix, apple)

    def test_after_completion_propagates(self, state):
        snap = task()
        state.link("a1", "e1", 1, snap)
        todo = ZTodo(1, snap, "SOURCE: apple\nUID: reminder:a1", 100)
        rem = ARem("a1", "e1", task(completed=True), 200, "提醒事项")
        zectrix, apple, engine = build(state, [todo], [rem])
        engine.run([todo])
        self._second_run_is_noop(state, zectrix, apple)

    def test_after_conflict_resolution(self, state):
        state.link("a1", "e1", 1, task("原始"))
        todo = ZTodo(1, task("墨水屏改的"), "SOURCE: apple\nUID: reminder:a1", 100)
        rem = ARem("a1", "e1", task("提醒事项改的"), 999, "提醒事项")
        zectrix, apple, engine = build(state, [todo], [rem])
        engine.run([todo])
        self._second_run_is_noop(state, zectrix, apple)

    def test_after_apple_deletion(self, state):
        snap = task()
        state.link("a1", "e1", 1, snap)
        todo = ZTodo(1, snap, "SOURCE: apple\nUID: reminder:a1", 100)
        zectrix, apple, engine = build(state, [todo], [])
        engine.run([todo])
        self._second_run_is_noop(state, zectrix, apple)

    def test_after_zectrix_deletion(self, state):
        state.link("a1", "e1", 1, task())
        rem = ARem("a1", "e1", task(), 100, "提醒事项")
        zectrix, apple, engine = build(state, [], [rem])
        engine.run([])
        self._second_run_is_noop(state, zectrix, apple)


# --------------------------------------------------------------------------
# 状态文件与迁移
# --------------------------------------------------------------------------

class TestState:
    def test_save_and_reload(self, tmp_path):
        path = str(tmp_path / "s.json")
        s = SyncState(path)
        s.link("a1", "e1", 42, task("持久化"))
        s.save(dry_run=False)

        again = SyncState(path)
        assert again.links["a1"]["zectrixId"] == 42
        assert Task.from_dict(again.links["a1"]["snapshot"]) == task("持久化")

    def test_dry_run_never_writes(self, tmp_path):
        path = str(tmp_path / "s.json")
        s = SyncState(path)
        s.link("a1", "e1", 42, task())
        s.save(dry_run=True)
        assert not os.path.exists(path)

    def test_version_mismatch_is_ignored(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(json.dumps({"version": 1, "links": {"a1": {}}}), encoding="utf-8")
        assert SyncState(str(path)).links == {}

    def test_corrupt_file_is_ignored(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text("{ not json", encoding="utf-8")
        assert SyncState(str(path)).links == {}

    def test_tombstones_expire(self, state, monkeypatch):
        monkeypatch.setattr(sc, "TOMBSTONE_DAYS", 30)
        old = (datetime.datetime.now() - datetime.timedelta(days=99)).isoformat(timespec="seconds")
        state.tomb_apple["stale"] = old
        state.tomb_apple["fresh"] = datetime.datetime.now().isoformat(timespec="seconds")
        state.prune_tombstones()
        assert "stale" not in state.tomb_apple
        assert "fresh" in state.tomb_apple

    def test_migration_recovers_link_from_description(self, state):
        """状态文件丢失后，靠 Zectrix 备注里的 UID 重新配对，而不是重复创建。"""
        rem = ARem("a1", "e1", task("已存在"), 100, "提醒事项")
        todo = ZTodo(7, task("已存在"), "SOURCE: apple\nUID: reminder:a1", 100)
        zectrix, apple, engine = build(state, [todo], [rem])
        engine.run([todo])

        assert state.links["a1"]["zectrixId"] == 7
        assert len(zectrix.todos) == 1
        assert len(apple.items) == 1


# --------------------------------------------------------------------------
# dry-run
# --------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_writes_nothing(self, state):
        rem = ARem("a1", "e1", task("新提醒"), 100, "提醒事项")
        todo = ZTodo(1, task("新待办"), "", 100)
        zectrix, apple, engine = build(state, [todo], [rem], dry_run=True)
        engine.run([todo])

        assert len(zectrix.todos) == 1          # 只有原来那条
        assert len(apple.items) == 1            # 只有原来那条
        assert state.links == {}                # dry-run 不建立配对


# --------------------------------------------------------------------------
# 日历事件解析（保留原有单向逻辑）
# --------------------------------------------------------------------------

class TestCalendarParsing:
    def _ics(self, summary, dtstart, uid="uid-1"):
        class Item:
            data = (
                "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
                f"UID:{uid}\r\nSUMMARY:{summary}\r\nDTSTART:{dtstart}\r\n"
                "END:VEVENT\r\nEND:VCALENDAR\r\n"
            )
        return Item()

    def test_parses_event_in_range(self):
        now = datetime.datetime.now().astimezone()
        soon = now + datetime.timedelta(hours=1)
        end = now + datetime.timedelta(days=1)
        out = sc.CalendarSync._parse(
            self._ics("周会", soon.strftime("%Y%m%dT%H%M%S")), now, end)
        assert len(out) == 1
        assert out[0]["title"] == "周会"
        assert out[0]["dueTime"] == soon.strftime("%H:%M")

    @pytest.mark.parametrize("summary", ["已取消 周会", "Cancelled standup", "CANCELED sync"])
    def test_skips_cancelled(self, summary):
        now = datetime.datetime.now().astimezone()
        soon = now + datetime.timedelta(hours=1)
        out = sc.CalendarSync._parse(
            self._ics(summary, soon.strftime("%Y%m%dT%H%M%S")),
            now, now + datetime.timedelta(days=1))
        assert out == []

    def test_keeps_event_that_already_started(self):
        """回归：会议开始后不能从结果里消失。

        之前起点取的是「现在」，已开始的会议落在窗口外 -> 匹配不上现有待办
        -> 被当成「日历里删了」删掉。表现就是会议一开始就从墨水屏上消失，
        而且 EXPIRE_HOURS 的自动划掉永远轮不到执行。
        """
        start, end = sc.CalendarSync.event_window()
        start, end = start.astimezone(), end.astimezone()
        began = datetime.datetime.now().astimezone() - datetime.timedelta(hours=2)
        out = sc.CalendarSync._parse(
            self._ics("两小时前开始的会", began.strftime("%Y%m%dT%H%M%S")), start, end)
        assert len(out) == 1
        assert out[0]["title"] == "两小时前开始的会"

    def test_skips_event_outside_the_window(self):
        start, end = sc.CalendarSync.event_window()
        start, end = start.astimezone(), end.astimezone()
        long_ago = start - datetime.timedelta(days=3)
        out = sc.CalendarSync._parse(
            self._ics("上周的会", long_ago.strftime("%Y%m%dT%H%M%S")), start, end)
        assert out == []

    def test_window_starts_at_midnight_not_now(self):
        start, end = sc.CalendarSync.event_window()
        assert (start.hour, start.minute, start.second) == (0, 0, 0)
        assert start.date() == datetime.date.today()
        assert end.date() == datetime.date.today() + datetime.timedelta(days=1)

    def test_recurring_instances_on_different_days_get_distinct_keys(self):
        """重复日程每次展开 UID 相同，靠日期后缀区分，否则一个系列只剩一条。"""
        now = datetime.datetime.now().astimezone()
        end = now + datetime.timedelta(days=7)
        out = []
        for days in (1, 2, 3):
            when = now + datetime.timedelta(days=days)
            out += sc.CalendarSync._parse(
                self._ics("站会", when.strftime("%Y%m%dT%H%M%S"), uid="weekly-1"),
                now, end)
        assert len({e["uid"] for e in out}) == 3
        assert all(e["uid"].startswith("weekly-1@") for e in out)

    def test_key_survives_a_time_change(self):
        """后缀只到日期不到分钟：会议改时间要走「更新」，不能变成删了重建。"""
        now = datetime.datetime.now().astimezone()
        end = now + datetime.timedelta(days=2)
        base = (now + datetime.timedelta(days=1)).replace(hour=10, minute=0, second=0)
        moved = base.replace(hour=14)
        a = sc.CalendarSync._parse(
            self._ics("组会", base.strftime("%Y%m%dT%H%M%S"), uid="evt-1"), now, end)
        b = sc.CalendarSync._parse(
            self._ics("组会", moved.strftime("%Y%m%dT%H%M%S"), uid="evt-1"), now, end)
        assert a[0]["uid"] == b[0]["uid"]
        assert a[0]["dueTime"] != b[0]["dueTime"]


class TestCalendarSources:
    """CALENDAR_SOURCE 的取事件 + 去重逻辑。"""

    def _syncer(self, monkeypatch, source, eventkit=None, caldav=None):
        monkeypatch.setattr(sc, "CALENDAR_SOURCE", source)
        monkeypatch.setattr(sc, "CALDAV_PASS", "x" if caldav is not None else "")
        syncer = sc.CalendarSync(FakeZectrix())

        def fake_ek():
            if isinstance(eventkit, Exception):
                raise eventkit
            evs = eventkit or []
            return evs, sorted({e.get("calendar", "日历") for e in evs}) or ["日历"]

        def fake_caldav():
            if caldav is None:
                return None
            return caldav, sorted({e.get("calendar", "CalDAV") for e in caldav})

        monkeypatch.setattr(syncer, "_fetch_from_eventkit", fake_ek)
        monkeypatch.setattr(syncer, "_fetch_from_caldav", fake_caldav)
        return syncer

    def _event(self, uid, title, cal="日历"):
        return {"uid": uid, "title": title, "dueDate": TODAY,
                "dueTime": "10:00", "calendar": cal}

    def test_both_sources_dedup_by_uid(self, monkeypatch):
        """同一条 iCloud 事件两边都能拿到，墨水屏上不能出现两次。"""
        shared = self._event("icloud-1@" + TODAY, "组会")
        only_google = self._event("google-1@" + TODAY, "UMD 课")
        syncer = self._syncer(monkeypatch, "both",
                              eventkit=[shared, only_google], caldav=[shared])
        events, visible = syncer._collect_events()
        assert len(events) == 2
        assert {e["uid"] for e in events} == {shared["uid"], only_google["uid"]}

    def test_eventkit_only_skips_caldav(self, monkeypatch):
        syncer = self._syncer(monkeypatch, "eventkit",
                              eventkit=[self._event("a@" + TODAY, "会")],
                              caldav=[self._event("b@" + TODAY, "不该出现")])
        events, visible = syncer._collect_events()
        assert [e["title"] for e in events] == ["会"]

    def test_one_source_failing_still_returns_the_other(self, monkeypatch):
        syncer = self._syncer(monkeypatch, "both",
                              eventkit=sc.AppleRemindersError("没权限"),
                              caldav=[self._event("b@" + TODAY, "CalDAV 的会")])
        events, visible = syncer._collect_events()
        assert [e["title"] for e in events] == ["CalDAV 的会"]

    def test_all_sources_failing_returns_none(self, monkeypatch):
        """全挂了要返回 None —— 让上层跳过删除，别把日程全清了。"""
        syncer = self._syncer(monkeypatch, "both",
                              eventkit=sc.AppleRemindersError("没权限"), caldav=None)
        assert syncer._collect_events() is None

    def test_missing_pyobjc_is_not_fatal(self, monkeypatch):
        syncer = self._syncer(monkeypatch, "both",
                              eventkit=ImportError("no pyobjc"),
                              caldav=[self._event("b@" + TODAY, "会")])
        assert len(syncer._collect_events()[0]) == 1


class TestCalendarSyncMatching:
    def _todo(self, todo_id, uid, title="[日历] 组会", time="10:00"):
        return ZTodo(todo_id, Task(title, TODAY, time, False),
                     f"SOURCE: calendar\nUID: {uid}", 100)

    def test_bare_uid_from_old_version_still_matches(self):
        """升级后 UID 格式变了，老日程不能被删掉重建。"""
        old = self._todo(1, "evt-1")
        zectrix = FakeZectrix([old])
        syncer = sc.CalendarSync(zectrix)
        syncer._sync([{"uid": f"evt-1@{TODAY}", "title": "组会",
                       "dueDate": TODAY, "dueTime": "10:00"}], [old])

        assert "delete" not in zectrix.kinds()
        assert "create" not in zectrix.kinds()
        assert zectrix.kinds() == ["update"]   # 只把备注升级成新格式

    def test_started_meeting_is_not_deleted_end_to_end(self):
        """整条链路的回归：日程还在日历里（只是已经开始），就不能被删。"""
        began = datetime.datetime.now() - datetime.timedelta(hours=2)
        uid = f"evt-1@{began:%Y-%m-%d}"
        todo = ZTodo(1, Task("[日历] 组会", f"{began:%Y-%m-%d}", f"{began:%H:%M}", False),
                     f"SOURCE: calendar\nUID: {uid}", 100)
        zectrix = FakeZectrix([todo])
        sc.CalendarSync(zectrix)._sync(
            [{"uid": uid, "title": "组会",
              "dueDate": f"{began:%Y-%m-%d}", "dueTime": f"{began:%H:%M}"}], [todo])

        assert "delete" not in zectrix.kinds()
        assert zectrix.todos[1].task.completed is False   # 由 _complete_expired 负责划掉

    def test_vanished_event_is_deleted(self):
        gone = self._todo(1, f"evt-1@{TODAY}")
        alive = self._todo(2, f"evt-2@{TODAY}", title="[日历] 还在的会")
        zectrix = FakeZectrix([gone, alive])
        # 取到了 evt-2，说明日历读得到；evt-1 是真没了
        sc.CalendarSync(zectrix)._sync(
            [{"uid": f"evt-2@{TODAY}", "title": "还在的会",
              "dueDate": TODAY, "dueTime": "10:00"}], [gone, alive])
        assert ("delete", 1) in zectrix.calls
        assert ("delete", 2) not in zectrix.calls

    def test_completed_calendar_todo_is_not_deleted(self):
        """已经划掉的过期日程不该再被删一次。"""
        done = ZTodo(1, Task("[日历] 组会", TODAY, "10:00", True),
                     f"SOURCE: calendar\nUID: evt-1@{TODAY}", 100)
        zectrix = FakeZectrix([done])
        sc.CalendarSync(zectrix)._sync([], [done])
        assert zectrix.calls == []

    def test_time_change_updates(self):
        todo = self._todo(1, f"evt-1@{TODAY}", time="10:00")
        zectrix = FakeZectrix([todo])
        sc.CalendarSync(zectrix)._sync(
            [{"uid": f"evt-1@{TODAY}", "title": "组会",
              "dueDate": TODAY, "dueTime": "14:00"}], [todo])
        assert zectrix.todos[1].task.due_time == "14:00"


class TestCalendarParsingMore:
    def test_uid_extraction(self):
        assert ReminderSync._uid_from_description("SOURCE: apple\nUID: reminder:abc") \
            == "reminder:abc"
        assert ReminderSync._uid_from_description("  UID:   x  ") == "x"
        assert ReminderSync._uid_from_description("") == ""
        assert ReminderSync._uid_from_description("没有 uid") == ""
