import asyncio
import json
import threading
from datetime import datetime
from typing import Any, Dict, Set
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent  # backend/services -> 项目根
TASKS_FILE = BASE / "config" / "tasks.json"


class _Ticket:
    """全局 FIFO 排队凭证：一个等待执行名额的任务对应一张票。"""

    __slots__ = ("tid", "order", "future", "loop")

    def __init__(self, tid, order, future, loop):
        self.tid = tid
        self.order = order
        self.future = future
        self.loop = loop


def _resolve_ticket(ticket, granted: bool):
    try:
        if not ticket.future.done():
            ticket.future.set_result(granted)
    except Exception:
        pass


def _wake_tickets(tickets, granted: bool = True):
    """可从任意线程调用：统一经事件循环回调唤醒等待协程。"""
    for tk in tickets:
        try:
            tk.loop.call_soon_threadsafe(_resolve_ticket, tk, granted)
        except Exception:
            pass


class DynamicSemaphore:
    """可运行时调整上限的「全局 FIFO」并发闸门（图片 / 视频 / Live 任务共用）。

    - limit：管理员设置的并发任务数；ceiling：最高并发限制。
    - 所有用户（本机 / 在线）共用同一闸门；超过 limit 的任务按提交序号 seq
      严格先进先出排队，后来者绝不插队。
    - 释放名额时直接把名额「交接」给队首任务（active 不减），保证顺序稳定。
    - 调高上限时立即按 FIFO 补足名额；取消 / 删除排队任务即时出队并让后续补位。
    """

    def __init__(self, limit: int = 2, ceiling: int = 5):
        self.ceiling = max(1, int(ceiling or 5))
        self.limit = max(1, min(int(limit or 2), self.ceiling))
        self.active = 0
        self._lock = threading.RLock()
        self._waiting = []        # 按 order 升序排列的 _Ticket 列表
        self._by_tid = {}         # tid -> _Ticket（仅排队中的任务）
        self._seq = 0             # 未传 seq 时的兜底序号

    def _drain_locked(self):
        """把空闲名额按 FIFO 顺序交接给队首等待者；调用方须已持有 _lock。"""
        wake = []
        while self.active < self.limit and self._waiting:
            tk = self._waiting.pop(0)
            if tk.tid:
                self._by_tid.pop(tk.tid, None)
            self.active += 1      # 名额直接交接，杜绝后来者插队
            wake.append(tk)
        return wake

    async def acquire(self, tid=None, seq=None, on_wait=None):
        """获取执行名额；需要排队时回调 on_wait（把任务标成「排队中」）。

        返回 False 表示排队期间任务被取消 / 删除，调用方应直接退出且不释放名额。
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            wake = self._drain_locked()
            if self.active < self.limit and not self._waiting:
                self.active += 1
                ticket = None
            else:
                self._seq += 1
                ticket = _Ticket(tid, seq if seq is not None else self._seq,
                                 loop.create_future(), loop)
                self._waiting.append(ticket)
                self._waiting.sort(key=lambda x: x.order)
                if tid:
                    self._by_tid[tid] = ticket
        _wake_tickets(wake)
        if ticket is None:
            return True
        if on_wait:
            try:
                on_wait()
            except Exception:
                pass
        return bool(await ticket.future)

    async def release(self):
        with self._lock:
            self.active = max(0, self.active - 1)
            wake = self._drain_locked()
        _wake_tickets(wake)

    def configure_sync(self, limit=None, ceiling=None) -> dict:
        """启动时同步套用配置（此刻无排队任务）。"""
        with self._lock:
            if ceiling is not None and int(ceiling) > 0:
                self.ceiling = int(ceiling)
            if limit is not None and int(limit) > 0:
                self.limit = max(1, min(int(limit), self.ceiling))
            else:
                self.limit = min(self.limit, self.ceiling)
            wake = self._drain_locked()
        _wake_tickets(wake)
        return self.status()

    async def configure(self, limit=None, ceiling=None) -> dict:
        """运行中调整并发数 / 上限，保存设置后即时生效。"""
        with self._lock:
            if ceiling is not None and int(ceiling) > 0:
                self.ceiling = int(ceiling)
            if limit is not None and int(limit) > 0:
                self.limit = max(1, min(int(limit), self.ceiling))
            else:
                self.limit = min(self.limit, self.ceiling)
            wake = self._drain_locked()
        _wake_tickets(wake)
        return self.status()

    def cancel_wait_many(self, tids) -> int:
        """线程安全：批量把任务移出 FIFO 队列，并让后续任务立即补位。"""
        with self._lock:
            removed = []
            for tid in tids:
                tk = self._by_tid.pop(tid, None)
                if not tk:
                    continue
                removed.append(tk)
                if tk in self._waiting:
                    self._waiting.remove(tk)
            wake = self._drain_locked()
        _wake_tickets(removed, False)
        _wake_tickets(wake)
        return len(removed)

    def cancel_wait(self, tid) -> bool:
        return self.cancel_wait_many([tid]) > 0

    def status(self) -> dict:
        with self._lock:
            return {"concurrency": self.limit, "max_concurrency": self.ceiling,
                    "active": self.active, "waiting": len(self._waiting)}

    def snapshot(self) -> dict:
        """队列快照（可从同步接口线程安全调用）。"""
        with self._lock:
            return {
                "concurrency": self.limit,
                "max_concurrency": self.ceiling,
                "active": self.active,
                "waiting_count": len(self._waiting),
                "waiting": [{"tid": tk.tid, "pos": i + 1, "ahead": i}
                            for i, tk in enumerate(self._waiting)],
            }


class TaskManager:
    """管理批处理任务：状态、进度、取消、磁盘持久化（已完成/已取消任务跨刷新/重启保留）。"""

    def __init__(self):
        self.tasks: Dict[str, dict] = {}
        self.cancelled: Set[str] = set()
        self.paused: Set[str] = set()          # 已暂停的任务（保留产物与进度，可继续）
        self.jobs: Dict[str, dict] = {}        # 运行中/暂停任务的上下文（含原始字节，仅内存）
        self.semaphore = DynamicSemaphore(2, 5)
        self._lock = threading.RLock()         # 保护 tasks / seq / 磁盘写入（异步循环 + 线程池都会访问）
        self._seq = 0                          # 全局提交序号：FIFO 排队的唯一依据
        self.load()

    # 可跨刷新/重启保留的终态（failed 也保留，否则失败原因会凭空消失，难以排查）
    FINAL_STATES = ("done", "cancelled", "failed")
    QUEUED_STATES = ("pending", "queued", "waiting")

    def load(self):
        """启动时恢复终态任务（进行中的视为中断，不恢复）。"""
        try:
            if TASKS_FILE.exists():
                data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
                for tid, t in data.items():
                    if t.get("status") in self.FINAL_STATES:
                        # 旧数据无归属，标为 legacy：仅管理员可见，不自动认领
                        t.setdefault("owner_id", "")
                        t.setdefault("download_count", 0)
                        t.setdefault("seq", 0)
                        self.tasks[tid] = t
        except Exception:
            pass

    def persist(self):
        """仅持久化终态任务，保持文件精简。"""
        with self._lock:
            try:
                TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
                data = {tid: t for tid, t in self.tasks.items()
                        if t.get("status") in self.FINAL_STATES}
                TASKS_FILE.write_text(json.dumps(data, ensure_ascii=False, default=str),
                                      encoding="utf-8")
            except Exception:
                pass

    def register(self, tid, kind="image", total=0, out_dir="", owner_id=""):
        """注册新任务：记录创建时间、下载计数、全局提交序号（FIFO 依据）。"""
        with self._lock:
            self._seq += 1
            self.tasks[tid] = {
                "id": tid, "kind": kind, "total": total, "out_dir": out_dir,
                "owner_id": owner_id,   # 不可变账户 id：任务/产物归属的唯一依据
                "created_at": datetime.now().isoformat(),
                "download_count": 0, "last_download_at": None,
                "seq": self._seq,
                "processed": 0, "percent": 0, "status": "pending",
                "stage": "准备中", "log": "", "outputs": [],
                "started_at": None,   # 进入 running 时记录开始时间（前端据此估算剩余倒计时）
            }
            self.persist()

    def renumber(self, tid):
        """重试 / 继续时重新发号：排到队尾，不抢占之前提交的任务。"""
        with self._lock:
            self._seq += 1
            t = self.tasks.get(tid)
            if t:
                t["seq"] = self._seq
            return self._seq

    def queue_overview(self):
        """全局待执行任务（含刚提交、尚未进入闸门的），按提交序号升序 = FIFO 顺序。"""
        with self._lock:
            pend = [dict(t) for t in list(self.tasks.values())
                    if t.get("status") in self.QUEUED_STATES]
        pend.sort(key=lambda t: (int(t.get("seq") or 0),
                                 str(t.get("created_at") or ""), str(t.get("id") or "")))
        return pend

    def bump_download(self, ids):
        """在线用户打包下载计数：每个被打包的任务 +1（本机「打开目录」不计入）。"""
        with self._lock:
            now = datetime.now().isoformat()
            for tid in ids:
                t = self.tasks.get(str(tid))
                if not t:
                    continue
                t["download_count"] = int(t.get("download_count") or 0) + 1
                t["last_download_at"] = now
            self.persist()

    def cancel(self, tid):
        with self._lock:
            self.cancelled.add(tid)
            t = self.tasks.get(tid)
            if t:
                t["status"] = "cancelled"
                t["stage"] = "已取消"
                self.persist()
        self.semaphore.cancel_wait(tid)

    def is_cancelled(self, tid):
        return tid in self.cancelled

    def pause(self, tid):
        """暂停任务：保留已产出的文件与进度，等待用户继续。"""
        with self._lock:
            if tid in self.tasks:
                self.paused.add(tid)
                self.tasks[tid]["status"] = "paused"
                self.tasks[tid]["stage"] = "已暂停"

    def unpause(self, tid):
        self.paused.discard(tid)

    def is_paused(self, tid):
        return tid in self.paused

    def remove(self, tid):
        """移除单个任务（含清除取消/暂停标记与运行上下文）。"""
        with self._lock:
            self.tasks.pop(tid, None)
            self.cancelled.discard(tid)
            self.paused.discard(tid)
            self.jobs.pop(tid, None)
            self.persist()
        self.semaphore.cancel_wait(tid)

    def clear(self, ids=None):
        """清空任务。ids 为 None 时清除全部；否则只清除指定 id。返回实际移除数量。"""
        with self._lock:
            if ids is None:
                removed = len(self.tasks)
                all_ids = list(self.tasks.keys())
                self.tasks.clear()
                self.cancelled.clear()
                self.paused.clear()
                self.jobs.clear()
            else:
                removed = 0
                all_ids = []
                for tid in ids:
                    if self.tasks.pop(tid, None) is not None:
                        removed += 1
                    all_ids.append(tid)
                    self.cancelled.discard(tid)
                    self.paused.discard(tid)
                    self.jobs.pop(tid, None)
            self.persist()
        self.semaphore.cancel_wait_many(all_ids)
        return removed

    def progress(self, tid, **kw):
        dirty_uid = None
        with self._lock:
            t = self.tasks.get(tid)
            if not t:
                return
            # 进入 running 时记录开始时间（仅记一次），供前端倒计时估算剩余时间
            if kw.get("status") == "running" and not t.get("started_at"):
                t["started_at"] = datetime.now().isoformat()
            t.update(kw)
            if t.get("status") in self.FINAL_STATES:
                self.persist()
                # 任务终态（产物已写盘）：标记该用户空间用量待重扫
                dirty_uid = t.get("owner_id") or None
        if dirty_uid:
            # 下次查询用量时重扫一次自愈，避免每次 info 都全盘扫描
            try:
                from . import space as _space_svc
                _space_svc.mark_dirty(dirty_uid)
            except Exception:
                pass
