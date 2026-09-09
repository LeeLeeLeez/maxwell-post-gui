# -*- coding: utf-8 -*-
"""
AEDT 连接公共模块 —— 所有脚本统一从这里取连接，避免重复踩环境变量的坑。

用法:
    from aedt_env import connect, disconnect
    d = connect()                       # 连本机 AEDT 2026 R1 的 gRPC :50051
    ...
    disconnect(d)                       # 断开但保留 AEDT 窗口

本机背景（务必保留这段注释）:
    WorkBuddy 进程是在卸载 AEDT 2023 R1 / 安装 2026 R1 **之前**启动的，
    所以进程内存里残留了三个过期变量（注册表里其实并没有）:
        ANSYSEM_ROOT231 / SIMPLORER_HOME / SIWAVE_INSTALL_DIR
        = D:\\Program Files\\AnsysEM\\v231\\Win64     ← 该目录已被删除
    PyAEDT 完全依赖 ANSYSEM_ROOTxxx / AWP_ROOTxxx 发现 AEDT 安装，
    扫到 ANSYSEM_ROOT231 就会误判版本为 2023.1，
    进而拼出不存在的 D:\\Program Files\\build_output\\64Release\\PyDesktopPlugin.dll 而崩溃。

    系统级 ANSYSEM_ROOT261 安装程序已正确设置，重启 WorkBuddy 后本模块即为无操作。
    保留 fix_env() 只是为了在旧进程里也能跑，属双保险。
"""
import os

AEDT_VERSION = "2026.1"
AEDT_ROOT = r"D:\Program Files\ANSYS Inc\v261\AnsysEM"
GRPC_PORT = 50051

# 指向已删除目录的旧版残留变量
_STALE_ENV = ["ANSYSEM_ROOT231", "SIMPLORER_HOME", "SIWAVE_INSTALL_DIR"]


def fix_env(verbose=False):
    """清除旧版残留环境变量，并设置本机的 2026 R1 根目录。"""
    for k in _STALE_ENV:
        if k in os.environ:
            v = os.environ.pop(k)
            if verbose:
                print("清除残留环境变量: %s = %s" % (k, v))
    os.environ.setdefault("ANSYSEM_ROOT261", AEDT_ROOT)


def connect(version=AEDT_VERSION, port=GRPC_PORT, graphical=True):
    """
    连接已运行的 AEDT 实例。
    close_on_exit=False 在构造时锁死 —— 无论如何退出都不会关掉 AEDT 窗口。
    """
    fix_env()
    # 坑 #45 加固（2026-09-04）：先探测 gRPC 端口，没监听就明确报错，
    # 绝不让 PyAEDT 自己拉起新的 AEDT 窗口（用户遇到过"扫描打开新窗口"）。
    import socket
    _s = socket.socket()
    _s.settimeout(2)
    try:
        _s.connect(("127.0.0.1", int(port)))
    except Exception:
        raise RuntimeError(
            "gRPC 端口 %s 没有监听 —— AEDT 未启动或未开启 gRPC 服务。\n"
            "请先打开 AEDT 并载入工程、激活设计后再运行；"
            "脚本不会自动拉起新的 AEDT 窗口。" % port)
    finally:
        _s.close()
    from ansys.aedt.core import Desktop
    return Desktop(version=version, new_desktop=False, port=port,
                   non_graphical=not graphical, close_on_exit=False)


def disconnect(d):
    """断开连接，保留 AEDT 窗口与工程。"""
    if d is None:
        return
    try:
        d.release_desktop(close_on_exit=False, close_projects=False)
    except Exception:
        pass


def val(obj, name, *args):
    """PyAEDT 1.4.0 里很多成员是属性而非方法，统一取值。"""
    v = getattr(obj, name)
    return v(*args) if callable(v) else v


def get_design(proj, design=""):
    """拿 odesign，**绕开坑 #42**，替代 `proj.GetActiveDesign()`。

    坑 #42 实测（2026-09-02）：
      `proj.GetActiveDesign()` 在 gRPC 下**不可靠** —— 同一个工程、同一份代码，
      有时通有时报 `GrpcApiError: Failed to execute gRPC AEDT command: GetActiveDesign`。
      它跟"是不是激活工程""有没有调用过 SetActiveProject"**都不完全对应**，
      机制未查明，规律不可依赖（曾观察到：A4 激活时失败、Winding_Sim 未激活时反而成功；
      过一段时间又反过来）。**结论：别用它。**

    可靠的两个 API（对任意工程、任意设计都 100% 通）：
      * `proj.GetChildNames()`      -> 该工程所有设计名
      * `proj.GetChildObject(name)` -> 直接取 odesign（可 `.GetName()` /
        `.GetModule(...)` / `.SetActiveEditor(...)`）

    策略：
      1. 显式给了 design 名 -> 直接 `GetChildObject`；
      2. 否则先试 `GetActiveDesign()`（能用就用，它给的是"用户当前在看的设计"）；
      3. 失败且工程只有 1 个设计 -> 就用那一个，无歧义；
      4. 失败且有多个设计 -> **抛错并列出候选**，绝不猜（猜错会作用到错误设计上）。
    """
    kids = [str(x) for x in proj.GetChildNames()]
    if design:
        if design not in kids:
            raise RuntimeError(
                "设计 '%s' 不存在（该工程现有: %s）。"
                "注意 Maxwell3d(design=<不存在的名字>) 会静默新建设计（坑 #43）。"
                % (design, ", ".join(kids)))
        return proj.GetChildObject(design)
    try:
        return proj.GetActiveDesign()
    except Exception:
        pass
    if len(kids) == 1:
        return proj.GetChildObject(kids[0])
    raise RuntimeError(
        "GetActiveDesign 不可用，且工程 '%s' 有 %d 个设计，无法确定用哪个；"
        "请显式传 design。候选: %s"
        % (str(proj.GetName()), len(kids), ", ".join(kids)))


def open_design(project="", design="", version=AEDT_VERSION, port=GRPC_PORT,
                kind="Maxwell3d"):
    """可靠地拿到 (Desktop, app)，绕开坑 #42。

    坑 #42（2026-09-02 实测）：`project.GetActiveDesign()` 在 gRPC 下时通时断，
    会报 GrpcApiError: Failed to execute gRPC AEDT command: GetActiveDesign。
    与"是否激活工程/是否调用过 SetActiveProject"都不对应，机制未明 —— 别依赖它。
    ⚠️ 本函数的目标**仍然是用户当前激活的设计**，只是取的方式更可靠，不改变操作对象。

    策略：
      1. 先试一次 GetActiveDesign 拿"真正的当前设计"；
      2. 失败则退回 `Maxwell3d(project=..., design=None)` 自动挑的那个，
         调用方务必**把实际打开的设计名打印出来**让用户核对。

    多设计工程（如 Winding_Sim 有 interleave / interleaveedit / ...）请显式传
    design，否则自动挑的可能不是你要的那个。
    """
    from ansys.aedt.core import Desktop

    d = Desktop(version=version, new_desktop=False, port=port,
                close_on_exit=False)
    projs = {str(p.GetName()): p for p in d.odesktop.GetProjects()}
    if project and project not in projs:
        raise RuntimeError("工程 '%s' 未打开（现有: %s）"
                           % (project, ", ".join(projs)))
    if not project:
        project = str(d.odesktop.GetActiveProject().GetName())
    pr = projs[project]

    want = design
    if not want:
        # ⚠️ 必须从**目标工程**取，不能从激活工程取（否则会把别的工程的设计名
        #    套到本工程上，配合坑 #43 直接新建一个空设计 —— 2026-09-02 真发生过）
        try:
            want = str(get_design(pr).GetName())
        except Exception:
            want = None                      # 多设计且无法确定，交给 Maxwell3d 自动挑

    # 坑 #43：Maxwell3d(design=<不存在的名字>) 会**静默新建**那个设计，不报错！
    kids = [str(x) for x in pr.GetChildNames()]
    if want and want not in kids:
        raise RuntimeError(
            "设计 '%s' 在工程 '%s' 中不存在（现有: %s）。"
            "注意 PyAEDT 会静默新建设计，已阻止。" % (want, project, ", ".join(kids)))

    import ansys.aedt.core as _core
    cls = getattr(_core, kind)
    try:
        app = cls(project=project, design=want or None, version=version,
                  port=port, new_desktop=False, close_on_exit=False)
    except Exception:
        if not want:
            raise
        app = cls(project=project, design=None, version=version, port=port,
                  new_desktop=False, close_on_exit=False)
    return d, app
