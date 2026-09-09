# -*- coding: utf-8 -*-
"""
aedt_gui.py —— Maxwell 绕组后处理 GUI（剖面电流积分 / OhmicLoss 积分）

运行环境：系统 Python 3.12（带 tkinter），**不需要** PyAEDT。
          所有 AEDT 操作以子进程方式调用 venv 里已验证过的脚本：
            扫描 / OhmicLoss  ->  aedt_gui_backend.py
            电流积分    ->  current_integral_pipeline.py

铁律（与 skill 一致）:
  1. 只操作【用户当前激活的设计】，不切工程、不切坐标系
  2. 几何类操作（剖面）必须 Non-model，否则结果文件会被清空
  3. 保存前后比对结果目录大小指纹，异常就拒绝保存

启动:
    python aedt_gui.py          # 或用 pythonw.exe 静默启动
    或双击 run_aedt_gui.bat（Windows）
    或双击 run_aedt_gui.bat
"""
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog

# 路径解析：脚本模式 vs PyInstaller 冻结模式
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    _RES = getattr(sys, "_MEIPASS", APP_DIR)   # 打包后后端脚本解压目录
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    _RES = APP_DIR

WORKDIR = APP_DIR   # 子进程 cwd / 配置文件所在目录

def _backend(name):
    """子进程要执行的脚本路径。

    冻结模式下**不能**直接用 sys._MEIPASS 里的副本：Python 会把「脚本所在目录」
    塞进 sys.path[0]，而 _MEI 临时目录里混着打包时那个 Python 的 .pyd；
    子进程若是另一个版本的解释器，import 扩展模块就会拿到版本不符的那个，报
    "Module use of python312.dll conflicts with this version of Python"。
    → 优先用 exe 旁边真实存在的 .py；旁边没有就拷一份到干净的临时目录。
    """
    local = os.path.join(APP_DIR, name)
    if os.path.isfile(local):
        return local
    if getattr(sys, "frozen", False):
        import shutil, tempfile
        td = os.path.join(tempfile.gettempdir(), "maxwell_post_run")
        try:
            os.makedirs(td, exist_ok=True)
            dst = os.path.join(td, name)
            shutil.copy2(os.path.join(_RES, name), dst)
            return dst
        except Exception:
            pass
    return os.path.join(_RES, name)


BACKEND = _backend("aedt_gui_backend.py")
PIPELINE = _backend("current_integral_pipeline.py")

# matrix等效 tab 依赖: indcalc_core (纯 numpy 集总计算, 无 AEDT 依赖)。
# 优先用 GUI 同目录副本; 缺失时回退 maxwell_matrix 原目录。
# 回退目录：默认关闭。若 indcalc_core.py 不在 GUI 同目录，
# 用环境变量 MAXWELL_MATRIX_CORE_DIR 指向其所在目录。
# （不要把个人/公司的绝对路径写死进源码）
MX_CORE_DIR = os.environ.get("MAXWELL_MATRIX_CORE_DIR", "")
try:
    import indcalc_core as _icc
except ImportError:
    _icc = None
    if os.path.isdir(MX_CORE_DIR):
        if MX_CORE_DIR not in sys.path:
            sys.path.insert(0, MX_CORE_DIR)
        try:
            import indcalc_core as _icc
        except ImportError:
            pass
GRPC_PORT = "50051"

CONFIG_FILE = os.path.join(APP_DIR, "aedt_gui_config.json")

def load_cfg():
    """读取配置；首运行返回默认（venv python 相对 exe 目录）。"""
    default_py = os.path.join(APP_DIR, "env", "Scripts", "python.exe")
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and d.get("python"):
                return d
        except Exception:
            pass
    return {"python": default_py}

def save_cfg(d):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False

# 剖切面：界面显示 -> AEDT 平面名（XZ 面在脚本里叫 ZX）
PLANES = [("XZ（法向 Y）", "ZX"), ("YZ（法向 X）", "YZ"), ("XY（法向 Z）", "XY")]

# ---------- 主题：现代浅色（风格参考 png2bar_chat，基于 ttk 自带 clam 深度定制） ----------
PALETTE = {
    "bg": "#F5F7FA",           # 窗口底色
    "surface": "#FFFFFF",      # 卡片 / 输入框
    "border": "#E1E7EF",       # 边框 / 分隔线
    "text": "#1F2937",         # 主文字
    "muted": "#6B7280",        # 次要文字
    "accent": "#2563EB",       # 主色
    "accent_hover": "#1D4ED8",
    "accent_soft": "#E8F0FE",  # 选中底色
    "danger": "#EF4444",
    "ok": "#16A34A",
}

FONT_FAMILY = "Microsoft YaHei UI"
SCALE = 1.0                     # 高 DPI 缩放系数（main() 里探测）


def _enable_hidpi():
    """开启 Windows 高 DPI 感知，返回相对 96dpi 的缩放系数。

    必须在创建任何 Tk 窗口之前调用，否则不生效。
    """
    if sys.platform != "win32":
        return 1.0
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
        hdc = ctypes.windll.user32.GetDC(0)
        try:
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)   # LOGPIXELSX
        finally:
            ctypes.windll.user32.ReleaseDC(0, hdc)
        if dpi:
            return max(1.0, dpi / 96.0)
    except Exception:
        pass
    return 1.0


def _pick_font_family(root):
    """挑一个既有中文、风格又现代的系统字体。"""
    try:
        from tkinter import font as tkfont
        avail = {f.lower() for f in tkfont.families(root)}
        for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"):
            if name.lower() in avail:
                return name
        return tkfont.nametofont("TkDefaultFont").actual()["family"]
    except Exception:
        return "Microsoft YaHei UI"


def setup_style(root):
    """配置现代浅色主题（基于 clam）。"""
    global FONT_FAMILY
    FONT_FAMILY = _pick_font_family(root)
    p = PALETTE
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")

    # 关掉 Windows Text 控件的拼写检查红波浪线
    try:
        root.tk.createcommand("tk_spellcheck", lambda *args: "")
    except Exception:
        pass

    f_normal = (FONT_FAMILY, 9)
    f_bold = (FONT_FAMILY, 9, "bold")

    style.configure(".", background=p["bg"], foreground=p["text"],
                    font=f_normal, borderwidth=0, troughcolor=p["bg"])
    # 本工具的内容基本都在白卡片/白页里，所以默认底色取 surface
    style.configure("TFrame", background=p["surface"])
    style.configure("Card.TFrame", background=p["surface"])
    style.configure("TSeparator", background=p["border"])

    # 文字
    style.configure("TLabel", background=p["surface"], foreground=p["text"])
    style.configure("Card.TLabel", background=p["surface"], foreground=p["text"])
    style.configure("Muted.TLabel", background=p["surface"],
                    foreground=p["muted"])
    style.configure("Title.TLabel", background=p["surface"],
                    foreground=p["text"], font=(FONT_FAMILY, 13, "bold"))
    style.configure("Status.TLabel", background=p["surface"],
                    foreground=p["muted"])

    # 卡片式分组框
    style.configure("Card.TLabelframe", background=p["surface"],
                    bordercolor=p["border"], borderwidth=1)
    style.configure("Card.TLabelframe.Label", background=p["surface"],
                    foreground=p["muted"], font=f_bold)

    # 标签页
    style.configure("TNotebook", background=p["bg"], borderwidth=0,
                    tabmargins=(2, 8, 2, 0))
    style.configure("TNotebook.Tab", background=p["bg"], foreground=p["muted"],
                    padding=(16, 7), bordercolor=p["border"], font=f_normal)
    style.map("TNotebook.Tab",
              background=[("selected", p["surface"]), ("active", "#EEF2F8")],
              foreground=[("selected", p["accent"])],
              bordercolor=[("selected", p["border"])])

    # 按钮
    style.configure("TButton", background=p["surface"], foreground=p["text"],
                    bordercolor=p["border"], lightcolor=p["surface"],
                    darkcolor=p["surface"], padding=(12, 6))
    style.map("TButton",
              background=[("active", "#F1F5FB"), ("disabled", p["surface"])],
              bordercolor=[("active", "#C7D2E4"), ("focus", p["accent"])],
              foreground=[("disabled", "#9AA3AF")])
    style.configure("Accent.TButton", background=p["accent"],
                    foreground="#FFFFFF", bordercolor=p["accent"],
                    lightcolor=p["accent"], darkcolor=p["accent"],
                    padding=(16, 7), font=f_bold)
    style.map("Accent.TButton",
              background=[("active", p["accent_hover"]), ("disabled", "#A5B4FC")],
              bordercolor=[("active", p["accent_hover"])])
    style.configure("Danger.TButton", background=p["surface"],
                    foreground=p["danger"], bordercolor=p["danger"],
                    lightcolor=p["surface"], darkcolor=p["surface"],
                    padding=(14, 6), font=f_bold)
    style.map("Danger.TButton",
              background=[("active", "#FEF2F2"), ("disabled", p["surface"])],
              foreground=[("disabled", "#9AA3AF")])

    # 复选框
    style.configure("TCheckbutton", background=p["surface"],
                    foreground=p["text"], padding=(4, 4),
                    indicatorbackground=p["surface"],
                    indicatorcolor=p["surface"],
                    indicatordiameter=12, bordercolor=p["border"],
                    focuscolor=p["accent_soft"])
    style.map("TCheckbutton",
              background=[("active", p["surface"])],
              indicatorcolor=[("selected", p["accent"]),
                              ("!selected", p["surface"])],
              foreground=[("disabled", "#9AA3AF")])
    style.configure("TRadiobutton", background=p["surface"],
                    foreground=p["text"], padding=(4, 4),
                    indicatorbackground=p["surface"],
                    indicatorcolor=p["surface"],
                    indicatordiameter=12, bordercolor=p["border"])
    style.map("TRadiobutton",
              background=[("active", p["surface"])],
              indicatorcolor=[("selected", p["accent"]),
                              ("!selected", p["surface"])])

    # 输入
    style.configure("TEntry", fieldbackground=p["surface"],
                    foreground=p["text"], bordercolor=p["border"],
                    lightcolor=p["surface"], darkcolor=p["surface"],
                    padding=(8, 5))
    style.map("TEntry", bordercolor=[("focus", p["accent"])])
    style.configure("TCombobox", fieldbackground=p["surface"],
                    foreground=p["text"], bordercolor=p["border"],
                    lightcolor=p["surface"], darkcolor=p["surface"],
                    padding=(7, 5), arrowsize=14)
    style.map("TCombobox",
              fieldbackground=[("readonly", p["surface"]),
                               ("disabled", "#F1F3F7")],
              foreground=[("readonly", p["text"])],
              bordercolor=[("focus", p["accent"])],
              arrowcolor=[("disabled", "#9AA3AF")])

    # 滚动条（细、浅色）
    style.configure("TScrollbar", background="#CBD5E1",
                    troughcolor=p["bg"], bordercolor=p["bg"],
                    arrowcolor=p["muted"], arrowsize=13, borderwidth=0)
    style.map("TScrollbar",
              background=[("active", "#B6C2D2"), ("pressed", "#94A3B8")])
    style.configure("Vertical.TScrollbar", width=10)
    style.configure("Horizontal.TScrollbar", width=10)


def listbox_kw():
    """tk.Listbox 的现代外观参数（ttk 没有 Listbox）。"""
    p = PALETTE
    return dict(bg=p["surface"], fg=p["text"], font=(FONT_FAMILY, 9),
                relief="flat", borderwidth=0, highlightthickness=1,
                highlightcolor=p["accent"], highlightbackground=p["border"],
                selectbackground=p["accent_soft"],
                selectforeground=p["text"],
                activestyle="none", exportselection=False)


def text_kw():
    """tk.Text 的现代外观参数。"""
    p = PALETTE
    return dict(bg=p["surface"], fg=p["text"], font=(FONT_FAMILY, 9),
                relief="flat", borderwidth=0, highlightthickness=1,
                highlightcolor=p["accent"], highlightbackground=p["border"],
                selectbackground=p["accent_soft"],
                selectforeground=p["text"],
                insertwidth=2, padx=6, pady=4, spacing1=1)



# ------------------------------------------------------------------ 执行器
class Runner:
    """后台跑子进程，输出经队列送回主线程，避免界面卡死。"""

    def __init__(self, log, on_done, on_state, root=None):
        self.log = log
        self.on_done = on_done
        self.on_state = on_state
        self.root = root
        self.q = queue.Queue()
        self.proc = None
        self.th = None
        self.running = False

    def _env(self):
        e = dict(os.environ)
        # 关键：清掉外部环境（如 WorkBuddy 沙箱）可能注入的 PYTHON* 变量。
        # 否则后端子进程会继承到与本机 python 不一致的 stdlib 路径，
        # 触发 "python312.dll conflicts" 之类的 ABI 冲突。
        for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
                  "PYTHONNOUSERSITE", "PYTHONUSERBASE"):
            e.pop(k, None)
        if getattr(sys, "frozen", False):
            # 保险: 剥掉 PyInstaller 塞进 PATH 的临时解压目录(_MEIxxx/_internal)
            e["PATH"] = os.pathsep.join(
                p for p in e.get("PATH", "").split(os.pathsep)
                if "_MEI" not in p and os.path.basename(p) != "_internal")
        e["PYTHONIOENCODING"] = "utf-8"
        e["PYTHONDONTWRITEBYTECODE"] = "1"
        return e

    def start(self, argv, tag=""):
        if self.running:
            self.log("!! 上一个后台任务仍在运行，本次点击已被忽略——"
                     "点顶部【中止】结束旧任务后再试", "err")
            return
        self.running = True
        self.on_state(True)
        self.log("\n$ %s\n" % " ".join(os.path.basename(a) if i == 0 else a
                                       for i, a in enumerate(argv)), "cmd")
        try:
            self.proc = subprocess.Popen(
                argv, cwd=WORKDIR, env=self._env(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
        except Exception as ex:
            self.log("!! 启动失败: %s" % ex, "err")
            self.running = False
            self.on_state(False)
            return

        def reader():
            try:
                for line in self.proc.stdout:
                    self.q.put(line.rstrip("\r\n"))
            except Exception as ex:
                self.q.put("!! 读取输出失败: %s" % ex)
            self.proc.wait()
            self.q.put(("__EXIT__", self.proc.returncode))

        self.th = threading.Thread(target=reader, daemon=True)
        self.th.start()
        self._poll(tag)

    def _poll(self, tag):
        done = False
        code = 0
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple) and item[0] == "__EXIT__":
                    done, code = True, item[1]
                    break
                self.log(item, "err" if (item.startswith("!!")
                                         or "FAIL" in item or "ERR" in item)
                         else ("ok" if "[OK]" in item else ""))
        except queue.Empty:
            pass
        if done:
            self.running = False
            self.on_state(False)
            self.log("---- 结束（退出码 %d）----" % code,
                     "err" if code else "ok")
            self.on_done(tag, code)
        else:
            (self.root or tk._default_root).after(90, lambda: self._poll(tag))

    def stop(self):
        if self.proc and self.running:
            try:
                self.proc.terminate()
                self.log("!! 已请求中止", "err")
            except Exception as ex:
                self.log("!! 中止失败: %s" % ex, "err")


# ------------------------------------------------------------- 入栈/出栈面板
class StackPanel(ttk.Frame):
    """左边候选池 + 中间按钮 + 右边栈（有序）。支持过滤、多选、双击。"""

    def __init__(self, master, left_title="候选", right_title="已入栈",
                 on_change=None, show_cu=True, prefix_values=None):
        ttk.Frame.__init__(self, master)
        self.on_change = on_change
        self.show_cu = show_cu
        self.prefix_values = list(prefix_values or [])
        self.pool_all = []
        self.pool_copper = []

        # 过滤
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(2, 4))
        ttk.Label(top, text="过滤:").pack(side="left")
        self.fvar = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.fvar, width=18)
        e.pack(side="left", padx=4)
        e.bind("<KeyRelease>", lambda _e: self._refill())
        if self.show_cu:
            self.cuvar = tk.BooleanVar(value=True)
            ttk.Checkbutton(top, text="只看 copper", variable=self.cuvar,
                            command=self._refill).pack(side="left", padx=6)
        else:
            self.cuvar = tk.BooleanVar(value=False)
        if self.prefix_values:
            ttk.Label(top, text="前缀:").pack(side="left", padx=(10, 2))
            self.pvar = tk.StringVar(value="全部")
            cb = ttk.Combobox(top, textvariable=self.pvar, width=13,
                              state="readonly",
                              values=["全部"] + self.prefix_values + ["其它"])
            cb.pack(side="left")
            cb.bind("<<ComboboxSelected>>", lambda _e: self._refill())
        else:
            self.pvar = None
        ttk.Button(top, text="清空过滤", width=9,
                   command=lambda: (self.fvar.set(""), self._refill())
                   ).pack(side="left", padx=6)

        body = tk.PanedWindow(self, orient=tk.HORIZONTAL, sashwidth=6, sashrelief="flat",
                            bg=PALETTE["bg"], bd=0)
        body.pack(fill="both", expand=True)

        # 左
        lf = ttk.Frame(body)
        body.add(lf, minsize=150, stretch="always")
        self.llab = ttk.Label(lf, text=left_title)
        self.llab.pack(anchor="w")
        self.lbox = tk.Listbox(lf, selectmode=tk.EXTENDED, height=12,
                               **listbox_kw())
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.lbox.yview)
        lsb2 = ttk.Scrollbar(lf, orient="horizontal", command=self.lbox.xview)
        self.lbox.configure(yscrollcommand=lsb.set, xscrollcommand=lsb2.set)
        self.lbox.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        lsb2.pack(side="bottom", fill="x")
        self.lbox.bind("<Double-Button-1>", lambda _e: self.push())

        # 中
        mid = ttk.Frame(body, width=86)
        body.add(mid, minsize=86, stretch="never")
        midp = ttk.Frame(mid)
        midp.pack(fill="both", expand=True, padx=4)
        ttk.Button(midp, text="入栈 >>", command=self.push).pack(fill="x", pady=3)
        ttk.Button(midp, text="<< 出栈", command=self.pop).pack(fill="x", pady=3)
        ttk.Button(midp, text="全部入栈", command=self.push_all).pack(fill="x", pady=3)
        ttk.Button(midp, text="清空栈", command=self.clear).pack(fill="x", pady=3)

        # 右
        rf = ttk.Frame(body)
        body.add(rf, minsize=150, stretch="always")
        self.rlab = ttk.Label(rf, text=right_title)
        self.rlab.pack(anchor="w")
        self.rbox = tk.Listbox(rf, selectmode=tk.EXTENDED, height=12,
                               **listbox_kw())
        rsb = ttk.Scrollbar(rf, orient="vertical", command=self.rbox.yview)
        rsb2 = ttk.Scrollbar(rf, orient="horizontal", command=self.rbox.xview)
        self.rbox.configure(yscrollcommand=rsb.set, xscrollcommand=rsb2.set)
        self.rbox.pack(side="left", fill="both", expand=True)
        rsb.pack(side="right", fill="y")
        rsb2.pack(side="bottom", fill="x")
        self.rbox.bind("<Double-Button-1>", lambda _e: self.pop())

    # ---------------- 数据
    def set_pool(self, copper, allobjs=None):
        self.pool_copper = list(copper)
        self.pool_all = list(allobjs) if allobjs is not None else list(copper)
        self._refill()

    def _refill(self):
        src = self.pool_copper if (self.show_cu and self.cuvar.get()) \
            else self.pool_all
        kw = self.fvar.get().strip().lower()
        pre = self.pvar.get() if self.pvar is not None else "全部"
        out = []
        for x in src:
            nm = x.split("=")[0].strip()
            if self.prefix_values and pre != "全部":
                if pre == "其它":
                    if any(nm.startswith(p) for p in self.prefix_values):
                        continue
                elif not nm.startswith(pre):
                    continue
            if kw and kw not in x.lower():
                continue
            out.append(x)
        src = out
        self.lbox.delete(0, tk.END)
        for x in src:
            self.lbox.insert(tk.END, x)
        self.llab.configure(text="候选（%d）" % self.lbox.size())
        self._refresh()

    @property
    def stack(self):
        return list(self.rbox.get(0, tk.END))

    def _refresh(self):
        self.rlab.configure(text="已入栈（%d）" % self.rbox.size())
        if self.on_change:
            self.on_change()

    # ---------------- 操作
    def push(self):
        have = set(self.stack)
        for i in self.lbox.curselection():
            v = self.lbox.get(i)
            if v not in have:
                self.rbox.insert(tk.END, v)
                have.add(v)
        self._refresh()

    def push_all(self):
        have = set(self.stack)
        for i in range(self.lbox.size()):
            v = self.lbox.get(i)
            if v not in have:
                self.rbox.insert(tk.END, v)
                have.add(v)
        self._refresh()

    def pop(self):
        sel = list(self.rbox.curselection())
        if sel:                       # 有选中 -> 删除选中的
            for i in reversed(sel):
                self.rbox.delete(i)
        elif self.rbox.size():        # 没选中 -> 栈顶（最后入栈的）出栈
            self.rbox.delete(tk.END)
        self._refresh()

    def clear(self):
        self.rbox.delete(0, tk.END)
        self._refresh()


# ------------------------------------------------------------------- 主界面
# ------------------------------------------------------------------ matrix等效
MX_DEMO_MATRIX = (
    "LWindingA_1, 1.412u, 1.181u, 1.101u, 1.052u, 1.020u, 0.995u\n"
    "LWindingA_2, 1.181u, 1.459u, 1.133u, 1.074u, 1.041u, 1.015u\n"
    "LWindingA_3, 1.101u, 1.133u, 1.466u, 1.122u, 1.088u, 1.062u\n"
    "LWindingA_4, 1.052u, 1.074u, 1.122u, 1.497u, 1.055u, 1.031u\n"
    "LWindingA_5, 1.020u, 1.041u, 1.088u, 1.055u, 1.503u, 1.118u\n"
    "LWindingA_6, 0.995u, 1.015u, 1.062u, 1.031u, 1.118u, 1.488u")

MX_DEMO_PORTS = (
    "# P_Pri = (A1串A2) ∥ A3\n"
    "@P_Pri 串联\n"
    "S: LWindingA_1, LWindingA_2 | LWindingA_3\n"
    "@P_Sec1 串联\n"
    "LWindingA_4\n"
    "@P_Sec2 串联\n"
    "S: LWindingA_5, LWindingA_6\n")


def mx_parse_port_blocks(text):
    """解析「matrix等效」tab 的端口定义文本 -> [{'name','chain_mode','segments'}]。

    语法 (兼容 indcalc_core.parse_segment_line, 在其上扩展 '|' 支路分隔):
      @端口名 [串联|并联]   开一个端口块; 第二词 = 段间连接方式, 默认串联
      其余行 = 段定义:
        '[S:]A1, A2'      段内串联 (裸行默认串联)
        '[P:]A1, A2'      段内并联 (每层单支路)
        'S: A1, A2 | A3'  '|' 分隔并联支路 -> (A1串A2) ∥ A3
        层名前 '-'        该层反接 (同名端对调)
        '#' 注释行, 空行忽略
    """
    import re as _re  # noqa: F401  (保留给未来扩展)
    ports, cur, lineno = [], None, 0
    for raw in text.splitlines():
        lineno += 1
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            toks = line[1:].split()
            if not toks:
                raise ValueError("第 %d 行: @ 后缺少端口名" % lineno)
            chain = "series"
            if len(toks) > 1 and toks[1].lower() in ("并联", "parallel", "p"):
                chain = "parallel"
            cur = {"name": toks[0], "chain_mode": chain, "segments": []}
            ports.append(cur)
            continue
        if cur is None:
            raise ValueError("第 %d 行: 端口定义必须以 '@端口名' 行开头" % lineno)
        brs = []
        for part in line.split("|"):
            part = part.strip()
            if not part:
                continue
            mode, layers = _icc.parse_segment_line(part, "series")
            if mode == "parallel":
                brs.extend([[l] for l in layers])      # 段内并联 = 单层支路并联
            else:
                brs.append(list(layers))
        if not brs:
            raise ValueError("第 %d 行: 段内没有层" % lineno)
        cur["segments"].append({"branches": brs})
    if not ports:
        raise ValueError("端口定义为空")
    for p in ports:
        if not p["segments"]:
            raise ValueError("端口 %s 没有任何段" % p["name"])
    return ports


def mx_parse_par_sd(path):
    """解析 Maxwell Matrix 结果文件 DV*_SOL*_PAR<矩阵ID>_V*.sd (纯文本)。

    每行"含 M( 的"数据行 = 一个解点: 行首数字 = 主扫描变量值, NSI(...) = 内禀值列表。
    矩阵键: M([i j]v) = 电感 (H); MR([i j]v) = 交流电阻 (Ω); MI([i j]v) = ω·L (Ω)。
    返回 [{'lead': float|None, 'nsi': [..], 'M': {(i,j): v}, 'MR': {...}, 'MI': {...}}, ...]
    """
    import re as _re2
    txt = open(path, encoding="utf-8", errors="replace").read()
    out = []
    for line in txt.splitlines():
        if "M(" not in line:
            continue
        rec = {"M": {}, "MR": {}, "MI": {}, "nsi": [], "lead": None}
        for m in _re2.finditer(r"(M|MR|MI)\(\[(\d+)\s+(\d+)\]([-+0-9.eE]+)\)", line):
            rec[m.group(1)][(int(m.group(2)), int(m.group(3)))] = float(m.group(4))
        if not rec["M"] and not rec["MR"]:
            continue
        rec["nsi"] = [float(x) for x in
                      _re2.findall(r"NSI\(([-+0-9.eE]+)\)", line)]
        lm = _re2.match(r"\s*([-+0-9.eE]+)", line)
        if lm:
            rec["lead"] = float(lm.group(1))
        out.append(rec)
    return out


def mx_windings_from_aedt(aedt_path, dname):
    """从 .aedt 文本解析指定设计的 Winding(激励) 名与 Matrix(矩阵) 参数定义。

    设计块定位同 backend._temps_from_file (坑 #50 风格):
    $begin 'Maxwell3DModel' 块头附近 Name='<设计名>'。
    返回 (windings {id: name}, matrices {id: {'name':..,'sources':[...]}}); 失败 (None, None)
    """
    import re as _re3
    try:
        s = open(aedt_path, encoding="utf-8", errors="replace").read()
    except Exception:
        return None, None
    tgt = None
    for m in _re3.finditer(r"\$begin 'Maxwell3DModel'", s):
        head = s[m.start():m.start() + 2500]
        if _re3.search(r"Name='" + _re3.escape(dname) + r"'", head):
            tgt = m.start()
            break
    if tgt is None:
        return None, None
    end = s.find("$end 'Maxwell3DModel'", tgt)
    blk = s[tgt: end if end > tgt else len(s)]

    windings = {}
    for m in _re3.finditer(r"\$begin '(Winding[^']*)'", blk):
        se = blk.find("$end '" + m.group(1) + "'", m.start())
        seg = blk[m.start(): se if se > m.start() else m.start() + 20000]
        mi = _re3.search(r"\bID=(\d+)", seg)
        if mi:
            windings[int(mi.group(1))] = m.group(1)

    matrices = {}
    for m in _re3.finditer(r"\$begin '(Matrix\d+)'", blk):
        se = blk.find("$end '" + m.group(1) + "'", m.start())
        seg = blk[m.start(): se if se > m.start() else m.start() + 20000]
        if "MaxwellParameterType" not in seg:
            continue
        mi = _re3.search(r"\bID=(\d+)", seg)
        if not mi:
            continue
        matrices[int(mi.group(1))] = {
            "name": m.group(1),
            "sources": [int(x) for x in _re3.findall(r"Source=(\d+)", seg)]}
    return windings, matrices


class App:
    def __init__(self, root, cfg):
        self.root = root
        self.cfg = cfg
        self.pyaedt_py = tk.StringVar(value=cfg.get("python", ""))
        self.state = {}
        root.title("Maxwell 仿真批量后处理")
        root.geometry("%dx%d" % (int(1080 * SCALE), int(800 * SCALE)))
        root.minsize(int(900 * SCALE), int(640 * SCALE))
        root.resizable(True, True)
        root.configure(bg=PALETTE["bg"])

        self.runner = Runner(self.log, self.on_done, self.on_state, root=root)

        # ---------------- 顶部：环境设置（PyAEDT 解释器）
        envf = ttk.LabelFrame(root, text="环境设置", style="Card.TLabelframe", padding=6)
        envf.pack(fill="x", padx=8, pady=6)
        er = ttk.Frame(envf)
        er.pack(fill="x", padx=6, pady=4)
        ttk.Label(er, text="PyAEDT 解释器：").pack(side="left")
        ttk.Entry(er, textvariable=self.pyaedt_py).pack(side="left", fill="x",
                                                        expand=True, padx=6)
        ttk.Button(er, text="浏览…", command=self._browse_py,
                   style="Accent.TButton").pack(side="left")
        ttk.Button(er, text="保存设置", command=self._save_cfg,
                   style="Accent.TButton").pack(side="left", padx=(6, 0))

        # ---------------- 顶部：扫描区
        top = ttk.LabelFrame(root, text="当前激活的设计",
                             style="Card.TLabelframe", padding=6)
        top.pack(fill="x", padx=8, pady=6)

        r0 = ttk.Frame(top)
        r0.pack(fill="x", padx=6, pady=(4, 2))
        self.b_scan = ttk.Button(r0, text="扫描当前设计",
                                 command=self.do_scan,
                                 style="Accent.TButton")
        self.b_scan.pack(side="left")
        self.b_stop = ttk.Button(r0, text="中止", command=self.runner.stop,
                                 state="disabled")
        self.b_stop.pack(side="left", padx=6)
        self.v_status = tk.StringVar(value="未扫描")
        ttk.Label(r0, textvariable=self.v_status,
                  style="Muted.TLabel").pack(side="left", padx=10)

        r1 = ttk.Frame(top)
        r1.pack(fill="x", padx=6)
        self.v_proj = tk.StringVar(value="工程: -")
        self.v_des = tk.StringVar(value="设计: -")
        self.v_cs = tk.StringVar(value="坐标系: -")
        for v in (self.v_proj, self.v_des, self.v_cs):
            ttk.Label(r1, textvariable=v).pack(side="left", padx=(0, 18))

        r2 = ttk.Frame(top)
        r2.pack(fill="x", padx=6, pady=(2, 6))
        self.v_objs = tk.StringVar(value="物体: -")
        self.v_cu = tk.StringVar(value="copper: -")
        self.v_sec = tk.StringVar(value="剖面(Sheets): -")
        self.v_expr = tk.StringVar(value="表达式: -")
        self.v_res = tk.StringVar(value="结果: -")
        self.v_fpl = tk.StringVar(value="场图: -")
        for v in (self.v_objs, self.v_cu, self.v_sec, self.v_expr, self.v_res,
                  self.v_fpl):
            ttk.Label(r2, textvariable=v).pack(side="left", padx=(0, 16))

        # ---------------- 标签页
        pw_main = tk.PanedWindow(root, orient=tk.VERTICAL, sashwidth=6, sashrelief="flat",
                            bg=PALETTE["bg"], bd=0)
        pw_main.pack(fill="both", expand=True, padx=8, pady=4)
        nb = ttk.Notebook(pw_main)
        pw_main.add(nb, minsize=260, stretch="always")

        # ---- Tab 1: OhmicLoss
        t1 = ttk.Frame(nb)
        nb.add(t1, text="  Ohmic Loss 积分  ")
        self.sp_ohm = StackPanel(t1, left_title="候选实体",
                                 right_title="已入栈（待积分）",
                                 on_change=self._sync_ohm)
        self.sp_ohm.pack(fill="both", expand=True, padx=6, pady=6)
        f1 = ttk.Frame(t1)
        f1.pack(fill="x", padx=6, pady=(0, 8))
        ttk.Label(f1, text="变量名前缀:").pack(side="left")
        self.v_prefix = tk.StringVar(value="OhmicLoss_")
        ttk.Entry(f1, textvariable=self.v_prefix, width=16).pack(side="left",
                                                                 padx=4)
        self.v_osave = tk.BooleanVar(value=True)
        ttk.Checkbutton(f1, text="完成后保存工程",
                        variable=self.v_osave).pack(side="left", padx=12)
        self.b_ohm = ttk.Button(f1, text="执行 Ohmic Loss 积分",
                                command=self.do_ohmic, state="disabled",
                                style="Accent.TButton")
        self.b_ohm.pack(side="right")

        # ---- Tab 2: 电流积分
        # 损耗柱状图（前提：Ohmic Loss 积分 tab 已创建 OhmicLoss_<实体> 表达式）
        t8 = ttk.Frame(nb)
        nb.add(t8, text="  损耗柱状图  ")
        self.sp_bar = StackPanel(t8, left_title="候选实体",
                                 right_title="已入栈（待对比）",
                                 on_change=self._sync_bar)
        self.sp_bar.pack(fill="both", expand=True, padx=6, pady=6)
        f8 = ttk.Frame(t8)
        f8.pack(fill="x", padx=6, pady=(0, 4))
        self.v_bsave = tk.BooleanVar(value=True)
        ttk.Checkbutton(f8, text="保存工程",
                        variable=self.v_bsave).pack(side="left")
        self.b_barall = ttk.Button(f8, text="生成/刷新总报表",
                                   command=self.do_barall,
                                   style="Accent.TButton")
        self.b_barall.pack(side="left", padx=(10, 4))
        self.b_bar = ttk.Button(f8, text="从缓存生成柱状图",
                                command=self.do_barloss,
                                style="Accent.TButton", state="disabled")
        self.b_bar.pack(side="left", padx=4)
        self.b_barwin = ttk.Button(f8, text="打开图表窗口",
                                   command=self._open_bar_window)
        self.b_barwin.pack(side="left", padx=4)
        self.v_barinfo = tk.StringVar(
            value="第一步：生成/刷新总报表（一次读取全部 OhmicLoss_*）→ "
                  "第二步：入栈后从缓存出图，不再访问 AEDT")
        ttk.Label(f8, textvariable=self.v_barinfo,
                  style="Muted.TLabel").pack(side="left", padx=6)
        self.bar_data = None
        self.bar_pool = None
        self._bar_report = ""
        self._bar_proj = ""
        self._bar_des = ""
        self._barwin = None
        self._bar_fig = None
        self._bar_sweep = "?"
        self._bar_freq = None

        t2 = ttk.Frame(nb)
        nb.add(t2, text="  电流积分  ")
        self.sp_cur = StackPanel(t2, left_title="候选实体",
                                 right_title="已入栈（待剖面积分）",
                                 on_change=self._sync_cur)
        self.sp_cur.pack(fill="both", expand=True, padx=6, pady=6)
        f2 = ttk.Frame(t2)
        f2.pack(fill="x", padx=6, pady=(0, 8))
        ttk.Label(f2, text="剖切面:").pack(side="left")
        self.v_plane = tk.StringVar(value="YZ")
        for label, val in PLANES:
            ttk.Radiobutton(f2, text=label, value=val,
                            variable=self.v_plane).pack(side="left", padx=6)
        f3 = ttk.Frame(t2)
        f3.pack(fill="x", padx=6, pady=(0, 8))
        ttk.Label(f3, text="报表名:").pack(side="left")
        self.v_report = tk.StringVar(value="")
        ttk.Entry(f3, textvariable=self.v_report, width=30).pack(side="left",
                                                                 padx=4)
        self.v_csave = tk.BooleanVar(value=True)
        self.v_cbackup = tk.BooleanVar(value=True)
        ttk.Checkbutton(f3, text="保存工程", variable=self.v_csave).pack(
            side="left", padx=10)
        ttk.Checkbutton(f3, text="保存前备份 .aedt",
                        variable=self.v_cbackup).pack(side="left")
        self.b_cur = ttk.Button(f3, text="执行电流积分",
                                command=self.do_current, state="disabled",
                                style="Accent.TButton")
        self.b_cur.pack(side="right")

        # ---- Tab 3: 剖面清单
        t3 = ttk.Frame(nb)
        nb.add(t3, text="  剖面 / Sheets  ")
        head = ttk.Frame(t3)
        head.pack(fill="x", padx=6, pady=4)
        self.v_secinfo = tk.StringVar(value="未扫描")
        ttk.Label(head, textvariable=self.v_secinfo).pack(side="left")
        sb = ttk.Scrollbar(t3)
        sb.pack(side="right", fill="y")
        self.secbox = tk.Listbox(t3, yscrollcommand=sb.set, height=18,
                                 **listbox_kw())
        self.secbox.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        sb.configure(command=self.secbox.yview)

        # ---- Tab 4: 场计算器变量
        t4 = ttk.Frame(nb)
        nb.add(t4, text="  场计算器变量  ")
        self.sp_expr = StackPanel(
            t4, left_title="全部变量", right_title="待删除（已入栈）",
            on_change=self._sync_expr, show_cu=False,
            prefix_values=["OhmicLoss_", "I_sec_"])
        self.sp_expr.pack(fill="both", expand=True, padx=6, pady=6)
        f4 = ttk.Frame(t4)
        f4.pack(fill="x", padx=6, pady=(0, 8))
        ttk.Label(f4, text="⚠ 只删表达式，不动几何与网格；被报表引用的变量删掉后该报表取不到数",
                  style="Muted.TLabel").pack(side="left")
        self.v_dsave = tk.BooleanVar(value=True)
        ttk.Checkbutton(f4, text="删除后保存工程",
                        variable=self.v_dsave).pack(side="left", padx=12)
        self.b_del = ttk.Button(f4, text="删除选中变量",
                                command=self.do_dropvars, state="disabled",
                                style="Danger.TButton")
        self.b_del.pack(side="right")

        # ---- Tab 5: 体积排序 / 匹配
        t5 = ttk.Frame(nb)
        nb.add(t5, text="  体积排序 / 匹配  ")

        pw5 = tk.PanedWindow(t5, orient=tk.VERTICAL, sashwidth=6, sashrelief="flat",
                            bg=PALETTE["bg"], bd=0)
        pw5.pack(fill="both", expand=True)
        vtop = ttk.LabelFrame(pw5, text="copper 按体积排序（同步重排候选池）",
                              style="Card.TLabelframe", padding=6)
        pw5.add(vtop, minsize=130, stretch="always")
        self.v_sort = tk.StringVar(value="默认")
        for _txt, _val in (("默认", "默认"), ("升序（小→大）", "升序"),
                           ("降序（大→小）", "降序")):
            ttk.Radiobutton(vtop, text=_txt, value=_val,
                            variable=self.v_sort,
                            command=self._on_sort_changed).pack(side="left",
                                                                padx=6)
        ttk.Label(vtop, text="体积在扫描时读取（GetObjectVolume，模型单位³）",
                  style="Muted.TLabel").pack(side="left", padx=14)
        vmid = ttk.Frame(vtop)
        vmid.pack(fill="both", expand=True, pady=(4, 0))
        self.volbox = tk.Listbox(vmid, height=7, **listbox_kw())
        _vsb = ttk.Scrollbar(vmid, command=self.volbox.yview)
        self.volbox.configure(yscrollcommand=_vsb.set)
        self.volbox.pack(side="left", fill="both", expand=True)
        _vsb.pack(side="left", fill="y")
        self.v_volinfo = tk.StringVar(value="未扫描")
        ttk.Label(vtop, textvariable=self.v_volinfo,
                  style="Muted.TLabel").pack(anchor="w", padx=2, pady=(2, 0))

        vmatch = ttk.LabelFrame(pw5, text="匹配筛选（与指定实体偏差 ≤ 容差；条件可独立勾选）",
                                style="Card.TLabelframe", padding=6)
        pw5.add(vmatch, minsize=130, stretch="always")
        mr = ttk.Frame(vmatch)
        mr.pack(fill="x", padx=4)
        ttk.Label(mr, text="参考实体:").pack(side="left")
        self.cb_ref = ttk.Combobox(mr, width=26)
        self.cb_ref.pack(side="left", padx=6)
        ttk.Label(mr, text="容差 ±").pack(side="left")
        self.v_tol = tk.StringVar(value="5")
        ttk.Entry(mr, textvariable=self.v_tol, width=6).pack(side="left",
                                                             padx=4)
        ttk.Label(mr, text="%").pack(side="left")
        self.v_matchvol = tk.BooleanVar(value=True)
        ttk.Checkbutton(mr, text="按体积",
                        variable=self.v_matchvol).pack(side="left", padx=(12, 0))
        self.v_matchxy = tk.BooleanVar(value=False)
        ttk.Checkbutton(mr, text="按 X/Y 坐标（默认不勾）",
                        variable=self.v_matchxy).pack(side="left", padx=12)
        mr2 = ttk.Frame(vmatch)
        mr2.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Button(mr2, text="筛选匹配实体", command=self._do_match,
                   style="Accent.TButton").pack(side="left")
        ttk.Label(mr2, text="排序:").pack(side="left", padx=(14, 2))
        self.cb_msort = ttk.Combobox(mr2, width=13, state="readonly",
                                     values=["偏差（默认）", "体积", "Z 坐标"])
        self.cb_msort.current(0)
        self.cb_msort.pack(side="left")
        ttk.Label(mr2, text="方向:").pack(side="left", padx=(10, 2))
        self.cb_mdir = ttk.Combobox(mr2, width=6, state="readonly",
                                    values=["正序", "逆序"])
        self.cb_mdir.current(0)
        self.cb_mdir.pack(side="left")
        self.v_matchinfo = tk.StringVar(value="")
        ttk.Label(mr2, textvariable=self.v_matchinfo,
                  style="Muted.TLabel").pack(side="left", padx=12)
        mmid = ttk.Frame(vmatch)
        mmid.pack(fill="both", expand=True, pady=(4, 0))
        self.matchbox = tk.Listbox(mmid, height=7, **listbox_kw())
        _msb = ttk.Scrollbar(mmid, command=self.matchbox.yview)
        self.matchbox.configure(yscrollcommand=_msb.set)
        self.matchbox.pack(side="left", fill="both", expand=True)
        _msb.pack(side="left", fill="y")
        mmid2 = ttk.Frame(vmatch)
        mmid2.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(mmid2, text="匹配结果全部入栈到:").pack(side="left")
        self.cb_mtarget = ttk.Combobox(mmid2, width=16, state="readonly",
                                       values=["温度赋值", "Ohmic Loss 积分",
                                               "电流积分", "电流密度场图",
                                               "损耗柱状图"])
        self.cb_mtarget.current(0)
        self.cb_mtarget.pack(side="left", padx=6)
        ttk.Button(mmid2, text="入栈", command=self._do_mpush).pack(
            side="left")

        zfilt = ttk.LabelFrame(
            pw5, text="Z 坐标筛选（bbox 中心 z，模型单位；两阈值可同时填=区间）",
                               style="Card.TLabelframe", padding=6)
        pw5.add(zfilt, minsize=110, stretch="always")
        zr = ttk.Frame(zfilt)
        zr.pack(fill="x", padx=4)
        ttk.Label(zr, text="z ≥").pack(side="left", padx=(2, 2))
        self.v_zlo = tk.StringVar(value="")
        ttk.Entry(zr, textvariable=self.v_zlo, width=8).pack(side="left",
                                                             padx=2)
        ttk.Label(zr, text="z ≤").pack(side="left", padx=(10, 2))
        self.v_zhi = tk.StringVar(value="")
        ttk.Entry(zr, textvariable=self.v_zhi, width=8).pack(side="left",
                                                             padx=2)
        ttk.Label(zr, text="（留空=该边不限）",
                  style="Muted.TLabel").pack(side="left", padx=2)
        self.v_znovia = tk.BooleanVar(value=True)
        ttk.Checkbutton(zr, text="排除名字含 via",
                        variable=self.v_znovia).pack(side="left", padx=10)
        ttk.Button(zr, text="筛选实体", command=self._do_zfilter,
                   style="Accent.TButton").pack(side="left", padx=10)
        self.v_zinfo = tk.StringVar(value="")
        ttk.Label(zr, textvariable=self.v_zinfo,
                  style="Muted.TLabel").pack(side="left", padx=8)
        zr2 = ttk.Frame(zfilt)
        zr2.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(zr2, text="筛选结果全部入栈到:").pack(side="left")
        self.cb_ztarget = ttk.Combobox(zr2, width=16, state="readonly",
                                       values=["温度赋值", "Ohmic Loss 积分",
                                               "电流积分", "电流密度场图",
                                               "损耗柱状图"])
        self.cb_ztarget.current(0)
        self.cb_ztarget.pack(side="left", padx=6)
        ttk.Button(zr2, text="入栈", command=self._do_zpush).pack(side="left")
        zmid = ttk.Frame(zfilt)
        zmid.pack(fill="both", expand=True, pady=(4, 0))
        self.zbox = tk.Listbox(zmid, height=5, **listbox_kw())
        _zsb = ttk.Scrollbar(zmid, orient="vertical",
                             command=self.zbox.yview)
        self.zbox.configure(yscrollcommand=_zsb.set)
        self.zbox.pack(side="left", fill="both", expand=True)
        _zsb.pack(side="right", fill="y")

        # ---- Tab 6: 电流密度场图
        t6 = ttk.Frame(nb)
        nb.add(t6, text="  电流密度场图  ")
        self.sp_j = StackPanel(t6, left_title="候选实体",
                               right_title="已入栈（待建 J 场图）",
                               on_change=self._sync_j)
        self.sp_j.pack(fill="both", expand=True, padx=6, pady=6)
        f6 = ttk.Frame(t6)
        f6.pack(fill="x", padx=6, pady=(0, 4))
        self.v_jmag = tk.BooleanVar(value=True)
        self.v_jvec = tk.BooleanVar(value=True)
        ttk.Checkbutton(f6, text="Mag_J", variable=self.v_jmag).pack(side="left")
        ttk.Checkbutton(f6, text="Vector_J",
                        variable=self.v_jvec).pack(side="left", padx=12)
        ttk.Label(f6, text="相位:").pack(side="left", padx=(12, 0))
        self.v_jphase = tk.StringVar(value="0deg")
        ttk.Entry(f6, textvariable=self.v_jphase, width=10).pack(side="left",
                                                                 padx=4)
        self.v_jsave = tk.BooleanVar(value=True)
        ttk.Checkbutton(f6, text="创建后保存工程",
                        variable=self.v_jsave).pack(side="left", padx=12)
        self.v_jsurface = tk.BooleanVar(value=True)
        ttk.Checkbutton(f6, text="纯表面场图（Plot on surface only）",
                        variable=self.v_jsurface).pack(side="left",
                                                       padx=(12, 0))
        f6b = ttk.Frame(t6)
        f6b.pack(fill="x", padx=6, pady=(0, 8))
        ttk.Label(f6b, text="场图名 = 实体名；文件夹 = Mag_J / Vector_J",
                  style="Muted.TLabel").pack(side="left")
        self.b_j = ttk.Button(f6b, text="创建 J 场图",
                              command=self.do_jplot, state="disabled",
                              style="Accent.TButton")
        self.b_j.pack(side="right")

        # ---- Tab 7: 温度赋值
        t7 = ttk.Frame(nb)
        nb.add(t7, text="  温度赋值  ")
        pw7 = tk.PanedWindow(t7, orient=tk.VERTICAL, sashwidth=6, sashrelief="flat",
                            bg=PALETTE["bg"], bd=0)
        pw7.pack(fill="both", expand=True)
        top7 = ttk.Frame(pw7)
        pw7.add(top7, minsize=180, stretch="always")
        self.sp_temp = StackPanel(top7, left_title="候选实体",
                                  right_title="已入栈（待赋温度）",
                                  on_change=self._sync_temp)
        self.sp_temp.pack(fill="both", expand=True, padx=6, pady=6)
        f7 = ttk.Frame(top7)
        f7.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(f7, text="温度 (°C):").pack(side="left")
        self.v_temp = tk.StringVar(value="22")
        ttk.Entry(f7, textvariable=self.v_temp, width=8).pack(side="left",
                                                              padx=4)
        self.v_tdep = tk.BooleanVar(value=True)
        self.v_tfb = tk.BooleanVar(value=False)
        ttk.Checkbutton(f7, text="包含温度依赖",
                        variable=self.v_tdep).pack(side="left", padx=10)
        ttk.Checkbutton(f7, text="启用热反馈",
                        variable=self.v_tfb).pack(side="left", padx=6)
        self.v_tsave = tk.BooleanVar(value=True)
        ttk.Checkbutton(f7, text="完成后保存工程",
                        variable=self.v_tsave).pack(side="left", padx=12)
        f7b = ttk.Frame(top7)
        f7b.pack(fill="x", padx=6, pady=(0, 8))
        ttk.Label(f7b, text="底层 = oDesign.SetObjectTemperature；"
                            "温度需材料带温度系数才影响损耗，属求解设置修改",
                  style="Muted.TLabel").pack(side="left")
        self.b_readtemp = ttk.Button(f7b, text="读取当前温度",
                                     command=self.do_readtemp,
                                     state="disabled",
                                     style="Accent.TButton")
        self.b_temp = ttk.Button(f7b, text="对入栈实体赋温度",
                                 command=self.do_temp, state="disabled",
                                 style="Accent.TButton")
        self.b_temp.pack(side="right")
        self.b_readtemp.pack(side="right", padx=(0, 8))
        f7c = ttk.Frame(pw7)
        pw7.add(f7c, minsize=110, height=200, stretch="always")
        trow = ttk.Frame(f7c)
        trow.pack(fill="x")
        ttk.Label(trow, text="当前温度:").pack(side="left")
        self.v_tempinfo = tk.StringVar(
            value="未读取 —— 入栈后点「读取当前温度」")
        ttk.Label(trow, textvariable=self.v_tempinfo,
                  style="Muted.TLabel").pack(side="left", padx=10)
        tmid = ttk.Frame(f7c)
        tmid.pack(fill="both", expand=True, pady=(3, 0))
        self.tempbox = tk.Listbox(tmid, height=6, **listbox_kw())
        _tsb = ttk.Scrollbar(tmid, orient="vertical",
                             command=self.tempbox.yview)
        self.tempbox.configure(yscrollcommand=_tsb.set)
        self.tempbox.pack(side="left", fill="both", expand=True)
        _tsb.pack(side="right", fill="y")

        # ---- Tab 9: matrix等效 (电感矩阵 -> 端口矩阵 -> T 型等效)
        # 纯 numpy 本地计算, 不走 Runner / 不碰 AEDT
        t9 = ttk.Frame(nb)
        nb.add(t9, text="  matrix等效  ")
        if _icc is None:
            _mxhint = ("!! 未找到 indcalc_core / numpy —— 计算不可用。"
                       "请把 indcalc_core.py 放到 GUI 同目录, 或设置环境变量 "
                       "MAXWELL_MATRIX_CORE_DIR 指向其所在目录, "
                       "并确认当前 Python 已装 numpy。")
            ttk.Label(t9, text=_mxhint,
                      foreground=PALETTE["danger"]).pack(fill="x", padx=6, pady=4)
        # 上下可拖: 输入区 (矩阵/端口/计算行) vs 结果区
        pw9 = tk.PanedWindow(t9, orient=tk.VERTICAL, sashwidth=6, sashrelief="flat",
                            bg=PALETTE["bg"], bd=0)
        pw9.pack(fill="both", expand=True)
        top9 = tk.PanedWindow(pw9, orient=tk.VERTICAL, sashwidth=6,
                              sashrelief="flat", bg=PALETTE["bg"], bd=0)
        pw9.add(top9, minsize=190, height=300, stretch="always")
        f9a = ttk.LabelFrame(top9, text='① Maxwell 电感矩阵 ("电阻, 电感" 双值或单值电感;'
                                      ' 导入 txt/tab/csv 或直接粘贴)',
                             style="Card.TLabelframe", padding=4)
        top9.add(f9a, minsize=90, padx=6, pady=4, stretch="always")
        bar9a = ttk.Frame(f9a)
        bar9a.pack(fill="x")
        ttk.Button(bar9a, text="导入矩阵文件…",
                   command=self.do_mx_file).pack(side="left")
        ttk.Button(bar9a, text="粘贴矩阵",
                   command=self.do_mx_paste).pack(side="left", padx=6)
        ttk.Button(bar9a, text="填入示例",
                   command=self.do_mx_demo).pack(side="left")
        ttk.Button(bar9a, text="从结果目录读 Matrix…",
                   command=self.do_mx_read).pack(side="left", padx=(8, 0))
        ttk.Label(bar9a, text="输入单位  电阻:").pack(side="left", padx=(14, 2))
        self.mx_ru_in = tk.StringVar(value="Ω")
        ttk.Combobox(bar9a, textvariable=self.mx_ru_in, width=4, state="readonly",
                     values=[u for u, _ in _icc.R_UNITS] if _icc else
                     ["Ω", "mΩ", "µΩ"]).pack(side="left")
        ttk.Label(bar9a, text="电感:").pack(side="left", padx=(8, 2))
        self.mx_lu_in = tk.StringVar(value="µH")
        ttk.Combobox(bar9a, textvariable=self.mx_lu_in, width=4, state="readonly",
                     values=[u for u, _ in _icc.L_UNITS] if _icc else
                     ["H", "mH", "µH", "nH"]).pack(side="left")
        self.mx_st1 = tk.StringVar(value="矩阵: 未解析")
        ttk.Label(bar9a, textvariable=self.mx_st1,
                  style="Muted.TLabel").pack(side="left", padx=12)
        _kw9a = text_kw()
        _kw9a.update(height=4, wrap="none", font=("Consolas", 9))
        self.mx_txt = tk.Text(f9a, **_kw9a)
        self.mx_txt.pack(fill="both", expand=True,
                         padx=2, pady=(4, 0))

        f9b = ttk.LabelFrame(top9, text="② 端口定义  (段间串联/并联; 段内多条并联支路, 支路内层串联;"
                                      " 层名下拉选择, 勾选“反”=反接; 双击行=编辑)",
                             style="Card.TLabelframe", padding=4)
        top9.add(f9b, minsize=110, padx=6, pady=2,
                stretch="always")
        bar9b = ttk.Frame(f9b)
        bar9b.pack(fill="x")
        ttk.Button(bar9b, text="添加端口",
                   command=lambda: self._mx_edit_port(-1)).pack(side="left")
        ttk.Button(bar9b, text="编辑",
                   command=self._mx_edit_sel).pack(side="left", padx=6)
        ttk.Button(bar9b, text="删除",
                   command=self._mx_del_port).pack(side="left")
        ttk.Button(bar9b, text="从文本导入…",
                   command=self.do_mx_portfile).pack(side="left", padx=(14, 0))
        ttk.Button(bar9b, text="导出为文本",
                   command=self.do_mx_portexport).pack(side="left", padx=6)
        self.mx_st2 = tk.StringVar(value="端口: 未定义")
        ttk.Label(bar9b, textvariable=self.mx_st2,
                  style="Muted.TLabel").pack(side="right")
        pm = ttk.Frame(f9b)
        pm.pack(fill="both", expand=True, padx=2, pady=(4, 0))
        self.tv_mxP = ttk.Treeview(pm, columns=("name", "desc"),
                                   show="headings", height=5)
        self.tv_mxP.heading("name", text="端口名")
        self.tv_mxP.column("name", width=110, anchor="w")
        self.tv_mxP.heading("desc", text="串并联结构 (段间串联, 段内 ∥ 并联)")
        self.tv_mxP.column("desc", width=540, anchor="w")
        psb = ttk.Scrollbar(pm, orient="vertical", command=self.tv_mxP.yview)
        self.tv_mxP.configure(yscrollcommand=psb.set)
        self.tv_mxP.pack(side="left", fill="both", expand=True)
        psb.pack(side="right", fill="y")
        self.tv_mxP.bind("<Double-1>", lambda e: self._mx_edit_sel())
        self.mx_ports = []

        f9c = ttk.Frame(top9)
        top9.add(f9c, minsize=34, padx=6, pady=2,
                stretch="never")
        self.mx_keep = tk.BooleanVar(value=True)
        ttk.Checkbutton(f9c, text="未引用层开路消去",
                        variable=self.mx_keep).pack(side="left")
        ttk.Label(f9c, text="输出单位  电感:").pack(side="left", padx=(14, 2))
        self.mx_lu_out = tk.StringVar(value="µH")
        ttk.Combobox(f9c, textvariable=self.mx_lu_out, width=4, state="readonly",
                     values=[u for u, _ in _icc.L_UNITS] if _icc else
                     ["H", "mH", "µH", "nH"]).pack(side="left")
        ttk.Label(f9c, text="电阻:").pack(side="left", padx=(8, 2))
        self.mx_ru_out = tk.StringVar(value="mΩ")
        ttk.Combobox(f9c, textvariable=self.mx_ru_out, width=4, state="readonly",
                     values=[u for u, _ in _icc.R_UNITS] if _icc else
                     ["Ω", "mΩ", "µΩ"]).pack(side="left")
        self.mx_tprim = tk.StringVar()
        self.mx_tsec = tk.StringVar()
        ttk.Button(f9c, text="导出 CSV…",
                   command=self.do_mx_export).pack(side="right", padx=(6, 0))
        self.b_mx = ttk.Button(f9c, text="计算端口矩阵 + T 型等效",
                               command=self.do_mx_compute, style="Accent.TButton")
        self.b_mx.pack(side="right")

        f9d = ttk.LabelFrame(pw9, text="④ 结果 (左: 端口矩阵  右: T 型等效)",
                             style="Card.TLabelframe", padding=4)
        pw9.add(f9d, minsize=150, height=300, stretch="always")
        res9 = tk.PanedWindow(f9d, orient=tk.HORIZONTAL, sashwidth=6, sashrelief="flat",
                            bg=PALETTE["bg"], bd=0)
        res9.pack(fill="both", expand=True)
        colL = tk.PanedWindow(res9, orient=tk.VERTICAL, sashwidth=6,
                              sashrelief="flat", bg=PALETTE["bg"], bd=0)
        res9.add(colL, minsize=200, stretch="always")
        colR = ttk.Frame(res9)
        res9.add(colR, minsize=200, stretch="always")
        gL = ttk.Frame(colL)
        ttk.Label(gL, text="端口电感矩阵:").pack(anchor="w")
        self.tv_mxL = ttk.Treeview(gL, show="headings", height=4)
        self.tv_mxL.pack(fill="both", expand=True)
        colL.add(gL, minsize=70, stretch="always")
        gR = ttk.Frame(colL)
        ttk.Label(gR, text="端口电阻矩阵:").pack(anchor="w", pady=(4, 0))
        self.tv_mxR = ttk.Treeview(gR, show="headings", height=4)
        self.tv_mxR.pack(fill="both", expand=True)
        colL.add(gR, minsize=70, stretch="always")
        trow9 = ttk.Frame(colR)
        trow9.pack(fill="x")
        ttk.Label(trow9, text="T 型等效  原边:").pack(side="left")
        self.cb_mxprim = ttk.Combobox(trow9, textvariable=self.mx_tprim,
                                      width=10, state="readonly")
        self.cb_mxprim.pack(side="left", padx=4)
        ttk.Label(trow9, text="副边:").pack(side="left", padx=(6, 0))
        self.cb_mxsec = ttk.Combobox(trow9, textvariable=self.mx_tsec,
                                     width=10, state="readonly")
        self.cb_mxsec.pack(side="left", padx=4)
        ttk.Button(trow9, text="计算 T 型",
                   command=self.do_mx_t).pack(side="left", padx=6)
        self.tv_mxT = ttk.Treeview(colR, show="headings", height=8)
        self.tv_mxT.pack(fill="both", expand=True, pady=(4, 0))

        # 单位改变 -> 自动重渲染 (仅对"从结果目录读入"的矩阵, 手填矩阵不覆盖)
        self.mx_ru_in.trace_add("write", self._mx_on_unit_change)
        self.mx_lu_in.trace_add("write", self._mx_on_unit_change)
        self.mx_ru_out.trace_add("write", self._mx_on_outunit_change)
        self.mx_lu_out.trace_add("write", self._mx_on_outunit_change)
        self.mx_si = None

        # ---------------- 日志
        lf = ttk.LabelFrame(pw_main, text="日志",
                            style="Card.TLabelframe", padding=4)
        pw_main.add(lf, minsize=80, height=185, stretch="never")
        _tkw = text_kw()
        _tkw["font"] = ("Consolas", 9)
        self.txt = scrolledtext.ScrolledText(lf, height=12, wrap="none",
                                             **_tkw)
        self.txt.pack(fill="both", expand=True, padx=4, pady=4)
        for tag, col in (("err", PALETTE["danger"]), ("ok", PALETTE["ok"]),
                         ("cmd", PALETTE["accent"])):
            self.txt.tag_configure(tag, foreground=col)
        ttk.Button(lf, text="清空日志",
                   command=lambda: self.txt.delete("1.0", tk.END)).pack(
            anchor="e", padx=6, pady=(0, 4))

        self.log("提示：先点【扫描当前设计】。所有操作只作用于 AEDT 里当前激活的设计。")

    # ---------------------------------------------------------- matrix等效
    def _mx_pick(self, labels, values, title="请选择"):
        """通用单选对话框 (Listbox + 确定/取消), 默认选中最后一项; 返回 value 或 None。"""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("720x320")
        win.resizable(True, True)
        win.minsize(480, 260)
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text=title).pack(anchor="w", padx=8, pady=(8, 4))
        mid = ttk.Frame(win)
        mid.pack(fill="both", expand=True, padx=8)
        lb = tk.Listbox(mid, **listbox_kw())
        sb = ttk.Scrollbar(mid, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for s in labels:
            lb.insert("end", s)
        if labels:
            lb.selection_set(len(labels) - 1)
            lb.see(len(labels) - 1)
        out = [None]

        def ok():
            i = lb.curselection()
            if i:
                out[0] = values[i[0]]
            win.destroy()

        btn = ttk.Frame(win)
        btn.pack(fill="x", padx=8, pady=8)
        ttk.Button(btn, text="取消", command=win.destroy).pack(side="right",
                                                              padx=(6, 0))
        ttk.Button(btn, text="确定", command=ok,
                   style="Accent.TButton").pack(side="right")
        win.wait_window()
        return out[0]

    def do_mx_read(self):
        """直读 .aedtresults 里 Maxwell Matrix 结果 (零 AEDT 交互, 不开子进程)。"""
        import glob as _glob
        import time as _time
        ri = (self.state or {}).get("results") or {}
        rdir = ri.get("dir", "")
        if not rdir or not os.path.isdir(rdir):
            messagebox.showwarning(
                "matrix等效",
                "请先点顶部【扫描当前设计】——\n"
                "读 Matrix 结果需要扫描结果里的结果目录路径。")
            return
        cands = sorted(_glob.glob(os.path.join(rdir, "*_PAR*.sd")),
                       key=lambda p: os.path.getmtime(p))
        if not cands:
            messagebox.showwarning(
                "matrix等效",
                "结果目录里没有 Matrix 结果文件 (*_PAR*.sd)：\n%s\n\n"
                "（该设计可能没有保存过 Matrix 求解结果；需先在 Maxwell 里配置 "
                "Matrix 参数并求解）" % rdir)
            return
        if len(cands) > 1:
            labels = []
            for p in cands:
                try:
                    rs = mx_parse_par_sd(p)
                    n = max([max(i, j) for r in rs for (i, j) in r["M"]] or [0])
                except Exception:
                    rs, n = [], 0
                labels.append("%s   [%d×%d 矩阵, %d 个解点, %s]"
                              % (os.path.basename(p), n, n, len(rs),
                                 _time.strftime("%m-%d %H:%M",
                                                _time.localtime(os.path.getmtime(p)))))
            path = self._mx_pick(
                labels, cands,
                "选择 Matrix 结果文件（默认 = 最新，通常 SOL 编号最大 = 最终解）")
            if not path:
                return
        else:
            path = cands[0]
        self._mx_load_par(path, rdir)

    def _mx_load_par(self, path, rdir):
        """解析选中的 .sd -> 生成「电阻, 电感」双值矩阵文本 -> 填入矩阵框。"""
        import math as _math
        import re as _re4
        try:
            rows = mx_parse_par_sd(path)
        except Exception as e:
            messagebox.showerror("matrix等效", "解析失败: %s" % e)
            return
        if not rows:
            messagebox.showerror("matrix等效",
                                 "文件里没有矩阵数据:\n%s" % path)
            return
        if len(rows) > 1:
            labels = []
            for k, r in enumerate(rows):
                n = max([max(i, j) for (i, j) in r["M"]] or [0])
                extra = ", ".join("%g" % v for v in r["nsi"])
                labels.append("解点 #%d: %d×%d 矩阵, 主变量=%s%s"
                              % (k + 1, n, n,
                                 ("%g" % r["lead"]) if r["lead"] is not None else "?",
                                 (", 内禀=" + extra) if extra else ""))
            row = self._mx_pick(
                labels, rows,
                "选择解点（默认 = 最后一个，通常是最终频率点）")
            if row is None:
                return
        else:
            row = rows[0]
        n = max([max(i, j) for (i, j) in row["M"]] or [0])
        if n < 1:
            messagebox.showerror("matrix等效", "未解析出矩阵元素")
            return

        # ---- 行名: .aedt 里 Winding 名 + MatrixEntry 顺序
        names = None
        dname = (self.state or {}).get("design", "")
        pres = os.path.dirname(rdir)
        aedt = os.path.join(os.path.dirname(pres),
                            os.path.basename(pres).replace(".aedtresults", "") + ".aedt")
        m = _re4.search(r"_PAR(\d+)_", os.path.basename(path))
        mid = int(m.group(1)) if m else None
        if os.path.isfile(aedt) and dname:
            self.log("matrix等效: 解析工程文件取激励名 …")
            try:
                self.root.update_idletasks()
                windings, matrices = mx_windings_from_aedt(aedt, dname)
            except Exception as e:
                windings, matrices = None, None
                self.log("matrix等效: 工程文件解析失败 (%s)" % e, "err")
            if windings and matrices:
                srcs = matrices.get(mid, {}).get("sources") if mid is not None else None
                if not srcs:
                    srcs = list(matrices.values())[0]["sources"]
                if len(srcs) == n:
                    names = [windings.get(s, "W%d" % (k + 1))
                             for k, s in enumerate(srcs)]
        if not names:
            names = ["W%d" % (k + 1) for k in range(n)]

        # ---- 自洽校验 MI ≈ 2πf·M (f 取行首/NSI 里 >1e3 的值)
        freq = None
        for v in ([row["lead"]] if row["lead"] is not None else []) + row["nsi"]:
            if v and v > 1e3:
                freq = v
                break
        if freq:
            for (i, j), lv in sorted(row["M"].items()):
                iv = row["MI"].get((i, j))
                if iv and lv:
                    est = iv / (2 * _math.pi * freq)
                    dev = abs(est - lv) / abs(lv) * 100
                    if dev > 2:
                        self.log("matrix等效: 注意 M[%d,%d]=%.6g H 与 MI/(2πf)=%.6g H "
                                 "偏差 %.1f%%" % (i, j, lv, est, dev))
                    break

        # 保存 SI 原始矩阵 (H / Ω), 之后按输入单位渲染 -> 改单位时数值自动换算
        import numpy as _np
        Lsi = _np.zeros((n, n))
        for (i, j), v in row["M"].items():
            if 1 <= i <= n and 1 <= j <= n:
                Lsi[i - 1, j - 1] = v
        Rsi = None
        if row["MR"]:
            Rsi = _np.zeros((n, n))
            for (i, j), v in row["MR"].items():
                if 1 <= i <= n and 1 <= j <= n:
                    Rsi[i - 1, j - 1] = v
        self.mx_si = {"names": names, "L": Lsi, "R": Rsi}
        self.mx_Lp = None
        self.mx_Rp = None
        self.mx_ru_in.set("Ω")
        self.mx_lu_in.set("µH")
        self._mx_render_si()
        self.log("matrix等效: 已读 %s —— %d×%d 矩阵（R=MR, L=M，按输入单位显示）；行名: %s"
                 % (os.path.basename(path), n, n, ", ".join(names)), "ok")

    def _mx_rin(self):
        return dict(_icc.R_UNITS).get(self.mx_ru_in.get(), 1.0)

    def _mx_lin(self):
        return dict(_icc.L_UNITS).get(self.mx_lu_in.get(), 1e-6)

    def do_mx_file(self):
        """导入矩阵文本文件 (txt/tab/csv), 填入文本框并自动解析。"""
        p = filedialog.askopenfilename(
            filetypes=[("矩阵文本", "*.txt;*.tab;*.csv;*.mtx"),
                       ("所有文件", "*.*")])
        if not p:
            return
        with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
            t = f.read()
        self.mx_txt.delete("1.0", "end")
        self.mx_txt.insert("1.0", t)
        self.do_mx_parse()

    def do_mx_paste(self):
        """剪贴板 -> 矩阵文本框并自动解析。"""
        try:
            t = self.clipboard_get()
        except Exception:
            messagebox.showwarning("提示", "剪贴板没有文本")
            return
        self.mx_txt.delete("1.0", "end")
        self.mx_txt.insert("1.0", t)
        self.do_mx_parse()

    def do_mx_demo(self):
        """填入 6 层示例矩阵 + 3 端口定义并直接计算。"""
        self.mx_txt.delete("1.0", "end")
        self.mx_txt.insert("1.0", MX_DEMO_MATRIX)
        self.mx_ports = mx_parse_port_blocks(MX_DEMO_PORTS)
        self._mx_refresh_ports()
        self.do_mx_compute()

    def do_mx_parse(self):
        """解析矩阵文本框 -> (层名, R, L); 成功返回 True。"""
        if _icc is None:
            self.mx_st1.set("矩阵: indcalc_core 不可用")
            return False
        try:
            names, R, L = _icc.parse_matrix_rl(
                self.mx_txt.get("1.0", "end"),
                r_unit=self._mx_rin(), l_unit=self._mx_lin())
        except Exception as e:
            self.mx_st1.set("矩阵: 解析失败 — %s" % e)
            self.log("matrix等效: 矩阵解析失败: %s" % e, "err")
            return False
        self.mx_names, self.mx_R, self.mx_L = names, R, L
        kind = "R+L 双值" if R is not None else "纯电感"
        self.mx_st1.set("矩阵: %d 层 (%s)" % (len(names), kind))
        head = ", ".join(names[:8]) + ("…" if len(names) > 8 else "")
        self.log("matrix等效: 矩阵解析 OK — %d 层 (%s): %s"
                 % (len(names), kind, head), "ok")
        return True

    def do_mx_portfile(self):
        """从文本导入端口定义 (DSL: @端口名 [串联|并联] + 每行一段)。"""
        p = filedialog.askopenfilename(
            filetypes=[("端口定义文本", "*.txt;*.port;*.def"),
                       ("所有文件", "*.*")])
        if not p:
            return
        with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
            t = f.read()
        try:
            ports = mx_parse_port_blocks(t)
        except Exception as e:
            messagebox.showerror("matrix等效", "端口文本解析失败:\n%s" % e)
            return
        self.mx_ports = ports
        self._mx_refresh_ports()
        self.log("matrix等效: 已从文本导入 %d 个端口" % len(ports), "ok")

    def do_mx_portexport(self):
        """把当前端口定义导出为 DSL 文本 (可保存 / 复制)。"""
        if not self.mx_ports:
            messagebox.showwarning("matrix等效", "还没有端口定义")
            return
        out = []
        for p in self.mx_ports:
            out.append("@%s %s" % (p["name"],
                                    "串联" if p.get("chain_mode", "series") == "series"
                                    else "并联"))
            for s in p.get("segments", []):
                brs = s.get("branches", [])
                out.append(" | ".join(
                    "S: " + ", ".join(("-" if sg < 0 else "") + str(nm)
                                      for (nm, sg) in b) if len(b) > 1
                    else ("- " if b[0][1] < 0 else "") + str(b[0][0])
                    for b in brs))
            out.append("")
        text = "\n".join(out).strip() + "\n"
        win = tk.Toplevel(self.root)
        win.title("端口定义文本 (DSL)")
        win.geometry("720x420")
        win.resizable(True, True)
        win.minsize(520, 320)
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text="可编辑后保存；语法: @端口名 [串联|并联] 开端口, 每行一段, "
                            "S: 层1, 层2 = 支路内层串联, | 分隔并联支路, -层名 = 反接",
                  wraplength=690).pack(anchor="w", padx=8, pady=(8, 4))
        box = tk.Text(win, **text_kw())
        box.pack(fill="both", expand=True, padx=8)
        box.insert("1.0", text)
        btn = ttk.Frame(win)
        btn.pack(fill="x", padx=8, pady=8)

        def save():
            fp = filedialog.asksaveasfilename(
                defaultextension=".txt", filetypes=[("文本", "*.txt")],
                initialfile="ports.txt")
            if not fp:
                return
            with open(fp, "w", encoding="utf-8") as f:
                f.write(box.get("1.0", "end"))
            self.log("matrix等效: 端口定义已导出 " + fp, "ok")
            win.destroy()

        ttk.Button(btn, text="关闭", command=win.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btn, text="保存到文件…", command=save).pack(side="right")


    def _mx_edit_sel(self):
        sel = self.tv_mxP.selection()
        if sel:
            self._mx_edit_port(self.tv_mxP.index(sel[0]))

    def _mx_del_port(self):
        sel = self.tv_mxP.selection()
        if not sel:
            return
        i = self.tv_mxP.index(sel[0])
        if 0 <= i < len(self.mx_ports):
            del self.mx_ports[i]
            self._mx_refresh_ports()

    def _mx_refresh_ports(self):
        """刷新端口列表 (描述格式同 indcalc_gui: (W1串W2) ∥ (W3))。"""
        tv = self.tv_mxP
        tv.delete(*tv.get_children())
        for p in self.mx_ports:
            joiner = "串" if p.get("chain_mode", "series") == "series" else "∥"
            segs = []
            for s in p.get("segments", []):
                parts = ["(" + "串".join(("-" if sg < 0 else "") + str(nm)
                                         for (nm, sg) in b) + ")"
                         for b in s.get("branches", [])]
                if parts:
                    segs.append("∥".join(parts))
            tv.insert("", "end", values=(p.get("name", "?"), joiner.join(segs)))
        n = len(self.mx_ports)
        self.mx_st2.set("端口: %d 个" % n if n else "端口: 未定义")

    def _mx_edit_port(self, index=-1):
        """端口编辑器 (独立窗口): 端口名 / 段间连接 / 段(并联支路数) / 支路(串联层 + 反接)。

        交互与 indcalc_gui.edit_port 一致 (去掉超级端口部分)。
        """
        if not getattr(self, "mx_names", None):
            messagebox.showwarning(
                "matrix等效",
                "请先解析矩阵（导入 / 粘贴 / 从结果目录读 Matrix），\n"
                "端口定义需要从矩阵取层名。")
            return
        layer_choices = list(self.mx_names)
        target = self.mx_ports
        p0 = target[index] if 0 <= index < len(target) else None

        win = tk.Toplevel(self.root)
        win.title("端口编辑器" if p0 is None else
                  "端口编辑器 — %s" % p0.get("name", ""))
        win.geometry("860x620")
        win.resizable(True, True)
        win.minsize(760, 480)
        win.transient(self.root)
        win.grab_set()
        btnbar = ttk.Frame(win)
        btnbar.pack(side="bottom", fill="x", padx=8, pady=8)

        row = ttk.Frame(win)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Label(row, text="端口名:").pack(side="left")
        name_var = tk.StringVar(value=(p0["name"] if p0
                                       else "Port%d" % (len(target) + 1)))
        ttk.Entry(row, textvariable=name_var, width=14).pack(side="left", padx=6)
        ttk.Label(row, text="段间连接:").pack(side="left", padx=(14, 2))
        cur_chain = p0.get("chain_mode", "series") if p0 else "series"
        chain_var = tk.StringVar(value="串联" if cur_chain == "series" else "并联")
        ttk.Combobox(row, textvariable=chain_var, width=8, state="readonly",
                     values=["串联", "并联"]).pack(side="left", padx=6)
        ttk.Label(row, text="(段与段之间)",
                  style="Muted.TLabel").pack(side="left")
        avail = "、".join(layer_choices[:24]) + ("…" if len(layer_choices) > 24 else "")
        ttk.Label(win, text="可用层: " + avail, wraplength=1200,
                  foreground=PALETTE["accent"]).pack(anchor="w", padx=8)

        hint = ttk.Label(win, justify="left", text="")
        hint.pack(anchor="w", padx=8, pady=4)

        def _upd_hint(*_):
            if chain_var.get() == "串联":
                head, tail = "段间串联: 各段电流相同, 磁链相加", "→ [(W1串W2)∥W3] 串 (W4串W5)。"
            else:
                head, tail = "段间并联: 各段电压相同, 电流相加", "→ [(W1串W2)∥W3] ∥ (W4串W5)。"
            hint.config(text=head + "。段内可设 N 条并联支路:\n"
                        "  (支路内层串联, 支路间并联; 支路数=1 即该段所有层串联)\n"
                        "层名从下拉选择, 每槽勾选“反”表示反接; "
                        "每条支路可用“＋层”追加串联槽。\n"
                        "例: 段1 支路数2 → (W1串W2) ∥ (W3);  "
                        "段2 支路数1 → (W4串W5)\n" + tail)
        chain_var.trace_add("write", _upd_hint)
        _upd_hint()

        outer = ttk.Frame(win)
        outer.pack(fill="both", expand=True, padx=8, pady=(0, 2))
        canvas = tk.Canvas(outer, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        segbox = ttk.Frame(canvas)
        _wid = canvas.create_window((0, 0), window=segbox, anchor="nw")
        segbox.bind("<Configure>",
                    lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        def _resize_inner(e):
            # 窗口横向拉伸时, 内部段容器跟随变宽 (否则拉宽后内容仍挤在左边)
            try:
                canvas.itemconfig(_wid, width=e.width)
            except Exception:
                pass
        canvas.bind("<Configure>", _resize_inner)
        canvas.bind("<MouseWheel>",
                    lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        seg_rows = []

        def add_seg(branches_spec=None):
            n = len(seg_rows) + 1
            frm = ttk.LabelFrame(segbox, text="段 %d" % n, padding=2)
            frm.pack(fill="x", pady=3)
            top = ttk.Frame(frm)
            top.pack(fill="x", padx=6, pady=2)
            ttk.Label(top, text="并联支路数:").pack(side="left")
            nb_var = tk.StringVar()
            ttk.Combobox(top, textvariable=nb_var, width=3, state="readonly",
                         values=[str(i) for i in range(1, 9)]).pack(side="left", padx=4)
            ttk.Label(top, text="(每支路 = 若干层串联, 支路间并联)",
                      style="Muted.TLabel").pack(side="left")
            idx = len(seg_rows)
            ttk.Button(top, text="删除段", width=8,
                       command=lambda: del_seg(idx)).pack(side="right")
            box = ttk.Frame(frm)
            box.pack(fill="x", padx=6, pady=(0, 4))
            branches = []
            pending = [None]

            def add_branch(layers=None):
                r = ttk.Frame(box)
                r.pack(fill="x", pady=1)
                ttk.Label(r, text="支路%d:" % (len(branches) + 1)).pack(side="left")
                slotsf = ttk.Frame(r)
                slotsf.pack(side="left", fill="x", expand=True, padx=4)
                slots = []
                branches.append({"slots": slots})

                def add_slot(name="", sign=1):
                    f = ttk.Frame(slotsf)
                    f.pack(side="left", padx=(0, 4))
                    var = tk.StringVar(value=name)
                    ttk.Combobox(f, textvariable=var, width=14,
                                 values=layer_choices,
                                 state="readonly").pack(side="left")
                    rev = tk.BooleanVar(value=(sign < 0))
                    ttk.Checkbutton(f, text="反", variable=rev).pack(side="left",
                                                                    padx=(2, 0))
                    slots.append({"var": var, "rev": rev, "frm": f})

                if layers:
                    for nm, sgn in layers:
                        add_slot(nm, sgn)
                else:
                    add_slot()
                ttk.Button(r, text="＋层", width=4,
                           command=add_slot).pack(side="left")
                ttk.Button(r, text="－", width=2,
                           command=lambda: (slots.pop()["frm"].destroy()
                                            if len(slots) > 1 else None)).pack(side="left")

            def rebuild(*_):
                if pending[0] is not None:
                    old = pending[0]
                    pending[0] = None
                else:
                    old = [[(s["var"].get().strip(), -1 if s["rev"].get() else 1)
                            for s in br["slots"] if s["var"].get().strip()]
                           for br in branches]
                for w in box.winfo_children():
                    w.destroy()
                branches.clear()
                try:
                    n_b = int(nb_var.get())
                except ValueError:
                    n_b = 1
                for i in range(n_b):
                    add_branch(old[i] if i < len(old) else None)

            nb_var.trace_add("write", rebuild)
            if branches_spec is None:
                pending[0] = None
                nb_var.set("1")
            else:
                pending[0] = [[(nm, sgn) for (nm, sgn) in b] for b in branches_spec]
                nb_var.set(str(len(branches_spec)))
            seg_rows.append({"frm": frm, "nb": nb_var,
                             "branches": branches, "box": box})

        def del_seg(idx):
            if 0 <= idx < len(seg_rows) and seg_rows[idx] is not None:
                seg_rows[idx]["frm"].destroy()
                seg_rows[idx] = None

        bar2 = ttk.Frame(win)
        bar2.pack(fill="x", padx=8)
        ttk.Button(bar2, text="+ 添加段",
                   command=lambda: add_seg()).pack(side="left")
        ttk.Label(bar2, text="(每个段内: 多条并联支路, 每条支路内层串联)",
                  style="Muted.TLabel").pack(side="left", padx=8)

        if p0:
            chain_var.set("串联" if p0.get("chain_mode", "series") == "series"
                          else "并联")
            for s in p0.get("segments", []):
                if "branches" in s:
                    spec = [[(nm, sgn) for (nm, sgn) in b] for b in s["branches"]]
                else:
                    mode = s.get("mode", "parallel")
                    layers = s.get("layers", [])
                    spec = ([[(nm, sgn)] for (nm, sgn) in layers]
                            if mode == "parallel"
                            else [[(nm, sgn) for (nm, sgn) in layers]])
                add_seg(spec)
        else:
            add_seg()

        def ok():
            try:
                segs = []
                for r in seg_rows:
                    if r is None:
                        continue
                    branches = []
                    for br in r["branches"]:
                        layers = [(s["var"].get().strip(),
                                   -1 if s["rev"].get() else 1)
                                  for s in br["slots"] if s["var"].get().strip()]
                        if layers:
                            branches.append(layers)
                    if not branches:
                        raise ValueError("段内至少需要一条支路 (每条支路至少一个层)")
                    segs.append({"branches": branches})
                if not segs:
                    raise ValueError("端口至少需要一个段")
                p = {"name": name_var.get().strip() or "Port",
                     "chain_mode": ("series" if chain_var.get() == "串联"
                                    else "parallel"),
                     "segments": segs}
                if 0 <= index < len(target):
                    target[index] = p
                else:
                    target.append(p)
                self._mx_refresh_ports()
                win.destroy()
            except Exception as e:
                messagebox.showerror("端口定义错误", str(e), parent=win)

        ttk.Button(btnbar, text="取消", command=win.destroy).pack(side="right",
                                                                 padx=(6, 0))
        ttk.Button(btnbar, text="确定", command=ok,
                   style="Accent.TButton").pack(side="right")

    def _mx_render_si(self):
        """按当前输入单位把 SI 矩阵 (self.mx_si) 渲染成文本 -> 矩阵框 -> 重新解析。

        单元格用制表符分隔 (逗号才是 R,L 双值内部分隔, 见 indcalc tokenizer 规则)。
        """
        si = getattr(self, "mx_si", None)
        if not si:
            return
        names = si["names"]
        L, R = si["L"], si["R"]
        rsc = self._mx_rin()          # Ω / 单位
        lsc = self._mx_lin()          # H / 单位
        n = len(names)
        lines = []
        for i, nm in enumerate(names, 1):
            cells = []
            for j in range(1, n + 1):
                r = 0.0 if R is None else R[i - 1, j - 1] / rsc
                l = L[i - 1, j - 1] / lsc
                cells.append("%.8g, %.8g" % (r, l))
            lines.append(nm + "\t" + "\t".join(cells))
        self.mx_txt.delete("1.0", "end")
        self.mx_txt.insert("1.0", "\n".join(lines))
        self.do_mx_parse()

    def _mx_on_unit_change(self, *_):
        """输入单位 (电阻/电感) 改变: 结果目录读入的矩阵按新单位重新渲染并重解析。"""
        if not getattr(self, "mx_si", None):
            return                     # 手填 / 文本导入的矩阵不动, 避免覆盖用户输入
        self._mx_render_si()
        if self.mx_ports and getattr(self, "mx_Lp", None) is not None:
            self.do_mx_compute()       # 已算过则静默重算 (端口矩阵 + T 型)

    def _mx_on_outunit_change(self, *_):
        """输出单位改变: 重绘端口矩阵表 + T 型参数表 (不重算)。"""
        if getattr(self, "mx_Lp", None) is None:
            return
        self._mx_fill(self.tv_mxL, self.mx_Lp, self.mx_lu_out.get(),
                      dict(_icc.L_UNITS).get(self.mx_lu_out.get(), 1e-6))
        if getattr(self, "mx_Rp", None) is not None:
            self._mx_fill(self.tv_mxR, self.mx_Rp, self.mx_ru_out.get(),
                          dict(_icc.R_UNITS).get(self.mx_ru_out.get(), 1e-3))
        if self.mx_tprim.get() and self.mx_tsec.get():
            self.do_mx_t()

    def do_mx_compute(self):
        """主计算: 矩阵解析 -> 端口定义解析 -> 端口矩阵 (L/R) -> T 型等效。"""
        if _icc is None:
            messagebox.showerror("matrix等效", "indcalc_core / numpy 不可用, 无法计算")
            return
        if not self.do_mx_parse():
            return
        ports = list(self.mx_ports)
        if not ports:
            messagebox.showwarning("matrix等效",
                                   "请先点【添加端口】定义至少一个端口")
            return
        names = self.mx_names
        unknown = sorted({nm for p in ports for s in p["segments"]
                          for b in s["branches"] for (nm, _sg) in b} - set(names))
        if unknown:
            messagebox.showerror("matrix等效",
                                 "端口引用了矩阵中不存在的层名:\n"
                                 + ", ".join(unknown))
            return
        try:
            Lp, info = _icc.compute_port_matrix(
                self.mx_L, names, ports, keep_unused_open=self.mx_keep.get())
            Rp = None
            if self.mx_R is not None:
                Rp, _ = _icc.compute_port_matrix(
                    self.mx_R, names, ports, keep_unused_open=self.mx_keep.get())
        except Exception as e:
            messagebox.showerror("计算失败", str(e))
            return
        self.mx_Lp, self.mx_Rp = Lp, Rp
        self.mx_pnames = [p["name"] for p in ports]
        self.mx_st2.set("端口: %d 个 / %d 段" % (info["M"], info["K"]))
        self._mx_fill(self.tv_mxL, Lp, self.mx_lu_out.get(),
                      dict(_icc.L_UNITS).get(self.mx_lu_out.get(), 1e-6))
        if Rp is not None:
            self._mx_fill(self.tv_mxR, Rp, self.mx_ru_out.get(),
                          dict(_icc.R_UNITS).get(self.mx_ru_out.get(), 1e-3))
        else:
            self._mx_clear(self.tv_mxR)
        vals = self.mx_pnames
        self.cb_mxprim["values"] = vals
        self.cb_mxsec["values"] = vals
        if self.mx_tprim.get() not in vals:
            self.mx_tprim.set(vals[0])
        if len(vals) >= 2:
            if self.mx_tsec.get() not in vals or self.mx_tsec.get() == self.mx_tprim.get():
                self.mx_tsec.set(vals[1])
        else:
            self.mx_tsec.set("")
        unused = ", ".join(info.get("unused_layers", []))
        self.log("matrix等效: 端口矩阵 OK — %d 端口 / %d 段%s"
                 % (info["M"], info["K"],
                    ("；开路未用层: " + unused) if unused else ""), "ok")
        self.do_mx_t()

    def _mx_fill(self, tv, M, unit, scale):
        """M×M 端口矩阵填入 Treeview (SI 值按 scale 缩放为显示单位)。"""
        tv.delete(*tv.get_children())
        m = len(self.mx_pnames)
        tv["columns"] = ["p"] + ["c%d" % i for i in range(m)]
        tv.heading("p", text="(%s)" % unit)
        tv.column("p", width=95, anchor="w")
        for j, nm in enumerate(self.mx_pnames):
            tv.heading("c%d" % j, text=nm)
            tv.column("c%d" % j, width=85, anchor="e")
        for i in range(m):
            tv.insert("", "end", values=[self.mx_pnames[i]] +
                      ["%.6g" % (x / scale) for x in M[i]])

    def _mx_clear(self, tv):
        tv.delete(*tv.get_children())
        tv["columns"] = ["p"]
        tv.heading("p", text="(无电阻数据)")
        tv.column("p", width=95, anchor="w")

    def do_mx_t(self):
        """按当前原/副边选择计算两绕组 T 型等效并填表。"""
        if _icc is None or getattr(self, "mx_Lp", None) is None:
            return
        prim, sec = self.mx_tprim.get(), self.mx_tsec.get()
        if not prim or not sec or prim == sec:
            self.tv_mxT.delete(*self.tv_mxT.get_children())
            return
        try:
            te = _icc.compute_t_equiv(self.mx_Lp, self.mx_pnames, prim, sec)
        except Exception as e:
            self.log("matrix等效: T 型等效失败: %s" % e, "err")
            self.tv_mxT.delete(*self.tv_mxT.get_children())
            return
        sc = dict(_icc.L_UNITS).get(self.mx_lu_out.get(), 1e-6)
        u = self.mx_lu_out.get()
        rows = [
            ("Lpp 原边自感", "%.6g %s" % (te["Lpp"] / sc, u), ""),
            ("Lss 副边自感", "%.6g %s" % (te["Lss"] / sc, u), ""),
            ("M 互感", "%.6g %s" % (te["M"] / sc, u), "负值 = 反极性"),
            ("k 耦合系数", "%.6g" % te["k"], "= |M|/√(Lpp·Lss)"),
            ("n 匝比 Np/Ns", "%.6g" % te["n"], "= √(Lpp/Lss)"),
            ("Lkp 原边漏感", "%.6g %s" % (te["Lkp"] / sc, u), ""),
            ("Lks 副边漏感", "%.6g %s" % (te["Lks"] / sc, u), ""),
            ("Lm 励磁电感", "%.6g %s" % (te["Lm"] / sc, u), "= k·Lpp"),
            ("n1 折算匝比", "%.6g" % te["n1"], "= |M|/Lss"),
            ("Lkp1 折算原边漏感", "%.6g %s" % (te["Lkp1"] / sc, u),
             "副边漏感全折算到原边"),
            ("Lm1 折算励磁电感", "%.6g %s" % (te["Lm1"] / sc, u), ""),
        ]
        tv = self.tv_mxT
        tv["columns"] = ("p", "v", "d")
        for c, w, a in (("p", 128, "w"), ("v", 108, "e"), ("d", 150, "w")):
            tv.heading(c, text={"p": "参数", "v": "数值", "d": "说明"}[c])
            tv.column(c, width=w, anchor=a)
        tv.delete(*tv.get_children())
        for r in rows:
            tv.insert("", "end", values=r)
        self.log("matrix等效: T 型等效 %s/%s — k=%.4f  n=%.4f  Lm=%.4g H"
                 % (prim, sec, te["k"], te["n"], te["Lm"]), "ok")

    def do_mx_export(self):
        """端口矩阵 (L/R) + T 型参数导出 CSV。"""
        if getattr(self, "mx_Lp", None) is None:
            messagebox.showwarning("提示", "请先计算端口矩阵")
            return
        import csv as _csv
        p = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile="port_matrix.csv")
        if not p:
            return
        lsc = dict(_icc.L_UNITS).get(self.mx_lu_out.get(), 1e-6)
        rsc = dict(_icc.R_UNITS).get(self.mx_ru_out.get(), 1e-3)
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = _csv.writer(f)
            w.writerow(["[电感矩阵] 单位: " + self.mx_lu_out.get()]
                       + self.mx_pnames)
            for i, nm in enumerate(self.mx_pnames):
                w.writerow([nm] + ["%.8g" % (x / lsc) for x in self.mx_Lp[i]])
            if self.mx_Rp is not None:
                w.writerow([])
                w.writerow(["[电阻矩阵] 单位: " + self.mx_ru_out.get()]
                           + self.mx_pnames)
                for i, nm in enumerate(self.mx_pnames):
                    w.writerow([nm] + ["%.8g" % (x / rsc)
                                       for x in self.mx_Rp[i]])
        self.log("matrix等效: 已导出 " + p, "ok")

    # ---------------------------------------------------------- 日志/状态
    def log(self, s, tag=""):
        self.txt.insert(tk.END, s + "\n", tag)
        self.txt.see(tk.END)

    def on_state(self, running):
        st = "disabled" if running else "normal"
        self.b_scan.configure(state=st)
        self.b_stop.configure(state="normal" if running else "disabled")
        self.b_ohm.configure(state="disabled" if running else
                             ("normal" if self.sp_ohm.rbox.size() else "disabled"))
        self.b_cur.configure(state="disabled" if running else
                             ("normal" if self.sp_cur.rbox.size() else "disabled"))
        self.b_del.configure(state="disabled" if running else
                             ("normal" if self.sp_expr.rbox.size() else "disabled"))
        self.b_j.configure(state="disabled" if running else
                            ("normal" if self.sp_j.rbox.size() else "disabled"))
        self.b_temp.configure(state="disabled" if running else
                              ("normal" if self.sp_temp.rbox.size() else "disabled"))
        self.b_readtemp.configure(state="disabled" if running else
                                  ("normal" if self.sp_temp.rbox.size() else "disabled"))
        self.v_status.set("执行中…" if running else "就绪")

    def _sync_expr(self):
        if not self.runner.running:
            self.b_del.configure(state="normal" if self.sp_expr.rbox.size()
                                 else "disabled")

    def _sync_j(self):
        if not self.runner.running:
            self.b_j.configure(state="normal" if self.sp_j.rbox.size()
                               else "disabled")

    def _sync_bar(self):
        if not self.runner.running:
            has = bool(self.sp_bar.rbox.size())
            self.b_bar.configure(state="normal" if has else "disabled")

    def do_barall(self):
        if not messagebox.askyesno(
                "确认 — 生成/刷新总报表",
                "设计：%s / %s\n\n"
                "一次性读取场计算器全部 OhmicLoss_* 表达式数值\n"
                "（只读，不动解、不动网格）+ 建/复用总报表 LossBar_All。\n"
                "之后入栈实体画柱状图直接读 GUI 缓存，零 AEDT 交互。"
                % (self.state.get("project", "?"),
                   self.state.get("design", "?"))):
            return
        argv = [self.pyaedt_py.get(), BACKEND, "barloss-all",
                "--port", GRPC_PORT]
        if not self.v_bsave.get():
            argv.append("--no-save")
        self.runner.start(argv, tag="barall")

    def do_barloss(self):
        objs = self.sp_bar.stack
        if not objs:
            messagebox.showinfo("提示", "先把实体入栈")
            return
        pool = self.bar_pool or {}
        if not pool:
            messagebox.showinfo("提示", "先点【生成/刷新总报表】读取全部 "
                                        "OhmicLoss_* 数据")
            return
        if (self._bar_proj and
                self._bar_proj != self.state.get("project", "")) \
                or (self._bar_des and
                    self._bar_des != self.state.get("design", "")):
            messagebox.showinfo(
                "提示", "总报表缓存属于设计 %s / %s，与当前扫描不一致，"
                        "请重新【生成/刷新总报表】"
                        % (self._bar_proj, self._bar_des))
            return
        miss = [o for o in objs if o not in pool]
        if miss:
            messagebox.showwarning(
                "缓存缺少实体",
                "以下 %d 个实体不在总报表缓存中：\n%s\n\n"
                "可能刚创建了新表达式——请点【生成/刷新总报表】。"
                % (len(miss), ", ".join(miss[:12]) +
                   ("…" if len(miss) > 12 else "")))
            return
        vals = [pool[o] for o in objs]
        total = sum(vals)
        self.bar_data = (objs, vals, total, self._bar_report,
                         self._bar_proj, self._bar_des)
        self._open_bar_window()
        self.log("已从总报表缓存出图：%d 个实体，Σ = %.6g W（无 AEDT 交互）"
                 % (len(objs), total), "ok")

    def _parse_lossbarall_from_log(self):
        txt = self.txt.get("1.0", tk.END)
        line = None
        for ln in txt.splitlines():
            if ln.startswith("@@LOSSBARALL@@"):
                line = ln[len("@@LOSSBARALL@@"):]
        if not line:
            self.log("（日志中未找到 @@LOSSBARALL@@ 数据行）")
            return
        try:
            d = json.loads(line)
        except Exception as e:
            self.log("!! 损耗 JSON 解析失败: %s" % e, "err")
            return
        pool = d.get("pool") or {}
        self.bar_pool = {str(k): float(v) for k, v in pool.items()}
        self._bar_report = str(d.get("report", ""))
        self._bar_proj = str(d.get("project", ""))
        self._bar_des = str(d.get("design", ""))
        self._bar_sweep = str(d.get("sweep", "?"))
        self._bar_freq = d.get("freq")
        xf = ""
        if self._bar_sweep == "Freq" and self._bar_freq is not None:
            xf = " | Freq=%g" % self._bar_freq
        self.v_barinfo.set("总报表 %s | 缓存 %d 个表达式 | X=%s%s"
                           % (self._bar_report or "-", len(self.bar_pool),
                              self._bar_sweep, xf))
        self.log("总报表缓存已就绪：%d 个 OhmicLoss_* 表达式——"
                 "入栈实体后点【从缓存生成柱状图】，零 AEDT 交互"
                 % len(self.bar_pool), "ok")

    def _open_bar_window(self):
        if not self.bar_data:
            messagebox.showinfo(
                "提示", "先入栈实体并点【读取损耗并生成柱状图】")
            return
        old = self._barwin
        if old is not None and old.winfo_exists():
            old.deiconify()
            old.lift()
            return
        objs, vals, total, rep, proj, dsn = self.bar_data
        win = tk.Toplevel(self.root)
        self._barwin = win
        win.title("损耗柱状图 — %s / %s" % (proj, dsn))
        win.geometry("1280x840")
        win.minsize(640, 420)
        win.resizable(True, True)
        win.configure(bg="#F5F7FA")
        top = ttk.Frame(win)
        top.pack(fill="x", padx=10, pady=(8, 4))
        xinfo = ""
        if self._bar_sweep == "Freq" and self._bar_freq is not None:
            xinfo = " | Freq = %g" % self._bar_freq
        ttk.Label(top, text="Σ 合计 = %.6g W（%d 个实体）| 报表 %s | X=%s%s"
                  % (total, len(objs), rep or "-", self._bar_sweep, xinfo),
                  style="Muted.TLabel").pack(side="left")
        ttk.Button(top, text="复制图表",
                   command=self._bar_copy).pack(side="right", padx=4)
        ttk.Button(top, text="保存…",
                   command=self._bar_save).pack(side="right")
        self._bar_fig = None
        try:
            import matplotlib
            matplotlib.use("TkAgg")
            from matplotlib.backends.backend_tkagg import (
                FigureCanvasTkAgg)
            from matplotlib.figure import Figure
            import numpy as np
            fig = Figure(figsize=(9.4, 5.0), dpi=100)
            fig.patch.set_facecolor("#F5F7FA")
            ax = fig.add_subplot(111)
            ax.set_facecolor("#FFFFFF")
            for sp_ in ("top", "right"):
                ax.spines[sp_].set_visible(False)
            for sp_ in ("left", "bottom"):
                ax.spines[sp_].set_color("#E1E7EF")
            ax.tick_params(colors="#6B7280", labelsize=9, length=0)
            n = len(objs)
            x = np.arange(n)
            v = np.array(vals, dtype=float)
            ax.bar(x, v, width=(0.62 if n <= 16 else 0.42),
                   color="#2563EB", linewidth=0)
            ax.set_xticks(x)
            if n <= 20:
                ax.set_xticklabels(objs, rotation=30, ha="right")
                for xi, vv in zip(x, v):
                    ax.text(xi, vv, "%.4g" % vv, ha="center",
                            va="bottom", fontsize=8, color="#6B7280")
            else:
                ax.set_xticklabels([])
                ax.set_xlabel("共 %d 项 · 名称过多已隐藏" % n,
                              fontsize=8, color="#6B7280", labelpad=6)
            ax.set_ylabel("OhmicLoss (W)")
            ax.yaxis.grid(True, color="#E1E7EF", linewidth=0.8)
            ax.set_axisbelow(True)
            topv = float(v.max()) if n else 1.0
            ax.set_ylim(0, topv * 1.18 if topv > 0 else 1.0)
            t2 = "OhmicLoss 对比"
            if self._bar_sweep == "Freq" and self._bar_freq is not None:
                t2 += " | Freq = %g" % self._bar_freq
            ax.set_title(t2, pad=12, fontsize=11, color="#1F2937")
            fig.tight_layout()
            cv = FigureCanvasTkAgg(fig, master=win)
            cv.draw()
            cv.get_tk_widget().pack(fill="both", expand=True,
                                    padx=10, pady=(4, 10))
            self._bar_fig = fig
        except ImportError:
            cv = tk.Canvas(win, bg="#FFFFFF", highlightthickness=0)
            cv.pack(fill="both", expand=True, padx=10, pady=(4, 10))
            self._bar_draw_canvas(cv, objs, vals)
            # 拉伸窗口时按新尺寸重绘 (_bar_draw_canvas 自带 delete("all"), 不会叠加)
            cv.bind("<Configure>",
                    lambda e: self._bar_draw_canvas(cv, objs, vals))
            ttk.Label(win, text="（未安装 matplotlib：降级 Canvas 显示，"
                                "复制/保存为文本数据）",
                      style="Muted.TLabel").pack(pady=(0, 8))

    def _bar_copy(self):
        if not self.bar_data:
            messagebox.showinfo("提示", "没有数据")
            return
        fig = self._bar_fig
        if fig is not None:
            import subprocess
            import tempfile
            p = os.path.join(tempfile.gettempdir(), "aedt_bar_clip.png")
            try:
                fig.savefig(p, dpi=150, bbox_inches="tight")
            except Exception as e:
                self.log("!! 图片导出失败: %s" % str(e)[:90], "err")
                return
            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "Add-Type -AssemblyName System.Drawing;"
                  "[System.Windows.Forms.Clipboard]::SetImage("
                  "[System.Drawing.Image]::FromFile('%s'))" % p)
            try:
                subprocess.run(
                    ["powershell", "-STA", "-NoProfile", "-Command", ps],
                    check=True, timeout=20, creationflags=0x08000000)
                self.log("图表已复制到剪贴板（可直接粘贴）", "ok")
                return
            except Exception as e:
                self.log("图片复制失败（%s），改为复制文本数据"
                         % str(e)[:80])
        objs, vals, total, rep, proj, dsn = self.bar_data
        lines = ["entity\tOhmicLoss(W)"]
        lines += ["%s\t%.6g" % (o, v) for o, v in zip(objs, vals)]
        lines.append("SUM\t%.6g" % total)
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.log("已复制文本数据到剪贴板", "ok")

    def _bar_save(self):
        if not self.bar_data:
            messagebox.showinfo("提示", "没有数据")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("PDF", "*.pdf"),
                       ("SVG", "*.svg"), ("CSV 数据", "*.csv")])
        if not path:
            return
        fig = self._bar_fig
        if fig is not None and not path.lower().endswith(".csv"):
            try:
                fig.savefig(path, dpi=200, bbox_inches="tight")
                self.log("已保存：%s" % path, "ok")
                return
            except Exception as e:
                messagebox.showerror("保存失败", str(e))
                return
        objs, vals, total, rep, proj, dsn = self.bar_data
        try:
            import csv as _csv
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                w = _csv.writer(fh)
                w.writerow(["entity", "OhmicLoss(W)"])
                for o, v in zip(objs, vals):
                    w.writerow([o, v])
                w.writerow(["SUM", total])
            self.log("已保存：%s" % path, "ok")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _bar_draw_canvas(self, cv, objs, vals):
        cv.delete("all")
        w = max(cv.winfo_width(), 600)
        h = max(cv.winfo_height(), 380)
        n = len(objs)
        ml, mr, mt, mb = 64, 16, 40, 34
        pw = w - ml - mr
        ph = h - mt - mb
        vmax = max(vals) if vals else 0.0
        top = vmax * 1.18 if vmax > 0 else 1.0
        for i in range(5):
            frac = i / 4.0
            y = mt + ph - ph * frac
            cv.create_line(ml, y, w - mr, y, fill="#E1E7EF")
            cv.create_text(ml - 6, y, anchor="e", fill="#6B7280",
                           font=("Microsoft YaHei", 8),
                           text="%g" % (top * frac))
        slot = pw / float(n) if n else pw
        bw = min(slot * 0.62, 56.0)
        hide = n > 20
        for i, (o, v) in enumerate(zip(objs, vals)):
            x0 = ml + slot * i + (slot - bw) / 2.0
            bh = ph * (v / top) if top > 0 else 0
            y0 = mt + ph - bh
            cv.create_rectangle(x0, y0, x0 + bw, mt + ph,
                                fill="#2563EB", outline="")
            if hide:
                cv.create_text(x0 + bw / 2.0, y0 - 8, angle=90,
                               anchor="w", fill="#6B7280",
                               font=("Microsoft YaHei", 7),
                               text="%.4g" % v)
            else:
                cv.create_text(x0 + bw / 2.0, y0 - 6, anchor="s",
                               fill="#6B7280",
                               font=("Microsoft YaHei", 8),
                               text="%.4g" % v)
                cv.create_text(x0 + bw / 2.0, mt + ph + 6, anchor="n",
                               fill="#1F2937",
                               font=("Microsoft YaHei", 8),
                               text=o)
        cv.create_line(ml, mt + ph, w - mr, mt + ph, fill="#E1E7EF")

    def _do_mpush(self):
        if not self.matchbox.size():
            messagebox.showinfo("提示", "先【筛选匹配实体】")
            return
        tgt = self.cb_mtarget.get()
        sp = {"温度赋值": self.sp_temp, "Ohmic Loss 积分": self.sp_ohm,
              "电流积分": self.sp_cur, "电流密度场图": self.sp_j,
              "损耗柱状图": self.sp_bar}.get(tgt)
        if sp is None:
            return
        have = set(sp.stack)
        added = 0
        for i in range(self.matchbox.size()):
            name = self.matchbox.get(i).split()[0]
            if name not in have:
                sp.rbox.insert(tk.END, name)
                have.add(name)
                added += 1
        sp._refresh()
        self.log("匹配结果入栈：共 %d 个 → 「%s」（新增 %d，已在栈中跳过 %d）"
                 % (self.matchbox.size(), tgt, added,
                    self.matchbox.size() - added), "ok")

    def _sync_temp(self):
        if not self.runner.running:
            has = bool(self.sp_temp.rbox.size())
            self.b_temp.configure(state="normal" if has else "disabled")
            self.b_readtemp.configure(state="normal" if has
                                      else "disabled")

    def _fill_temps(self, d):
        """扫描后填入全部 copper 的当前温度（来源：工程文件温度表）。"""
        temps = d.get("temps") or {}
        cu = d.get("copper", [])
        self.tempbox.delete(0, tk.END)
        for o in cu:
            self.tempbox.insert(tk.END, "%-28s %s"
                                % (o, self._fmt_temp(temps.get(o))))
        n_set = sum(1 for o in cu if temps.get(o))
        self.v_tempinfo.set("扫描读取 %d 个 copper，%d 个已赋温"
                            % (len(cu), n_set))

    def do_readtemp(self):
        objs = self.sp_temp.stack
        if not objs:
            messagebox.showinfo("提示", "先把实体入栈")
            return
        argv = [self.pyaedt_py.get(), BACKEND, "temp", "--port", GRPC_PORT,
                "--objects", ",".join(objs), "--read-only"]
        self.runner.start(argv, tag="readtemp")

    @staticmethod
    def _fmt_temp(v):
        if not v:
            return "（未设置）"
        s = str(v).strip()
        low = s.lower()
        try:
            if low.endswith("cel"):
                return "%g °C" % float(s[:-3])
            if low.endswith("kel"):
                return "%g °C" % (float(s[:-3]) - 273.15)
        except Exception:
            pass
        return s

    def _parse_temps_from_log(self):
        txt = self.txt.get("1.0", tk.END)
        line = None
        for ln in txt.splitlines():
            if ln.startswith("@@TEMPS@@"):
                line = ln[len("@@TEMPS@@"):]
        if not line:
            self.log("（日志中未找到 @@TEMPS@@ 数据行）")
            return
        try:
            d = json.loads(line)
        except Exception as e:
            self.log("!! 温度 JSON 解析失败: %s" % e, "err")
            return
        self.tempbox.delete(0, tk.END)
        for o in self.sp_temp.stack:
            self.tempbox.insert(tk.END, "%-28s %s"
                                % (o, self._fmt_temp(d.get(o))))
        self.v_tempinfo.set("已更新 %d 个实体（来源：工程文件温度表）"
                            % self.tempbox.size())

    def do_jplot(self):
        objs = self.sp_j.stack
        if not objs:
            messagebox.showinfo("提示", "先把实体入栈")
            return
        types = []
        if self.v_jmag.get():
            types.append("Mag")
        if self.v_jvec.get():
            types.append("Vector")
        if not types:
            messagebox.showinfo("提示", "至少勾一种 J 类型（Mag / Vector）")
            return
        phase = self.v_jphase.get().strip() or "0deg"
        surf = self.v_jsurface.get()
        if not messagebox.askyesno(
                "确认 — 电流密度场图",
                "设计：%s / %s\n实体：%d 个\nJ 类型：%s\n相位：%s\n"
                "场图类型：%s\n\n"
                "将在每个实体上建 J 场图：**场图名 = 实体名**\n"
                "文件夹分别为 Mag_J / Vector_J；同名旧场图会被删除重建。\n"
                "不改几何、不动网格、不影响结果。"
                % (self.state.get("project", "?"), self.state.get("design", "?"),
                   len(objs), " + ".join(types), phase,
                   "纯表面（surface only）" if surf else "体绘制")):
            return
        argv = [self.pyaedt_py.get(), BACKEND, "jplot", "--port", GRPC_PORT,
                "--objects", ",".join(objs),
                "--types", ",".join(types),
                "--phase", phase]
        if surf:
            argv.append("--surface-only")
        if not self.v_jsave.get():
            argv.append("--no-save")
        self.runner.start(argv, tag="jplot")

    def do_temp(self):
        objs = self.sp_temp.stack
        if not objs:
            messagebox.showinfo("提示", "先把实体入栈")
            return
        try:
            tv = float(self.v_temp.get().strip())
        except Exception:
            messagebox.showerror("错误", "温度不是数字: %r" % self.v_temp.get())
            return
        dep = self.v_tdep.get()
        fb = self.v_tfb.get()
        if not messagebox.askyesno(
                "确认 — 温度赋值",
                "设计：%s / %s\n实体：%d 个\n温度：%g °C\n"
                "包含温度依赖：%s    热反馈：%s\n\n"
                "底层命令 = Maxwell Fields → Set Object Temperature。\n"
                "不改几何、不动网格；但属求解设置修改：\n"
                "⚠ 已有解会被标记失效，重新求解后温度才体现在结果里。\n"
                "材料需带温度系数（thermal modifier）温度才影响损耗。"
                % (self.state.get("project", "?"), self.state.get("design", "?"),
                   len(objs), tv, "开" if dep else "关", "开" if fb else "关")):
            return
        argv = [self.pyaedt_py.get(), BACKEND, "temp", "--port", GRPC_PORT,
                "--objects", ",".join(objs), "--temp", "%g" % tv,
                "--dep", "1" if dep else "0",
                "--feedback", "1" if fb else "0"]
        if not self.v_tsave.get():
            argv.append("--no-save")
        self.runner.start(argv, tag="temp")

    def _sync_ohm(self):
        if not self.runner.running:
            self.b_ohm.configure(state="normal" if self.sp_ohm.rbox.size()
                                 else "disabled")

    def _sync_cur(self):
        if not self.runner.running:
            self.b_cur.configure(state="normal" if self.sp_cur.rbox.size()
                                 else "disabled")
        objs = self.sp_cur.stack
        if objs and not self.v_report.get().startswith("I_sec"):
            self.v_report.set(self._auto_name(objs))

    # --------------------------------------------------- 删除场计算器变量
    def do_dropvars(self):
        names = [e.split("=")[0].strip() for e in self.sp_expr.stack]
        if not names:
            messagebox.showinfo("提示", "先把要删除的变量入栈")
            return
        if not messagebox.askyesno(
                "确认删除命名表达式",
                "将删除以下 %d 个场计算器变量：\n\n%s\n\n"
                "设计：%s / %s\n\n"
                "只删表达式本身，不动几何、不动网格、不删剖面物体。\n"
                "⚠ 若某变量正被报表引用，该报表会取不到数。"
                % (len(names),
                   "\n".join(names[:15]) + ("\n…" if len(names) > 15 else ""),
                   self.state.get("project", "?"), self.state.get("design", "?"))):
            return
        self.sp_expr.clear()
        argv = [self.pyaedt_py.get(), BACKEND, "dropvars", "--port", GRPC_PORT,
                "--names", ",".join(names)]
        if not self.v_dsave.get():
            argv.append("--no-save")
        self.runner.start(argv, tag="dropvars")

    @staticmethod
    def _auto_name(objs):
        nums = [o.split("_")[-1] for o in objs]
        if len(nums) <= 4:
            return "I_sec_%s_vs_Phase" % "_".join(nums)
        return "I_sec_%s_etc%d_vs_Phase" % (nums[0], len(nums))

    # ------------------------------------------------------------ 扫描
    def _apply_pools(self, d=None):
        """按体积排序设置重建 OhmicLoss / 电流积分两个候选池。"""
        d = d if d is not None else self.state
        if not d:
            return
        copper = list(d.get("copper", []))
        g = d.get("groups", {})
        allobjs = sorted(set(copper) |
                         set(k for k, v in g.items() if v == "Solids"))
        vols = d.get("volumes") or {}
        mode = self.v_sort.get()
        if vols and (mode.startswith("升") or mode.startswith("降")):
            rev = mode.startswith("降")
            withv = [c for c in copper if isinstance(vols.get(c), (int, float))]
            nov = [c for c in copper if not isinstance(vols.get(c), (int, float))]
            cu = sorted(withv, key=lambda n: float(vols[n]), reverse=rev) + nov
            rest = [o for o in allobjs if o not in set(copper)]
            self.sp_ohm.set_pool(cu, cu + rest)
            self.sp_cur.set_pool(cu, cu + rest)
            self.sp_j.set_pool(cu, cu + rest)
            self.sp_temp.set_pool(cu, cu + rest)
            self.sp_bar.set_pool(cu, cu + rest)
            self._log_ranking(cu, vols, rev)
        else:
            self.sp_ohm.set_pool(copper, allobjs)
            self.sp_cur.set_pool(copper, allobjs)
            self.sp_j.set_pool(copper, allobjs)
            self.sp_temp.set_pool(copper, allobjs)
            self.sp_bar.set_pool(copper, allobjs)

    def _log_ranking(self, cu, vols, rev):
        self.log("copper 体积排序（%s，共 %d 个，模型单位³）:"
                 % ("大→小" if rev else "小→大", len(cu)))
        for i, n in enumerate(cu, 1):
            v = vols.get(n)
            self.log("  %3d. %-30s %s" % (i, n, "%.6g" % v
                                          if isinstance(v, (int, float)) else "?"))

    def _on_sort_changed(self):
        self._apply_pools()
        self._refresh_vol_tab()

    def _refresh_vol_tab(self):
        d = self.state or {}
        vols = d.get("volumes") or {}
        self.volbox.delete(0, tk.END)
        if not vols:
            self.v_volinfo.set("未扫描 —— 先点【扫描当前设计】读取体积/坐标")
            self.cb_ref["values"] = ()
            return
        items = sorted(((n, float(v)) for n, v in vols.items()
                        if isinstance(v, (int, float))), key=lambda t: t[1])
        rev = self.v_sort.get().startswith("降")
        if rev:
            items.reverse()
        for i, (n, v) in enumerate(items, 1):
            self.volbox.insert(tk.END, "%4d.  %-30s %14.6g" % (i, n, v))
        self.v_volinfo.set("共 %d 个 copper 实体（%s，模型单位³）"
                           % (len(items), "大→小" if rev else "小→大"))
        names = [n for n, _ in items]
        self.cb_ref["values"] = names
        if self.cb_ref.get() not in names:
            self.cb_ref.set("")

    def _do_match(self):
        d = self.state or {}
        vols = d.get("volumes") or {}
        cents = d.get("centers") or {}
        ref = self.cb_ref.get().strip()
        if not vols:
            messagebox.showinfo("提示", "先【扫描当前设计】获取体积/坐标数据")
            return
        if ref not in vols:
            messagebox.showinfo("提示", "参考实体 \"%s\" 不在 copper 体积表里" % ref)
            return
        try:
            tol_pct = abs(float(self.v_tol.get()))
        except Exception:
            messagebox.showerror("错误", "容差不是数字: %r" % self.v_tol.get())
            return
        use_vol = self.v_matchvol.get()
        use_xy = self.v_matchxy.get()
        if not use_vol and not use_xy:
            messagebox.showinfo("提示", "至少勾一个匹配条件（按体积 / 按 X-Y 坐标）")
            return
        v0 = float(vols[ref]) if use_vol else None
        if use_xy and ref not in cents:
            messagebox.showinfo("提示", "参考实体没有中心坐标数据，请重新扫描")
            return
        c0 = cents.get(ref) if use_xy else None
        rows = []
        for n, v in vols.items():
            if not isinstance(v, (int, float)):
                continue
            score = 0.0
            dv = dx = dy = None
            if use_vol:
                dv = (float(v) - v0) / v0 * 100.0 if v0 else float("inf")
                if abs(dv) > tol_pct:
                    continue
                score += abs(dv)
            if use_xy:
                c = cents.get(n)
                dx = self._coord_dev(c[0], c0[0]) if c else None
                dy = self._coord_dev(c[1], c0[1]) if c else None
                if dx is None or dy is None or abs(dx) > tol_pct \
                        or abs(dy) > tol_pct:
                    continue
                score += abs(dx) + abs(dy)
            rows.append((score, n, float(v), dv, dx, dy))
        mode = self.cb_msort.get()
        rev = self.cb_mdir.get() == "逆序"
        if mode == "Z 坐标":
            if not any(c and len(c) >= 3 for c in cents.values()):
                messagebox.showinfo(
                    "提示", "当前扫描数据没有 Z 坐标（旧版本扫描），请重新扫描")
                return
            rows.sort(key=lambda r: (cents.get(r[1]) or (0, 0, 0))[2]
                      if cents.get(r[1]) else float("-inf"),
                      reverse=rev)
        elif mode == "体积":
            rows.sort(key=lambda r: r[2], reverse=rev)
        else:
            rows.sort(reverse=rev)  # 偏差：正序=匹配优先（默认），逆序=偏差大优先
        self.matchbox.delete(0, tk.END)
        for _, n, v, dv, dx, dy in rows:
            zc = cents.get(n)
            ztxt = ""
            if mode == "Z 坐标" and zc and len(zc) >= 3:
                ztxt = "  z=%12.6g" % zc[2]
            if use_vol and use_xy:
                self.matchbox.insert(tk.END,
                    "%-26s %12.6g  ΔV%+7.2f%%  Δx%+7.2f%%  Δy%+7.2f%%%s"
                    % (n, v, dv, dx, dy, ztxt))
            elif use_vol:
                self.matchbox.insert(tk.END,
                    "%-26s %12.6g  ΔV%+7.2f%%%s" % (n, v, dv, ztxt))
            else:
                self.matchbox.insert(tk.END,
                    "%-26s %12.6g  Δx%+7.2f%%  Δy%+7.2f%%%s"
                    % (n, v, dx, dy, ztxt))
        cond = ("体积" if use_vol else "") \
               + ("+" if (use_vol and use_xy) else "") \
               + ("X/Y坐标" if use_xy else "")
        ord_txt = mode + ("·逆序" if rev else "·正序")
        self.v_matchinfo.set("匹配 %d / %d 个（参考 %s，±%.4g%%，按 %s，%s）"
                             % (len(rows), len(vols), ref, tol_pct, cond,
                                ord_txt))
        self.log("体积匹配：参考 %s（±%.4g%%，按 %s，%s）→ %d 个"
                 % (ref, tol_pct, cond, ord_txt, len(rows)), "ok")

    def _do_zfilter(self):
        d = self.state or {}
        cents = d.get("centers") or {}
        if not cents:
            messagebox.showinfo("提示", "先【扫描当前设计】获取中心坐标")
            return
        sample = next(iter(cents.values()), None)
        if not sample or len(sample) < 3:
            messagebox.showinfo(
                "提示", "当前扫描数据没有 Z 坐标（旧版本扫描），请重新扫描")
            return
        zlo = zhi = None
        try:
            s1 = self.v_zlo.get().strip()
            s2 = self.v_zhi.get().strip()
            if s1:
                zlo = float(s1)
            if s2:
                zhi = float(s2)
        except Exception:
            messagebox.showerror("错误", "阈值不是数字")
            return
        if zlo is None and zhi is None:
            messagebox.showinfo("提示", "至少填一个阈值（z ≥ 或 z ≤，留空=该边不限）")
            return
        novia = self.v_znovia.get()
        rows = []
        n_via = 0
        for n, c in cents.items():
            if len(c) < 3:
                continue
            if novia and "via" in n.lower():
                n_via += 1
                continue
            z = float(c[2])
            if zlo is not None and z < zlo:
                continue
            if zhi is not None and z > zhi:
                continue
            rows.append((n, z))
        rows.sort(key=lambda t: (t[1], t[0]))
        self.zbox.delete(0, tk.END)
        for n, z in rows:
            self.zbox.insert(tk.END, "%-26s z=%+10.4g" % (n, z))
        cond = ("z ≥ %g" % zlo if zlo is not None else "z ≥ -∞") \
            + " 且 " + ("z ≤ %g" % zhi if zhi is not None else "z ≤ +∞")
        if novia and n_via:
            cond += "，已排除 via ×%d" % n_via
        self.v_zinfo.set("筛选出 %d / %d 个（%s）"
                         % (len(rows), len(cents), cond))
        self.log("Z 坐标筛选：%s → %d 个（bbox 中心 z，模型单位）"
                 % (cond, len(rows)), "ok")

    def _do_zpush(self):
        if not self.zbox.size():
            messagebox.showinfo("提示", "先筛选出实体")
            return
        tgt = self.cb_ztarget.get()
        sp = {"温度赋值": self.sp_temp, "Ohmic Loss 积分": self.sp_ohm,
              "电流积分": self.sp_cur, "电流密度场图": self.sp_j,
              "损耗柱状图": self.sp_bar}.get(tgt)
        if sp is None:
            return
        have = set(sp.stack)
        added = 0
        for i in range(self.zbox.size()):
            name = self.zbox.get(i).split()[0]
            if name not in have:
                sp.rbox.insert(tk.END, name)
                have.add(name)
                added += 1
        sp._refresh()
        self.log("Z 筛选结果入栈：共 %d 个 → 「%s」（新增 %d，已在栈中跳过 %d）"
                 % (self.zbox.size(), tgt, added,
                    self.zbox.size() - added), "ok")

    @staticmethod
    def _coord_dev(c, c0):
        """坐标相对偏差 %（基准=坐标数值本身，2026-09-03 用户指定）。"""
        if c is None or c0 is None:
            return None
        if abs(c0) > 1e-12:
            return (c - c0) / abs(c0) * 100.0
        return 0.0 if abs(c - c0) <= 1e-12 else None

    def _browse_py(self):
        p = filedialog.askopenfilename(
            title="选择带 PyAEDT 的 python.exe",
            filetypes=[("Python 解释器", "python.exe"), ("所有文件", "*.*")])
        if p:
            self.pyaedt_py.set(p)

    def _save_cfg(self):
        self.cfg["python"] = self.pyaedt_py.get()
        if save_cfg(self.cfg):
            self.log("设置已保存：%s" % self.pyaedt_py.get(), "ok")
            messagebox.showinfo("已保存", "解释器路径已写入\n%s" % CONFIG_FILE)
        else:
            messagebox.showerror("保存失败", "无法写入\n%s" % CONFIG_FILE)

    def do_scan(self):
        self.v_status.set("扫描中…")
        self.runner.start([self.pyaedt_py.get(), BACKEND, "scan", "--port", GRPC_PORT],
                          tag="scan")

    def on_done(self, tag, code):
        if tag == "scan" and code == 0:
            self.apply_scan()
        elif tag in ("ohmic", "current", "dropvars") and code == 0:
            self.log("（自动刷新扫描结果）")
            self.root.after(300, self.do_scan)
        elif tag in ("temp", "readtemp") and code == 0:
            self._parse_temps_from_log()
            if tag == "temp":
                self.sp_temp.clear()
                self.log("温度赋值完成，温度赋值栈已清空", "ok")
                self.log("（自动刷新扫描结果）")
                self.root.after(300, self.do_scan)
        elif tag == "barall" and code == 0:
            self._parse_lossbarall_from_log()

    def apply_scan(self):
        """从日志里取最后一行 @@JSON@@ 解析。"""
        txt = self.txt.get("1.0", tk.END)
        line = None
        for ln in txt.splitlines():
            if ln.startswith("@@JSON@@"):
                line = ln[len("@@JSON@@"):]
        if not line:
            self.v_status.set("扫描完成，但未取到数据")
            return
        try:
            d = json.loads(line)
        except Exception as e:
            self.log("!! JSON 解析失败: %s" % e, "err")
            return
        self.state = d
        self.v_proj.set("工程: %s" % d.get("project", "?"))
        self.v_des.set("设计: %s" % d.get("design", "?"))
        self.v_cs.set("坐标系: %s（共 %s 个，脚本不切换）"
                      % (d.get("cs", "?"), d.get("ncs", "?")))
        g = d.get("groups", {})
        self.v_objs.set("物体: %d（%s）" % (
            sum(g.values()), " ".join("%s=%d" % (k, v)
                                      for k, v in sorted(g.items()))))
        self.v_cu.set("copper 实体: %d" % len(d.get("copper", [])))
        self.v_sec.set("剖面(Sheets): %d / 共 %d sheets"
                       % (len(d.get("sections", [])), len(d.get("sheets", []))))
        self.v_expr.set("表达式: %d（OhmicLoss %d / I_sec %d）"
                        % (d.get("expr_total", 0), d.get("n_ohmic", 0),
                           d.get("n_isec", 0)))
        r = d.get("results", {})
        self.v_res.set("结果: %d 个文件 / %.1f MB" % (r.get("files", 0),
                                                     r.get("mb", 0.0)))
        fpl = d.get("fplots", [])
        self.v_fpl.set("场图: %d" % len(fpl))
        if fpl:
            self.log("场图 (%d): %s" % (len(fpl), ", ".join(fpl)))
        self.v_status.set("就绪")

        # 场计算器变量
        self.sp_expr.set_pool(d.get("expressions", []))

        # 候选池（排序方式见「体积排序 / 匹配」tab）
        self._apply_pools(d)
        self._refresh_vol_tab()

        # 剖面清单
        self.secbox.delete(0, tk.END)
        nm = set(d.get("nonmodel", []))
        for n in d.get("sections", []) or d.get("sheets", []):
            flag = "Non-model" if n in nm or n in d.get("sheets", []) \
                else "Model?"
            self.secbox.insert(tk.END, "  %-46s %s" % (n, flag))
        self.v_secinfo.set("剖面 / Sheets 共 %d 个（OLD_ 前缀 %d 个）"
                           % (self.secbox.size(), len(d.get("old", []))))
        self.zbox.delete(0, tk.END)
        self.v_zinfo.set("设计已更新，请重新筛选")
        self._fill_temps(d)
        if self.bar_data and (self.bar_data[4] != d.get("project", "")
                              or self.bar_data[5] != d.get("design", "")):
            self.bar_data = None
            self.bar_pool = None
            w = self._barwin
            if w is not None and w.winfo_exists():
                w.destroy()
            self._barwin = None
            self.v_barinfo.set("设计已切换，损耗数据已清空，请重新读取")
        self._sync_ohm()
        self._sync_cur()
        self._sync_temp()

    # ------------------------------------------------------- Ohmic Loss
    def do_ohmic(self):
        objs = self.sp_ohm.stack
        if not objs:
            messagebox.showinfo("提示", "先把实体入栈")
            return
        pre = self.v_prefix.get().strip() or "OhmicLoss_"
        if not messagebox.askyesno(
                "确认", "将对以下 %d 个实体创建 %s<实体名> 场计算器变量：\n\n%s\n\n"
                        "设计：%s / %s\n（不改几何、不动网格、不影响结果）"
                        % (len(objs), pre, ", ".join(objs[:12]) +
                           ("…" if len(objs) > 12 else ""),
                           self.state.get("project", "?"),
                           self.state.get("design", "?"))):
            return
        argv = [self.pyaedt_py.get(), BACKEND, "ohmic", "--port", GRPC_PORT,
                "--objects", ",".join(objs), "--prefix", pre]
        if not self.v_osave.get():
            argv.append("--no-save")
        self.runner.start(argv, tag="ohmic")

    # --------------------------------------------------------- 电流积分
    def do_current(self):
        objs = self.sp_cur.stack
        if not objs:
            messagebox.showinfo("提示", "先把实体入栈")
            return
        plane = self.v_plane.get()
        rep = self.v_report.get().strip() or self._auto_name(objs)
        if not messagebox.askyesno(
                "确认 — 电流积分",
                "设计：%s / %s\n实体：%s\n剖切面：%s\n报表：%s\n\n"
                "流程：建 Non-model 剖面 → 保留最大片 → 场计算器电流积分\n"
                "      → Maxwell 内建 Fields Report（X=Phase）→ 正弦拟合\n"
                "      → 结果目录指纹校验 → 保存\n\n"
                "⚠ 剖面为 Non-model，不改变网格；但会新增 sheet 物体。"
                % (self.state.get("project", "?"), self.state.get("design", "?"),
                   ", ".join(objs[:12]) + ("…" if len(objs) > 12 else ""),
                   plane, rep)):
            return
        argv = [self.pyaedt_py.get(), PIPELINE,
                "--objects", ",".join(objs),
                "--plane", plane,
                "--report", rep,
                "--project", self.state.get("project", ""),
                "--design", self.state.get("design", ""),
                "--port", GRPC_PORT, "--no-png"]
        if not self.v_csave.get():
            argv.append("--no-save")
        if not self.v_cbackup.get():
            argv.append("--no-backup")
        self.runner.start(argv, tag="current")


def _set_taskbar_appid():
    """Windows：给进程一个独立 AppUserModelID，任务栏才会用自己的图标。

    不设的话，窗口会被归到宿主 python.exe 名下，任务栏显示 python 的图标
    （root.iconbitmap 只管标题栏和窗口自身图标，管不到任务栏分组）。
    """
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "WorkBuddy.MaxwellPost.1")
    except Exception:
        pass


def main():
    global SCALE
    cfg = load_cfg()
    if not os.path.isfile(cfg["python"]):
        messagebox.showwarning(
            "缺少 PyAEDT 解释器",
            "未找到：\n%s\n\n请在顶部「环境设置」里指定带 PyAEDT 的 python.exe，"
            "或把本程序放到含 env\\Scripts\\python.exe 的目录下再运行。" % cfg["python"])
    _set_taskbar_appid()         # 必须在建窗口前：任务栏图标归本进程
    SCALE = _enable_hidpi()      # 必须在建 Tk 窗口之前调用
    root = tk.Tk()
    setup_style(root)            # 现代浅色主题（clam 定制）
    _ico = os.path.join(APP_DIR, "maxwell_m.ico")
    if os.path.isfile(_ico):
        try:
            root.iconbitmap(_ico)   # 窗口/任务栏图标（Maxwell 风格 m）
        except Exception:
            pass
    App(root, cfg)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
