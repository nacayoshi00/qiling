#!/usr/bin/env python3
#
# Cross Platform and Multi Architecture Advanced Binary Emulation Framework
#

import time

from qiling.const import *
from qiling.os.linux.thread import *
from qiling.const import *
from qiling.os.posix.filestruct import *
from qiling.os.filestruct import *
from qiling.os.posix.const_mapping import *
from qiling.exception import *

def ql_syscall_time(ql, *args, **kw):
    regreturn = int(time.time())
    return regreturn

def ql_syscall_clock_nanosleep_time64(ql, nanosleep_clk_id, nanosleep_flags, nanosleep_req, nanosleep_rem, *args, **kw):
    def _sched_sleep(cur_thread):
        gevent.sleep(tv_sec)

    n = ql.pointersize

    tv_sec = ql.unpack(ql.mem.read(nanosleep_req, n))
    tv_sec += ql.unpack(ql.mem.read(nanosleep_req + n, n)) / 1000000000

    if ql.os.thread_management == None:
        time.sleep(tv_sec)
    else:
        ql.emu_stop()
        ql.os.thread_management.cur_thread.sched_cb = _sched_sleep
        th = ql.os.thread_management.cur_thread

    regreturn = 0
    return regreturn


def ql_syscall_nanosleep(ql, nanosleep_req, nanosleep_rem, *args, **kw):
    def _sched_sleep(cur_thread):
        gevent.sleep(tv_sec)

    n = ql.pointersize

    tv_sec = ql.unpack(ql.mem.read(nanosleep_req, n))
    tv_sec += ql.unpack(ql.mem.read(nanosleep_req + n, n)) / 1000000000

    if ql.os.thread_management == None:
        time.sleep(tv_sec)
    else:
        ql.emu_stop()
        ql.os.thread_management.cur_thread.sched_cb = _sched_sleep
        th = ql.os.thread_management.cur_thread

    regreturn = 0
    return regreturn

def ql_syscall_clock_nanosleep(ql, clock_nanosleep_clockid, clock_nanosleep_flags, clock_nanosleep_req, clock_nanosleep_remain, *args, **kw):
    def _sched_sleep(cur_thread):
        gevent.sleep(tv_sec)

    n = ql.pointersize

    tv_sec = ql.unpack(ql.mem.read(clock_nanosleep_req, n))
    tv_sec += ql.unpack(ql.mem.read(clock_nanosleep_req + n, n)) / 1000000000

    if ql.os.thread_management == None:
        time.sleep(tv_sec)
    else:
        ql.emu_stop()
        ql.os.thread_management.cur_thread.sched_cb = _sched_sleep
        th = ql.os.thread_management.cur_thread

    regreturn = 0
    return regreturn


def ql_syscall_setitimer(ql, setitimer_which, setitimer_new_value, setitimer_old_value, *args, **kw):
    # TODO:The system provides each process with three interval timers, each decrementing in a distinct time domain.
    # When any timer expires, a signal is sent to the process, and the timer (potentially) restarts.
    # But I haven’t figured out how to send a signal yet.
    regreturn = 0
    return regreturn


def ql_syscall_times(ql, times_tbuf, *args, **kw):
    tmp_times = os.times()
    if times_tbuf != 0:
        tmp_buf = b''
        tmp_buf += ql.pack32(int(tmp_times.user * 1000))
        tmp_buf += ql.pack32(int(tmp_times.system * 1000))
        tmp_buf += ql.pack32(int(tmp_times.children_user * 1000))
        tmp_buf += ql.pack32(int(tmp_times.children_system * 1000))
        ql.mem.write(times_tbuf, tmp_buf)
    regreturn = int(tmp_times.elapsed * 100)
    return regreturn

