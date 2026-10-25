"""检测本机 CPU、内存与显卡，供资源预算与运行环境建议使用；并向系统登记本进程的常驻内存下限。仅依赖标准库。"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
from uuid import UUID

from ..core.contracts.inference import GraphicsAdapter, MemoryStatus, SystemProfile

logger = logging.getLogger(__name__)

_SOFTWARE_ADAPTER = 2
_FEATURE_LEVEL_11_0 = 0xB000


def available_logical_count():
    logical = getattr(os, "process_cpu_count", os.cpu_count)() or 1
    if sys.platform != "win32":
        return logical
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.GetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                                                  ctypes.POINTER(ctypes.c_size_t)]
        kernel.GetProcessAffinityMask.restype = ctypes.c_int
        process, system = ctypes.c_size_t(), ctypes.c_size_t()
        if not kernel.GetProcessAffinityMask(kernel.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(system)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not process.value:
            raise ValueError("当前处理器组未返回亲和性掩码")
        return min(logical, process.value.bit_count())
    except (OSError, AttributeError, ValueError):
        logger.warning("可用处理器查询失败，采用系统逻辑核数 %s", logical, exc_info=True)
        return logical


def physical_core_count(logical_threads=None):
    logical_threads = logical_threads or available_logical_count()
    try:
        if sys.platform != "win32":
            raise OSError("当前平台无 Windows 处理器拓扑接口")
        query = ctypes.WinDLL("kernel32", use_last_error=True).GetLogicalProcessorInformationEx
        query.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        query.restype = ctypes.c_int
        size = ctypes.c_uint32()
        if query(0, None, ctypes.byref(size)) or ctypes.get_last_error() != 122 or size.value < 8:
            raise OSError("处理器拓扑缓冲区查询失败")
        buffer = ctypes.create_string_buffer(size.value)
        if not query(0, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        offset, cores = 0, 0
        while offset < size.value:
            if offset + 8 > size.value:
                raise ValueError("处理器拓扑记录不完整")
            relationship = ctypes.c_uint32.from_buffer(buffer, offset).value
            length = ctypes.c_uint32.from_buffer(buffer, offset + 4).value
            if length < 8 or offset + length > size.value:
                raise ValueError("处理器拓扑记录长度无效")
            cores += relationship == 0
            offset += length
        if not cores:
            raise ValueError("处理器拓扑未返回物理核心")
        return cores
    except (OSError, AttributeError, ValueError):
        fallback = max(1, logical_threads // 2)
        logger.warning("物理核数探测失败，采用回退值 %s", fallback, exc_info=True)
        return fallback


class _GUID(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16), ("data3", ctypes.c_uint16),
                ("data4", ctypes.c_ubyte * 8)]

    @classmethod
    def from_string(cls, value):
        return cls.from_buffer_copy(UUID(value).bytes_le)


class _AdapterDescription(ctypes.Structure):
    _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", ctypes.c_uint), ("DeviceId", ctypes.c_uint),
                ("SubSysId", ctypes.c_uint), ("Revision", ctypes.c_uint), ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t), ("SharedSystemMemory", ctypes.c_size_t),
                ("LuidLow", ctypes.c_uint32), ("LuidHigh", ctypes.c_int32), ("Flags", ctypes.c_uint)]


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32)] + [
        (name, ctypes.c_uint64) for name in ("ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
                                             "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]


def memory_status():
    """读取此刻的物理内存总量与可用量；读不到时返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatus)]
        kernel.GlobalMemoryStatusEx.restype = ctypes.c_int
        memory = _MemoryStatus()
        memory.dwLength = ctypes.sizeof(_MemoryStatus)
        if not kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
            return None
    except (OSError, AttributeError):
        return None
    return MemoryStatus(int(memory.ullTotalPhys), int(memory.ullAvailPhys))


def available_memory():
    """当前可用的物理内存（字节）；读不到时返回 None。"""
    status = memory_status()
    return status.available_bytes if status is not None else None


_HARD_MINIMUM_ENABLE, _HARD_MINIMUM_DISABLE, _HARD_MAXIMUM_DISABLE = 1, 2, 8
# 系统默认的常驻内存下限：50 页
_DEFAULT_WORKING_SET_MINIMUM = 50 * 4096


def set_working_set_minimum(size):
    """向系统登记本进程常驻内存的下限（字节）；size 为 0 时恢复系统默认。返回系统是否批准。

    登记按先到先得；批准后，本进程已经用上的内存在下限以内不会被换出，没用到的部分别的程序照常使用。
    每次调用都会把常驻内存收到上限参数以内，所以上限参数固定取接近物理内存的值并保持软性，
    不限制实际用量，也不会在调整下限时把正在用的内存收走。
    """
    status = memory_status()
    if status is None:
        return False
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetProcessWorkingSetSizeEx.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t,
                                                      ctypes.c_uint32]
        kernel.SetProcessWorkingSetSizeEx.restype = ctypes.c_int
        minimum, flag = (size, _HARD_MINIMUM_ENABLE) if size else (_DEFAULT_WORKING_SET_MINIMUM, _HARD_MINIMUM_DISABLE)
        if kernel.SetProcessWorkingSetSizeEx(kernel.GetCurrentProcess(), minimum,
                                             status.total_bytes - status.total_bytes // 16,
                                             flag | _HARD_MAXIMUM_DISABLE):
            return True
        logger.warning("常驻内存下限登记被拒绝 size=%s error=%s", size, ctypes.get_last_error())
    except (OSError, AttributeError):
        logger.warning("常驻内存下限登记失败 size=%s", size, exc_info=True)
    return False


def _method(pointer, index, *argtypes):
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, *argtypes)(vtable[index])


def _adapters():
    dxgi = ctypes.WinDLL("dxgi")
    dxgi.CreateDXGIFactory1.argtypes = [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    dxgi.CreateDXGIFactory1.restype = ctypes.c_int32
    try:
        d3d12 = ctypes.WinDLL("d3d12")
        d3d12.D3D12CreateDevice.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(_GUID), ctypes.c_void_p]
        d3d12.D3D12CreateDevice.restype = ctypes.c_int32
    except OSError:
        d3d12 = None
    factory = ctypes.c_void_p()
    result = dxgi.CreateDXGIFactory1(ctypes.byref(_GUID.from_string("770aae78-f26f-4dba-a829-253c83d1b387")),
                                     ctypes.byref(factory))
    if result < 0:
        raise OSError(f"CreateDXGIFactory1: HRESULT 0x{result & 0xFFFFFFFF:08X}")
    device_iid = _GUID.from_string("189819f1-1db6-4b57-be54-1821339b85f7")
    found = {}
    try:
        index = 0
        while True:
            adapter = ctypes.c_void_p()
            if _method(factory, 12, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(
                    factory, index, ctypes.byref(adapter)) < 0:
                break
            index += 1
            try:
                description = _AdapterDescription()
                if _method(adapter, 10, ctypes.POINTER(_AdapterDescription))(adapter, ctypes.byref(description)) < 0:
                    continue
                identity = (description.LuidHigh, description.LuidLow)
                if description.Flags & _SOFTWARE_ADAPTER or identity in found:
                    continue
                directx12 = d3d12 is not None and d3d12.D3D12CreateDevice(
                    adapter, _FEATURE_LEVEL_11_0, ctypes.byref(device_iid), None) >= 0
                found[identity] = GraphicsAdapter(description.Description, description.VendorId,
                                                  description.DedicatedVideoMemory, bool(directx12))
            finally:
                _method(adapter, 2)(adapter)
    finally:
        _method(factory, 2)(factory)
    # 同一块显卡可能以不同适配器标识出现多次，按名称与显存再合并一次。
    return tuple({(item.name, item.dedicated_bytes): item for item in found.values()}.values())


def detect_profile():
    if sys.platform != "win32":
        return SystemProfile(error="runtime.profile.unsupported")
    try:
        import winreg
        memory = memory_status()
        if memory is None:
            raise OSError("GlobalMemoryStatusEx")
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            cpu = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        logical = os.cpu_count() or 0
        return SystemProfile(cpu, logical, memory.total_bytes, _adapters(),
                             physical_cores=physical_core_count(logical or None))
    except (OSError, AttributeError, ValueError):
        logger.exception("本机配置检测失败")
        return SystemProfile(error="runtime.profile.failed")
