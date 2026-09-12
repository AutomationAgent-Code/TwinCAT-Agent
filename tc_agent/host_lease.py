"""Pin a foreground run to one Windows process lifetime and solution."""
import asyncio
import ctypes
import os


class HostUnavailable(RuntimeError):
    pass


def process_identity(pid):
    if not pid:
        return None
    if os.name != 'nt':
        raise HostUnavailable('XAE host monitoring requires Windows')
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise HostUnavailable(f'XAE PID {pid} 已退出或不可访问；已暂停任务，请重开同一工程后继续。')
    try:
        status = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(status)) or status.value != 259:
            raise HostUnavailable(f'XAE PID {pid} 已退出；已暂停任务。')
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            raise HostUnavailable('无法确认 XAE 进程启动标识，拒绝继续写入')
        return (pid, times[0].dwHighDateTime, times[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


class HostLease:
    def __init__(self, pid, solution):
        self.pid, self.solution = int(pid or 0), str(solution or '')
        self.identity = process_identity(self.pid)

    def check(self):
        if self.pid and process_identity(self.pid) != self.identity:
            raise HostUnavailable('XAE 进程已更换，不能在旧任务中自动换目标；请重新核对工程后继续。')

    def check_solution(self):
        self.check()
        if not self.pid:
            return
        from tc_template._ps_bridge import tool_target, ps_com
        try:
            with tool_target(self.pid):
                info = ps_com('project-info', timeout=15)
        except Exception as exc:
            raise HostUnavailable('当前 XAE 会话不可访问，已暂停工程写入；检查宿主/弹窗后继续。' + str(exc)[:200]) from exc
        self.check()
        if os.path.normcase(os.path.abspath(info.get('solution') or '')) != os.path.normcase(os.path.abspath(self.solution)):
            raise HostUnavailable('XAE 解决方案已变化；任务保持原工程上下文，拒绝写入另一工程。')

    def check_context(self, pid, solution):
        self.check()
        if int(pid or 0) != self.pid or str(solution or '').casefold() != self.solution.casefold():
            raise HostUnavailable('任务运行期间宿主上下文改变，必须重新核对原工程后继续。')

    def adopt_created_solution(self, result):
        self.check()
        if self.solution or not result.get('verified') or int(result.get('pid') or 0) != self.pid:
            raise HostUnavailable('新建工程结果与原空白 XAE 不匹配')
        candidate = str(result.get('solution') or '')
        if not candidate:
            raise HostUnavailable('新建工程缺少解决方案路径')
        self.solution = candidate
        self.check_solution()
        return candidate

    async def watch_model(self, factory, interval=2):
        self.check()
        task = asyncio.create_task(factory())
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=interval)
                self.check()
                if done:
                    return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
