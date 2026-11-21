"""Tiny background scheduler: named tasks that run every N seconds in one daemon thread.

Failures are recorded per task and never stop the loop, so a dead internet
connection just means stale data with an "as of" time.
"""

import threading
import time
import traceback
from datetime import datetime


class Scheduler:
    def __init__(self, tick=30):
        self.tick = tick
        self.tasks = {}
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self.thread = None

    def every(self, name, seconds, fn, first_delay=5, at_hour=None):
        """Run fn every `seconds`; with at_hour, only once a day in that local hour."""
        with self.lock:
            self.tasks[name] = {"fn": fn, "seconds": seconds, "at_hour": at_hour,
                                "next": time.time() + first_delay, "last_run": "", "last_ok": "",
                                "last_error": "", "running": False, "last_day": ""}

    def run_now(self, name):
        with self.lock:
            self.tasks[name]["next"] = 0

    def status(self):
        with self.lock:
            return {n: {k: t[k] for k in ("seconds", "at_hour", "last_run", "last_ok", "last_error", "running")}
                    for n, t in self.tasks.items()}

    def _due(self, t, now):
        if t["running"] or now < t["next"]:
            return False
        if t["at_hour"] is not None and t["next"] != 0:
            local = datetime.now()
            return local.hour == t["at_hour"] and t["last_day"] != local.date().isoformat()
        return True

    def _run(self, name, t):
        stamp = datetime.now().isoformat(timespec="seconds")
        try:
            t["fn"]()
            t["last_ok"], t["last_error"] = stamp, ""
        except Exception as exc:
            t["last_error"] = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        finally:
            with self.lock:
                t["running"] = False
                t["last_run"] = stamp
                t["last_day"] = datetime.now().date().isoformat()
                t["next"] = time.time() + t["seconds"]

    def loop(self):
        while not self._stop.wait(self.tick):
            now = time.time()
            with self.lock:
                due = [(n, t) for n, t in self.tasks.items() if self._due(t, now)]
                for _n, t in due:
                    t["running"] = True
            for n, t in due:
                threading.Thread(target=self._run, args=(n, t), daemon=True, name=f"task-{n}").start()

    def start(self):
        self.thread = threading.Thread(target=self.loop, daemon=True, name="scheduler")
        self.thread.start()

    def stop(self):
        self._stop.set()
