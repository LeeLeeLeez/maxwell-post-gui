# -*- coding: utf-8 -*-
"""
aedt_gui_backend.py —— aedt_gui.py 的后端（跑在带 PyAEDT 的 venv 里）

子命令:
    scan                        扫描【当前激活的设计】，最后输出一行
                                @@JSON@@{...}  供 GUI 解析
    ohmic  --objects a,b,c      对指定 copper 实体建 OhmicLoss_* 场计算器变量
    jplot  --objects a,b,c      对指定实体建 J 场图（Mag/Vector），名=实体名
    temp   --objects a,b,c      对指定实体执行 Set Object Temperature；
                                加 --read-only 只读取当前温度（不赋值不保存）
    dropvars --names a,b,c      删除场计算器命名表达式（只删表达式，不动几何）
    delsheets --objects a,b,c   删除剖面/片体（仅 Sheets/Non Model 组，不碰 Solids）
    oldprefix --objects a,b,c   给片体加 OLD_ 前缀（改名不删除；已带前缀跳过）
    delreports --names a,b       删除报表（ReportSetup.DeleteReports）
    delfplots  --names a,b       删除场图（FieldsReporter.DeleteFieldPlot）
    barloss-all                 一次性读取场计算器全部 OhmicLoss_* 表达式
                                （PyAEDT get_solution_data，X=Freq 自动回退
                                Phase；坑 #46）+ 建/复用总报表 LossBar_All
                                （存在即复用，绝不重建），
                                输出 @@LOSSBARALL@@{pool...} 供 GUI 缓存；
                                之后入栈实体出图纯 GUI 本地，零 AEDT 交互
    irms   --items a:Sec_a_Section1:ScalarY,...
                                场计算器 Integrate(Surface(片),
                                CmplxMag(Scalar?(<Jx,Jy,Jz>))) = 电流【峰值】，
                                有效值 = 峰值/sqrt(2)；全程在 AEDT 内部
                                IronPython 完成（CalculatorWrite → .fld），
                                不建报表、不建命名表达式、不动几何，
                                输出 @@CURBAR@@{...}
    mats                        列出设计里出现的所有材料及实体数（调试用）

铁律:
  * 只操作【当前激活的工程/设计】—— 不切工程、不切坐标系（用户已选好）
  * ohmic 不改几何、不动网格、不影响结果
  * ohmic 走 AEDT 内部 IronPython 批处理（gRPC 封送坑 #36：
    EnterVol 之类带变体的调用在 gRPC 下不稳，用 RunScript 绕开）
  * 几何类操作（剖面）一律 Non-model —— 见 current_integral_pipeline.py
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time

from aedt_env import connect, disconnect, get_design, GRPC_PORT
from section_cs import (obj_map, model_flag, exprs, drop_var,
                        snap_results, diff_results)

# ---------------------------------------------------------------- IronPython
# 在 AEDT 内部执行的 IronPython 2.7 脚本模板。
# 注意：AEDT 内的 IronPython 不受 gRPC 封送问题影响（坑 #36/#42 都是 gRPC 侧的问题）。
OHMIC_TEMPLATE = r'''# -*- coding: utf-8 -*-
OUT = r"__OUT__"
OBJS = __OBJS__
PREFIX = "__PREFIX__"

f = open(OUT, "w")
f.close()

def safe(v):
    try:
        s = str(v)
    except Exception:
        return "<unprintable>"
    s = s.replace("\x00", "")
    o = ""
    for ch in s:
        o += ch if ord(ch) < 128 else "?"
    return o

def log(s):
    f = open(OUT, "a")
    f.write(safe(s) + "\n")
    f.close()

log("=== OHMIC BATCH START ===")
try:
    oDesktop.RestoreWindow()
    oProject = oDesktop.GetActiveProject()
    # AEDT 内部（非 gRPC）：GetActiveDesign 可靠；仍加一层兜底
    oDesign = None
    try:
        oDesign = oProject.GetActiveDesign()
    except Exception:
        oDesign = None
    if oDesign is None:
        kids = list(oProject.GetChildNames())
        if kids:
            oDesign = oProject.GetChildObject(kids[0])
    if oDesign is None:
        raise RuntimeError("拿不到设计对象")
    log("design: " + safe(oDesign.GetName()))
    oFR = oDesign.GetModule("FieldsReporter")

    ok = 0
    fail = 0
    for obj in OBJS:
        varname = PREFIX + obj
        try:
            oFR.CalcStack("clear")
            oFR.EnterQty("OhmicLoss")
            oFR.EnterVol(obj)
            oFR.CalcOp("Integrate")
            oFR.AddNamedExpression(varname, "Fields")
            ok += 1
            log("  OK   " + varname)
        except Exception as e:
            fail += 1
            log("  FAIL " + safe(obj) + " -> " + safe(e)[:120])

    log("batch ok=%d fail=%d" % (ok, fail))
except Exception as e:
    log("!! ERR: " + safe(type(e).__name__) + " : " + safe(e))
log("=== OHMIC BATCH DONE ===")
'''

OHMIC_OUT = os.path.join(tempfile.gettempdir(), "aedt_gui_ohmic_out.txt")
OHMIC_PY = os.path.join(tempfile.gettempdir(), "aedt_gui_ohmic.py")


# --------------------------------------------------- 剖面电流有效值（irms）
# 场计算器序列（用户 2026-09-10 手验）：
#   EnterQty("J") -> CalcOp(ScalarX/Y/Z) -> CalcOp("CmplxMag")
#   -> EnterSurf(<片>) -> CalcOp("Integrate")
# 得到 Integrate(Surface(<片>), CmplxMag(Scalar?(<Jx,Jy,Jz>))) = 电流【峰值】
# 有效值 = 峰值 / sqrt(2)。
# 取值走 CalculatorWrite 写 .fld 再读最后一行 —— 等价于计算器的 Eval 按钮，
# 不建 Maxwell 报表、不建命名表达式、不动几何。
# ⚠ 整个流程都在 AEDT 内部 IronPython 里跑：gRPC 下
#   DoesNamedExpressionExists / EnterSurf 之类不稳（坑 #36）。
IRMS_TEMPLATE = r"""# -*- coding: utf-8 -*-
import os

OUT = r"__OUT__"
FLD = r"__FLD__"
SETUP = "__SETUP__"
VARIATION = __VARIATION__
ITEMS = __ITEMS__
FACTOR = 0.7071067811865476

f = open(OUT, "w")
f.close()


def safe(v):
    try:
        s = str(v)
    except Exception:
        return "<unprintable>"
    s = s.replace("\x00", "")
    o = ""
    for ch in s:
        o += ch if ord(ch) < 128 else "?"
    return o


def log(s):
    f = open(OUT, "a")
    f.write(safe(s) + "\n")
    f.close()


def read_last_float(path):
    if not os.path.isfile(path):
        return None
    try:
        lines = [x.strip() for x in open(path, "r") if x.strip()]
    except Exception:
        return None
    for ln in reversed(lines):
        try:
            return float(ln)
        except Exception:
            pass
    return None


def clc_write(oFR, path):
    # 旧版简单写法 + 2026 R1 的 NAME:Write 写法，两种都试
    try:
        oFR.CalculatorWrite(path, ["Solution:=", SETUP], VARIATION)
        return True
    except Exception as e1:
        try:
            sol = ["NAME:Setup", "Solution:=", SETUP]
            exp = ["NAME:Expression", "NameOfExpression:=", []]
            oFR.CalculatorWrite(path, ["NAME:Write", sol, exp], VARIATION)
            return True
        except Exception as e2:
            raise RuntimeError("CalculatorWrite fail: %s | %s"
                               % (safe(e1)[:90], safe(e2)[:90]))


log("=== IRMS BATCH START ===")
log("setup     : " + SETUP)
log("variation : " + safe(VARIATION))
try:
    oDesktop.RestoreWindow()
    oProject = oDesktop.GetActiveProject()
    oDesign = None
    try:
        oDesign = oProject.GetActiveDesign()
    except Exception:
        oDesign = None
    if oDesign is None:
        kids = list(oProject.GetChildNames())
        if kids:
            oDesign = oProject.GetChildObject(kids[0])
    if oDesign is None:
        raise RuntimeError("can not get design")
    log("design: " + safe(oDesign.GetName()))
    oFR = oDesign.GetModule("FieldsReporter")

    for label, sheet, scalar in ITEMS:
        try:
            oFR.CalcStack("clear")
            oFR.EnterQty("J")
            oFR.CalcOp(scalar)
            oFR.CalcOp("CmplxMag")
            oFR.EnterSurf(sheet)
            oFR.CalcOp("Integrate")
            try:
                if os.path.isfile(FLD):
                    os.remove(FLD)
            except Exception:
                pass
            clc_write(oFR, FLD)
            peak = read_last_float(FLD)
            if peak is None:
                log("  FAIL %s: no numeric value in .fld" % label)
                try:
                    if os.path.isfile(FLD):
                        for ln in open(FLD, "r"):
                            log("    fld| " + safe(ln)[:180])
                except Exception:
                    pass
                continue
            rms = abs(peak) * FACTOR
            log("  OK   %s  peak=%.10g  rms=%.10g A" % (label, peak, rms))
            log("  DATA %s|%.12g|%.12g" % (label, peak, rms))
        except Exception as e:
            log("  FAIL %s -> %s" % (label, safe(e)[:200]))
    try:
        oFR.CalcStack("clear")
    except Exception:
        pass
    log("=== IRMS BATCH DONE ===")
except Exception as e:
    log("!! ERR: " + safe(type(e).__name__) + " : " + safe(e))
"""

IRMS_OUT = os.path.join(tempfile.gettempdir(), "aedt_gui_irms_out.txt")
IRMS_PY = os.path.join(tempfile.gettempdir(), "aedt_gui_irms.py")
IRMS_FLD = os.path.join(tempfile.gettempdir(), "aedt_gui_irms.fld")
IRMS_RMS_FACTOR = 0.7071067811865476          # 1/sqrt(2)


def _results_info(proj, dname):
    rd = os.path.join(str(proj.GetPath()), str(proj.GetName()) + ".aedtresults",
                      dname + ".results")
    nf, mb = 0, 0.0
    if os.path.isdir(rd):
        for root, _dirs, files in os.walk(rd):
            for fn in files:
                try:
                    nf += 1
                    mb += os.path.getsize(os.path.join(root, fn))
                except Exception:
                    pass
    return dict(dir=rd, files=nf, mb=round(mb / 1e6, 1))


def _temps_from_file(proj_path, proj_name, dname):
    # 从工程文件解析指定设计的温度表，返回 {实体名: '50cel'}（只含表里有的）。
    # 依据（坑 #50，2026-09-04）：SetObjectTemperature 只有 Set 没有 Get，
    # 温度表持久化在 .aedt 文本里，结构为（父链 AnsoftProject -> Maxwell3DModel）：
    #   $begin 'Maxwell3DModel'        块头附近 Name='<设计名>'
    #       $begin 'TemperatureSettings'
    #           Temperatures( <PartID>,'<temp>', ... )
    #   GeometryPart 块内：Attributes.Name='<实体名>' + ParentPartID=<PartID>
    # 注意：以最近一次保存为准，未保存的赋值读不到。
    path = os.path.join(proj_path, proj_name + ".aedt")
    if not os.path.isfile(path):
        print("   [提示] 工程文件不存在: %s" % path)
        return {}
    try:
        f = open(path, encoding="utf-8", errors="replace")
        s = f.read()
        f.close()
    except Exception as e:
        print("   [提示] 工程文件读取失败: %s" % str(e)[:90])
        return {}
    tgt = None
    for m in re.finditer(re.escape("$begin 'Maxwell3DModel'"), s):
        head = s[m.start():m.start() + 2500]
        if re.search(r"Name='" + re.escape(dname) + r"'", head):
            tgt = m.start()
            break
    if tgt is None:
        return {}
    end = s.find("$end 'Maxwell3DModel'", tgt)
    blk = s[tgt:end if end > tgt else tgt + 20000000]

    temps_by_id = {}
    mt = re.search(r"\$begin 'TemperatureSettings'(.*?)"
                   r"\$end 'TemperatureSettings'", blk, re.S)
    if mt:
        me = re.search(r"Temperatures\((.*?)\)", mt.group(1), re.S)
        if me:
            ent = [x.strip() for x in me.group(1).split(",")]
            for i in range(0, len(ent) - 1, 2):
                pid = ent[i].strip()
                val = ent[i + 1].strip().strip("'")
                if pid.isdigit():
                    temps_by_id[pid] = val

    name_by_id = {}
    for m in re.finditer(r"\$begin 'GeometryPart'", blk):
        e = blk.find("$end 'GeometryPart'", m.start())
        seg = blk[m.start():e if e > m.start() else m.start() + 200000]
        nm = re.search(r"Name='([^']+)'", seg)
        pid = re.search(r"ParentPartID=(\d+)", seg)
        if nm and pid:
            name_by_id[pid.group(1)] = nm.group(1)

    out = {}
    for pid, val in temps_by_id.items():
        nm = name_by_id.get(pid)
        if nm:
            out[nm] = val
    return out


def _design_block(src, dname):
    """定位 .aedt 文本中指定设计的设计块片段（gRPC 缺方法时的离线数据源）。"""
    start = -1
    for m in re.finditer(r"\$begin '(Maxwell3DModel|Maxwell2DModel)'", src):
        if "Name='%s'" % dname in src[m.start(): m.start() + 3000]:
            start = m.start()
            break
    if start < 0:
        return ""
    nxt = re.search(r"\$begin '(?:Maxwell3DModel|Maxwell2DModel)'",
                    src[start + 10:])
    return src[start: start + 10 + nxt.start()] if nxt else src[start:]


def _setups_from_file(aedt_path, dname):
    """从 .aedt 文本解析设计块的 setup 名列表（如 ['Setup1']）。"""
    try:
        with open(aedt_path, encoding="utf-8", errors="replace") as f:
            src = f.read()
    except Exception:
        return []
    seg = _design_block(src, dname)
    if not seg:
        return []
    names = []
    for m in re.finditer(r"\$begin '(Setup\d+)'", seg):
        if m.group(1) not in names:
            names.append(m.group(1))
    return names


def _resolve_setup(m3d, des, proj, want):
    """稳健解析解上下文 "<setup> : LastAdaptive"。

    顺序：显式指定 > nominal_adaptive > COM GetSetups > 工程文件解析。
    gRPC 下 oanalysis.GetSetups 可能失败（nominal_adaptive 随之返回空串），
    任何一级成功即返回；全部失败返回 ""（调用方须明确报错，禁止拿空串撞 API）。
    """
    if want:
        return "%s : LastAdaptive" % want
    try:
        s = str(m3d.nominal_adaptive)
        if s and s != "None":
            return s
    except Exception:
        pass
    try:
        sn = [str(x) for x in
              des.GetModule("AnalysisSetup").GetSetups()]
        if sn:
            return "%s : LastAdaptive" % sn[0]
    except Exception:
        pass
    try:
        aedt = os.path.join(str(proj.GetPath()),
                            str(proj.GetName()) + ".aedt")
        names = _setups_from_file(aedt, str(des.GetName()))
        if names:
            return "%s : LastAdaptive" % names[0]
    except Exception:
        pass
    return ""


def _expand_sweep_data(data):
    """展开参数扫描定义：'LIN 6mm 9mm 1mm' / 'LINC 6mm 9mm 4' / 离散列表。

    LIN  = 起 止 步长；LINC = 起 止 点数；其它（离散列表）按空白/逗号/分号切分。
    最多展开 500 个点，防止极小步长炸内存。
    """
    sdata = (data or "").strip()
    if not sdata:
        return []
    toks = sdata.replace(",", " ").replace(";", " ").split()
    if not toks:
        return []

    def _split_unit(t):
        m = re.match(r"^([0-9.eE+-]+)\s*([A-Za-z%]*)$", t)
        if not m:
            return None, ""
        return float(m.group(1)), m.group(2)

    kind = toks[0].upper()
    if kind in ("LIN", "LINC") and len(toks) >= 4:
        v0, u0 = _split_unit(toks[1])
        v1, _u1 = _split_unit(toks[2])
        v2, _u2 = _split_unit(toks[3])
        if v0 is None or v1 is None or v2 is None:
            return []
        if kind == "LIN":
            if not v2:
                return []
            n = int(round((v1 - v0) / v2)) + 1
            n = max(1, min(n, 500))
            step = v2
        else:
            n = max(1, min(int(round(v2)), 500))
            step = (v1 - v0) / (n - 1) if n > 1 else 0.0
        out = []
        for k in range(n):
            out.append(("%.10g" % (v0 + step * k)) + (u0 or ""))
        return out
    return [t for t in toks if t]


def _varcols_and_un(aedt_path, dname):
    """解析设计块的求解取值列 + Optimetrics 参数扫描定义。

    返回 (cols, unsolved, hist, solved_map)：
    - cols {变量: [取值]}：Freq、已求解的多取值列；
      以及参数扫描定义里的变量（未求解时按定义展开的值）；
    - unsolved：扫描定义里但**完全没有已求解值**的变量名；
    - hist：仅存在于已求解多取值列（以前扫描留下的解）、当前未定义扫描的变量名；
    - solved_map {参数: [已求解取值]}：供 GUI 对未求解取值逐个标注。

    ⚠ Optimetrics 定义只在**保存后**才写入工程文件的
    `Optimetrics > OptimetricsSetups > <ParametricSetupN>` 块；
    未保存时该块为空 -> 扫描参数不会出现在 cols 里。
    """
    try:
        with open(aedt_path, encoding="utf-8", errors="replace") as f:
            src = f.read()
    except Exception:
        return {}, []
    seg = _design_block(src, dname)
    if not seg:
        return {}, []

    # 1) 设计块的求解取值列（已求解 variation 列）
    solved = {}
    for m in re.finditer(
            r"\$begin 'Sweep'\s*\n\s*Variable='([^']+)'"
            r"\s*\n\s*Column='([^']*)'"
            r"(?:\s*\n\s*Units='([^']*)')?", seg):
        name = m.group(1)
        if name == "Pass":
            continue
        unit = (m.group(3) or "") if m.lastindex and m.lastindex >= 3 else ""
        vals = [v for v in m.group(2).split(";") if v]
        if name.lower().startswith("freq"):
            # 频率列单位可能各异（kHz/GHz），且值可能自带单位
            # （Column='0.00065GHz' + Units='GHz'）-> 统一换算到 Hz 再数值去重
            norm = []
            for v in vals:
                fv, uv = None, unit
                m2 = re.match(r"^([0-9.eE+-]+)\s*([A-Za-z%]*)$", str(v))
                if m2:
                    try:
                        fv = float(m2.group(1))
                    except Exception:
                        fv = None
                    if m2.group(2):
                        uv = m2.group(2)
                norm.append(v or "-" if fv is None
                            else _fmt_pt("Freq", _to_hz(fv, uv)))
            vals = norm
        cur = solved.setdefault(name, [])
        for v in vals:
            if v not in cur:
                cur.append(v)

    # 2) Optimetrics 参数扫描定义（含尚未求解的）
    defined = {}
    i = seg.find("$begin 'OptimetricsSetups'")
    if i >= 0:
        j = seg.find("$end 'OptimetricsSetups'", i)
        oseg = seg[i:j if j > i else len(seg)]
        for pm in re.finditer(r"\$begin '(ParametricSetup[^']*)'"
                              r"(.*?)\$end '\1'", oseg, re.S):
            body = pm.group(2)
            if "IsEnabled=true" not in body:
                continue
            for sm in re.finditer(r"\$begin 'SweepDefinition'(.*?)"
                                  r"\$end 'SweepDefinition'", body, re.S):
                blk = sm.group(1)
                vm = re.search(r"Variable='([^']+)'", blk)
                dm = re.search(r"Data='([^']*)'", blk)
                if not vm:
                    continue
                vals = _expand_sweep_data(dm.group(1) if dm else "")
                if vals:
                    cur = defined.setdefault(vm.group(1), [])
                    for v in vals:
                        if v not in cur:
                            cur.append(v)

    # 参数与取值以「当前启用的扫描定义」为准；历史多取值列不计入参数
    hist = ([k for k in solved
             if k != "Freq" and k not in defined and len(solved[k]) > 1]
            if defined else [])
    if defined:
        cols = {}
        if solved.get("Freq"):
            cols["Freq"] = list(solved["Freq"])
        unsolved = []
        for k, v in defined.items():
            cols[k] = list(v)
            # 定义值与已求解值**完全没有交集**才算"整组未求解"；
            # 有交集时由 GUI 按 varsolved 逐值标注
            if not (set(v) & set(solved.get(k) or [])):
                unsolved.append(k)
    else:                       # 没有扫描定义 -> 旧的兜底规则
        cols = {k: list(v) for k, v in solved.items()
                if k == "Freq" or len(v) > 1}
        unsolved = []
    solved_map = {k: list(solved.get(k) or []) for k in cols if k != "Freq"}
    return cols, unsolved, hist, solved_map


def _varcols_from_file(aedt_path, dname):
    """兼容包装：只要取值列（旧调用方/冒烟用）。"""
    return _varcols_and_un(aedt_path, dname)[0]


def _resolve_setup(m3d, des, proj, want):
    """稳健解析解上下文 "<setup> : LastAdaptive"。

    顺序：显式指定 > nominal_adaptive > COM GetSetups > 工程文件解析。
    gRPC 下 oanalysis.GetSetups 可能失败（nominal_adaptive 随之返回空串），
    任何一级成功即返回；全部失败返回 ""（调用方须明确报错，禁止拿空串撞 API）。
    """
    if want:
        return "%s : LastAdaptive" % want
    try:
        s = str(m3d.nominal_adaptive)
        if s and s != "None":
            return s
    except Exception:
        pass
    try:
        sn = [str(x) for x in
              des.GetModule("AnalysisSetup").GetSetups()]
        if sn:
            return "%s : LastAdaptive" % sn[0]
    except Exception:
        pass
    try:
        aedt = os.path.join(str(proj.GetPath()),
                            str(proj.GetName()) + ".aedt")
        names = _setups_from_file(aedt, str(des.GetName()))
        if names:
            return "%s : LastAdaptive" % names[0]
    except Exception:
        pass
    return ""


def _varcols_from_file(aedt_path, dname):
    """从 .aedt 文本解析设计变量的求解取值列（Sweeps 段）。

    gRPC 下 GetSweeps / OptimetricsSetup 不可用，且 LastAdaptive 的
    variation 字符串不含 Freq 项 -> 参数名与频率值只能从工程文件读。
    返回 {"Freq": ["650kHz", ...], "<被扫描变量>": [v1, v2, ...]}；
    Pass 列与单取值变量（非 Freq）剔除。
    """
    try:
        with open(aedt_path, encoding="utf-8", errors="replace") as f:
            src = f.read()
    except Exception:
        return {}
    start = -1
    for m in re.finditer(r"\$begin '(Maxwell3DModel|Maxwell2DModel)'", src):
        if "Name='%s'" % dname in src[m.start(): m.start() + 3000]:
            start = m.start()
            break
    if start < 0:
        return {}
    nxt = re.search(r"\$begin '(?:Maxwell3DModel|Maxwell2DModel)'",
                    src[start + 10:])
    seg = src[start: start + 10 + nxt.start()] if nxt else src[start:]
    out = {}
    for m in re.finditer(
            r"\$begin 'Sweep'\s*\n\s*Variable='([^']+)'"
            r"\s*\n\s*Column='([^']*)'", seg):
        name = m.group(1)
        if name == "Pass":
            continue
        vals = [v for v in m.group(2).split(";") if v]
        if name != "Freq" and len(vals) < 2:
            continue
        cur = out.setdefault(name, [])
        for v in vals:
            if v not in cur:
                cur.append(v)
    return out



def cmd_scan(a):
    d = connect(port=a.port)
    try:
        proj = d.odesktop.GetActiveProject()
        if proj is None:
            print("!! 没有打开的工程")
            return 2
        pname = str(proj.GetName())
        des = get_design(proj)
        dname = str(des.GetName())
        ed = des.SetActiveEditor("3D Modeler")

        print("工程 : %s" % pname)
        print("设计 : %s  (%s)" % (dname, str(des.GetDesignType())))

        om = obj_map(ed)
        gcount = {}
        for _n, g in om.items():
            gcount[g] = gcount.get(g, 0) + 1

        try:
            cs = str(ed.GetActiveCoordinateSystem())
        except Exception:
            cs = "?"
        try:
            ncs = len(ed.GetCoordinateSystems())
        except Exception:
            ncs = -1
        print("激活坐标系 : %s （共 %d 个）—— 脚本不切换" % (cs, ncs))

        try:
            copper = sorted(str(x) for x in ed.GetObjectsByMaterial("copper"))
        except Exception as e:
            print("   [警告] GetObjectsByMaterial(copper) 失败: %s" % str(e)[:90])
            copper = []

        vols = {}
        for n in copper:
            try:
                vols[n] = float(ed.GetObjectVolume(n))
            except Exception:
                pass
        if copper:
            print("体积        : %d/%d 个 copper 实体取到（GetObjectVolume，模型单位³）"
                  % (len(vols), len(copper)))

        centers = {}
        for n in copper:
            try:
                bb = [float(x) for x in ed.GetObjectBoundingBox(n)]
                centers[n] = [(bb[0] + bb[3]) / 2.0, (bb[1] + bb[4]) / 2.0,
                              (bb[2] + bb[5]) / 2.0]
            except Exception:
                pass
        print("中心坐标    : %d/%d 个 copper 实体取到（bbox 中心 x/y/z）"
              % (len(centers), len(copper)))

        sheets = sorted(n for n, g in om.items() if g == "Sheets")
        nonmodel = sorted(n for n, g in om.items() if g == "Non Model")
        sections = [n for n in sheets + nonmodel if "Section" in n]
        old = [n for n in om if n.startswith("OLD_")]

        oFR = des.GetModule("FieldsReporter")
        ex = exprs(oFR)
        n_ohm = sum(1 for e in ex if e.split("=")[0].strip().startswith("OhmicLoss_"))
        n_isec = sum(1 for e in ex if e.split("=")[0].strip().startswith("I_sec_"))

        varinfo = {}
        try:
            oAS = des.GetModule("AnalysisSetup")
            setups = [str(x) for x in oAS.GetSetups()]
            # 求解点枚举：setup : sweep -> 已求解 variation 列表
            oSol = des.GetModule("Solutions")
            for _sname in setups:
                _sw_list = ["LastAdaptive"]
                try:
                    _sw_list += [str(x) for x in oAS.GetSweeps(_sname)]
                except Exception:
                    pass
                for _sw in _sw_list:
                    _key = "%s : %s" % (_sname, _sw)
                    try:
                        _vs = [re.sub(r"\s+", " ", str(x)).strip()
                               for x in oSol.GetAvailableVariations(_key)]
                    except Exception:
                        _vs = []
                    varinfo[_key] = [v for v in _vs if v]
        except Exception:
            setups = []
            varinfo = {}

        # 变量取值列（参数名/频率值）：gRPC 拿不到，只能从工程文件解析；
        # 同时拿「已定义但未求解」的参数扫描变量（varunsolved）
        varcols, varunsolved, varhist, varsolved = {}, [], [], {}
        try:
            _aedt = os.path.join(str(proj.GetPath()), pname + ".aedt")
            varcols, varunsolved, varhist, varsolved = \
                _varcols_and_un(_aedt, dname)
            print("参数(扫描定义): %s"
                  % (", ".join(k for k in varcols if k != "Freq") or "无"))
            if varhist:
                print("历史多取值列（以前扫描留下的解，不作为参数显示）: %s"
                      % ", ".join(varhist))
            if varunsolved:
                print("已定义未求解的参数: %s" % ", ".join(varunsolved))
        except Exception as e:
            print("[提示] 变量取值列解析失败: %s" % str(e)[:90])

        print("物体分组 : %s" % ", ".join("%s=%d" % (k, v)
                                          for k, v in sorted(gcount.items())))
        print("copper 实体 : %d" % len(copper))
        print("Sheets      : %d（其中名称含 Section 的剖面: %d）"
              % (len(sheets), len(sections)))
        print("Non Model   : %d | OLD_ 前缀: %d" % (len(nonmodel), len(old)))
        print("命名表达式  : 共 %d（OhmicLoss_* %d, I_sec_* %d）"
              % (len(ex), n_ohm, n_isec))

        try:
            fplots = [str(x) for x in oFR.GetFieldPlotNames()]
        except Exception:
            fplots = []
        print("场图        : %d 个" % len(fplots))
        reports = []
        try:
            reports = [str(x) for x in
                       des.GetModule("ReportSetup").GetAllReportNames()]
        except Exception:
            reports = []
        print("报表        : %d 个" % len(reports))
        for n in reports[:20]:
            print("    %s" % n)
        if len(reports) > 20:
            print("    … 其余 %d 个见界面" % (len(reports) - 20))
        for n in fplots[:20]:
            print("    %s" % n)
        if len(fplots) > 20:
            print("    … 其余 %d 个见界面" % (len(fplots) - 20))
        for e in ex[:20]:
            print("    %s" % e[:120])
        if len(ex) > 20:
            print("    … 其余 %d 条见界面" % (len(ex) - 20))

        temps = _temps_from_file(str(proj.GetPath()), pname, dname)
        if temps:
            print("温度表      : %d 条（工程文件温度表，未保存的赋值不含）"
                  % len(temps))
            for k, v in sorted(temps.items())[:10]:
                print("    %-30s %s" % (k, v))
            if len(temps) > 10:
                print("    … 其余 %d 条见温度赋值页"
                      % (len(temps) - 10))
        else:
            print("温度表      : 无（从未赋温或未保存）")

        ri = _results_info(proj, dname)
        print("结果目录    : %s" % ri["dir"])
        print("结果文件    : %d 个，%.1f MB" % (ri["files"], ri["mb"]))

        data = dict(project=pname, design=dname, cs=cs, ncs=ncs,
                    dtype=str(des.GetDesignType()), groups=gcount,
                    copper=copper, volumes=vols, centers=centers,
                    sheets=sheets, nonmodel=nonmodel,
                    sections=sections, old=old,
                    expr_total=len(ex), n_ohmic=n_ohm, n_isec=n_isec,
                    fplots=fplots,
                    reports=reports,
                    expressions=ex, temps=temps,
                    results=ri, setups=setups, varinfo=varinfo,
                    varcols=varcols,
                    varunsolved=varunsolved,
                    varhist=varhist,
                    varsolved=varsolved)
        print("@@JSON@@" + json.dumps(data, ensure_ascii=False))
        return 0
    finally:
        disconnect(d)


def cmd_ohmic(a):
    objs = [x.strip() for x in a.objects.split(",") if x.strip()]
    if not objs:
        print("!! 没有指定物体")
        return 2

    d = connect(port=a.port)
    try:
        proj = d.odesktop.GetActiveProject()
        if proj is None:
            print("!! 没有打开的工程")
            return 2
        pname = str(proj.GetName())
        des = get_design(proj)
        dname = str(des.GetName())
        ed = des.SetActiveEditor("3D Modeler")
        print("工程 : %s" % pname)
        print("设计 : %s" % dname)

        om = obj_map(ed)
        miss = [o for o in objs if o not in om]
        if miss:
            print("!! 物体不存在: %s" % ", ".join(miss))
            print("!! 未做任何修改")
            return 2

        # 材料自检（只警告，不拦）
        try:
            cu = set(str(x) for x in ed.GetObjectsByMaterial("copper"))
            for o in objs:
                if o not in cu:
                    print("   [提示] %s 的材料不是 copper" % o)
        except Exception:
            pass

        oFR = des.GetModule("FieldsReporter")
        have = set(e.split("=")[0].strip() for e in exprs(oFR))
        pending = [o for o in objs if (a.prefix + o) not in have]
        print("目标 %d 个，已存在 %d 个，待创建 %d 个"
              % (len(objs), len(objs) - len(pending), len(pending)))
        if not pending:
            print("全部已存在，无需重复创建")
            return 0

        body = (OHMIC_TEMPLATE
                .replace("__OUT__", OHMIC_OUT)
                .replace("__OBJS__", repr(pending))
                .replace("__PREFIX__", a.prefix))
        if os.path.isfile(OHMIC_OUT):
            try:
                os.remove(OHMIC_OUT)
            except Exception:
                pass
        with open(OHMIC_PY, "w") as fh:
            fh.write(body)

        total_ok = total_fail = 0
        n = len(pending)
        for bi in range(0, n, a.batch):
            chunk = pending[bi:bi + a.batch]
            body = (OHMIC_TEMPLATE
                    .replace("__OUT__", OHMIC_OUT)
                    .replace("__OBJS__", repr(chunk))
                    .replace("__PREFIX__", a.prefix))
            with open(OHMIC_PY, "w") as fh:
                fh.write(body)
            d.odesktop.RunScript(OHMIC_PY)

            txt = ""
            if os.path.isfile(OHMIC_OUT):
                with open(OHMIC_OUT, "r") as fh:
                    txt = fh.read()
            ok = txt.count("  OK   ")
            fl = txt.count("  FAIL ")
            total_ok += ok
            total_fail += fl
            print("  批次 [%d/%d] ok=%d fail=%d  累计 ok=%d"
                  % (min(bi + a.batch, n), n, ok, fl, total_ok))
            for line in txt.splitlines():
                if "FAIL" in line or "ERR" in line:
                    print("     " + line.strip()[:150])
            sys.stdout.flush()

        print("")
        print("本轮合计: 成功 %d / 失败 %d" % (total_ok, total_fail))

        # 复核
        oFR = des.GetModule("FieldsReporter")
        now = set(e.split("=")[0].strip() for e in exprs(oFR))
        got = [o for o in objs if (a.prefix + o) in now]
        print("复核: 目标 %d 个中已有变量 %d 个" % (len(objs), len(got)))

        if not a.no_save and total_ok:
            proj.Save()
            print("工程已保存")
        else:
            print("（未保存）")

        # 结果目录护栏（保存后）
        ri = _results_info(proj, dname)
        print("结果目录: %d 个文件，%.1f MB" % (ri["files"], ri["mb"]))
        if ri["files"] == 0:
            print("!! 警告：结果目录为空！请立即检查")
            return 3
        return 0 if total_fail == 0 else 1
    finally:
        disconnect(d)


def cmd_dropvars(a):
    """删除场计算器命名表达式（只删表达式，不动几何、不动网格）。"""
    names = [x.strip() for x in a.names.split(",") if x.strip()]
    if not names:
        print("!! 没有指定变量名")
        return 2

    d = connect(port=a.port)
    try:
        proj = d.odesktop.GetActiveProject()
        if proj is None:
            print("!! 没有打开的工程")
            return 2
        des = get_design(proj)
        dname = str(des.GetName())
        print("工程 : %s" % str(proj.GetName()))
        print("设计 : %s" % dname)

        oFR = des.GetModule("FieldsReporter")
        have = {}
        for e in exprs(oFR):
            have[e.split("=")[0].strip()] = e

        snap_before, rd = snap_results(proj, dname)
        print("结果快照  : %s" % ("%d 个文件" % len(snap_before)
                                  if snap_before else "目录不存在: %s" % rd))

        ok, fail, skip = [], [], []
        for n in names:
            if n not in have:
                skip.append(n)
                print("  [跳过] %-24s 不存在" % n)
                continue
            good, msg = drop_var(des, oFR, n)
            (ok if good else fail).append(n)
            print("  [%s] %-24s %s"
                  % ("删除" if good else "失败", n, "OK" if good else msg))
            sys.stdout.flush()

        print("")
        print("删除 %d / 失败 %d / 跳过 %d" % (len(ok), len(fail), len(skip)))

        if ok and not a.no_save:
            mid, _ = snap_results(proj, dname)
            safe, msgs = diff_results(snap_before, mid)
            if not safe:
                print("!! 保存前结果目录就有变化，拒绝保存")
                for m in msgs:
                    print("   %s" % m)
                return 3
            proj.Save()
            print("工程已保存")

        # 复核
        now = set(e.split("=")[0].strip() for e in exprs(oFR))
        left = [n for n in names if n in now]
        print("复核      : 目标 %d 个中仍存在 %d 个" % (len(names), len(left)))

        after, _ = snap_results(proj, dname)
        safe, msgs = diff_results(snap_before, after)
        print("结果目录  : %s" % ("完好" if safe else "!! 异常 !!"))
        for m in msgs:
            print("   %s" % m)
        if not safe:
            return 3
        return 0 if not fail else 1
    finally:
        disconnect(d)


def cmd_delsheets(a):
    """删除剖面/片体（仅 Sheets / Non Model 组；不碰 Solids、不碰网格）。"""
    names = [x.strip() for x in a.objects.split(",") if x.strip()]
    if not names:
        print("!! 没有指定片体名")
        return 2

    d = connect(port=a.port)
    try:
        proj = d.odesktop.GetActiveProject()
        if proj is None:
            print("!! 没有打开的工程")
            return 2
        des = get_design(proj)
        dname = str(des.GetName())
        print("工程 : %s" % str(proj.GetName()))
        print("设计 : %s" % dname)
        ed = des.SetActiveEditor("3D Modeler")
        om = obj_map(ed)
        sheets = set(n for n, g in om.items() if g in ("Sheets", "Non Model"))

        snap_before, rd = snap_results(proj, dname)
        print("结果快照  : %s" % ("%d 个文件" % len(snap_before)
                                  if snap_before else "目录不存在: %s" % rd))

        ok, skip = [], []
        for n in names:
            if n not in om:
                skip.append(n)
                print("  [跳过] %-30s 设计里不存在" % n)
            elif n not in sheets:
                skip.append(n)
                print("  [跳过] %-30s 不是片体（%s 组），拒绝删除"
                      % (n, om[n]))
            else:
                ok.append(n)
                print("  [删除] %-30s （%s）" % (n, om[n]))
            sys.stdout.flush()

        if ok:
            try:
                ed.Delete(["NAME:Selections", "Selections:=", ",".join(ok)])
            except Exception as e:
                print("!! Delete 调用失败: %s" % str(e)[:200])
                return 3

        # 复核
        om2 = obj_map(ed)
        left = [n for n in ok if n in om2]
        print("复核      : 目标 %d 个中仍存在 %d 个" % (len(ok), len(left)))

        if ok and not a.no_save:
            mid, _ = snap_results(proj, dname)
            safe, msgs = diff_results(snap_before, mid)
            if not safe:
                print("!! 保存前结果目录就有变化，拒绝保存")
                for m in msgs:
                    print("   %s" % m)
                return 3
            proj.Save()
            print("工程已保存")

        after, _ = snap_results(proj, dname)
        safe, msgs = diff_results(snap_before, after)
        print("结果目录  : %s" % ("完好" if safe else "!! 异常 !!"))
        for m in msgs:
            print("   %s" % m)
        if not safe:
            return 3
        return 0 if not left else 1
    finally:
        disconnect(d)


def cmd_oldprefix(a):
    """给片体加 OLD_ 前缀（改名不删除；仅 Sheets / Non Model 组）。"""
    names = [x.strip() for x in a.objects.split(",") if x.strip()]
    if not names:
        print("!! 没有指定片体名")
        return 2

    d = connect(port=a.port)
    try:
        proj = d.odesktop.GetActiveProject()
        if proj is None:
            print("!! 没有打开的工程")
            return 2
        des = get_design(proj)
        dname = str(des.GetName())
        print("工程 : %s" % str(proj.GetName()))
        print("设计 : %s" % dname)
        ed = des.SetActiveEditor("3D Modeler")
        om = obj_map(ed)
        sheets = set(n for n, g in om.items() if g in ("Sheets", "Non Model"))

        snap_before, rd = snap_results(proj, dname)
        print("结果快照  : %s" % ("%d 个文件" % len(snap_before)
                                  if snap_before else "目录不存在: %s" % rd))

        todo, skip = [], []
        for n in names:
            if n not in om:
                skip.append(n)
                print("  [跳过] %-30s 设计里不存在" % n)
            elif n not in sheets:
                skip.append(n)
                print("  [跳过] %-30s 不是片体（%s 组）" % (n, om[n]))
            elif n.startswith("OLD_"):
                skip.append(n)
                print("  [跳过] %-30s 已带 OLD_ 前缀" % n)
            else:
                todo.append(n)
        sys.stdout.flush()

        done, fail = [], []
        for n in todo:
            newn = "OLD_" + n
            try:
                ed.ChangeProperty(
                    ["NAME:AllTabs",
                     ["NAME:Geometry3DAttributeTab",
                      ["NAME:PropServers", n],
                      ["NAME:ChangedProps",
                       ["NAME:Name", "Value:=", newn]]]])
                print("  [改名] %s -> %s" % (n, newn))
                done.append(n)
            except Exception as e:
                print("  [失败] %-30s %s" % (n, str(e)[:120]))
                fail.append(n)
            sys.stdout.flush()

        # 复核：新名字应出现在物体表里
        om2 = obj_map(ed)
        left = [n for n in done if ("OLD_" + n) not in om2]
        print("复核      : 改名 %d 个，未确认 %d 个" % (len(done), len(left)))

        if done and not a.no_save:
            mid, _ = snap_results(proj, dname)
            safe, msgs = diff_results(snap_before, mid)
            if not safe:
                print("!! 保存前结果目录就有变化，拒绝保存")
                for m in msgs:
                    print("   %s" % m)
                return 3
            proj.Save()
            print("工程已保存")

        after, _ = snap_results(proj, dname)
        safe, msgs = diff_results(snap_before, after)
        print("结果目录  : %s" % ("完好" if safe else "!! 异常 !!"))
        for m in msgs:
            print("   %s" % m)
        if not safe:
            return 3
        return 0 if not (fail or left) else 1
    finally:
        disconnect(d)


def cmd_delreports(a):
    """删除报表（ReportSetup.DeleteReports；不动几何、网格与解）。"""
    names = [x.strip() for x in a.names.split(",") if x.strip()]
    if not names:
        print("!! 没有指定报表名")
        return 2

    d = connect(port=a.port)
    try:
        proj = d.odesktop.GetActiveProject()
        if proj is None:
            print("!! 没有打开的工程")
            return 2
        des = get_design(proj)
        dname = str(des.GetName())
        print("工程 : %s" % str(proj.GetName()))
        print("设计 : %s" % dname)

        oRS = des.GetModule("ReportSetup")
        try:
            have = [str(x) for x in oRS.GetAllReportNames()]
        except Exception:
            have = []

        snap_before, rd = snap_results(proj, dname)
        print("结果快照  : %s" % ("%d 个文件" % len(snap_before)
                                  if snap_before else "目录不存在: %s" % rd))

        ok, fail, skip = [], [], []
        for n in names:
            if n not in have:
                skip.append(n)
                print("  [跳过] %-28s 不存在" % n)
                continue
            try:
                oRS.DeleteReports([n])
                ok.append(n)
                print("  [删除] %-28s OK" % n)
            except Exception as e:
                fail.append(n)
                print("  [失败] %-28s %s" % (n, str(e)[:110]))
            sys.stdout.flush()

        print("")
        print("删除 %d / 失败 %d / 跳过 %d" % (len(ok), len(fail), len(skip)))

        if ok and not a.no_save:
            mid, _ = snap_results(proj, dname)
            safe, msgs = diff_results(snap_before, mid)
            if not safe:
                print("!! 保存前结果目录就有变化，拒绝保存")
                for m in msgs:
                    print("   %s" % m)
                return 3
            proj.Save()
            print("工程已保存")

        try:
            now = [str(x) for x in oRS.GetAllReportNames()]
            left = [n for n in names if n in now]
        except Exception:
            left = []
        print("复核      : 目标 %d 个中仍存在 %d 个" % (len(names), len(left)))

        after, _ = snap_results(proj, dname)
        safe, msgs = diff_results(snap_before, after)
        print("结果目录  : %s" % ("完好" if safe else "!! 异常 !!"))
        for m in msgs:
            print("   %s" % m)
        if not safe:
            return 3
        return 0 if not fail else 1
    finally:
        disconnect(d)


def cmd_delfplots(a):
    """删除场图（FieldsReporter.DeleteFieldPlot；不动几何、网格与解）。"""
    names = [x.strip() for x in a.names.split(",") if x.strip()]
    if not names:
        print("!! 没有指定场图名")
        return 2

    d = connect(port=a.port)
    try:
        proj = d.odesktop.GetActiveProject()
        if proj is None:
            print("!! 没有打开的工程")
            return 2
        des = get_design(proj)
        dname = str(des.GetName())
        print("工程 : %s" % str(proj.GetName()))
        print("设计 : %s" % dname)

        oFR = des.GetModule("FieldsReporter")
        try:
            have = [str(x) for x in oFR.GetFieldPlotNames()]
        except Exception:
            have = []

        snap_before, rd = snap_results(proj, dname)
        print("结果快照  : %s" % ("%d 个文件" % len(snap_before)
                                  if snap_before else "目录不存在: %s" % rd))

        ok, fail, skip = [], [], []
        for n in names:
            if n not in have:
                skip.append(n)
                print("  [跳过] %-28s 不存在" % n)
                continue
            try:
                oFR.DeleteFieldPlot([n])
                ok.append(n)
                print("  [删除] %-28s OK" % n)
            except Exception as e:
                fail.append(n)
                print("  [失败] %-28s %s" % (n, str(e)[:110]))
            sys.stdout.flush()

        print("")
        print("删除 %d / 失败 %d / 跳过 %d" % (len(ok), len(fail), len(skip)))

        if ok and not a.no_save:
            mid, _ = snap_results(proj, dname)
            safe, msgs = diff_results(snap_before, mid)
            if not safe:
                print("!! 保存前结果目录就有变化，拒绝保存")
                for m in msgs:
                    print("   %s" % m)
                return 3
            proj.Save()
            print("工程已保存")

        try:
            now = [str(x) for x in oFR.GetFieldPlotNames()]
            left = [n for n in names if n in now]
        except Exception:
            left = []
        print("复核      : 目标 %d 个中仍存在 %d 个" % (len(names), len(left)))

        after, _ = snap_results(proj, dname)
        safe, msgs = diff_results(snap_before, after)
        print("结果目录  : %s" % ("完好" if safe else "!! 异常 !!"))
        for m in msgs:
            print("   %s" % m)
        if not safe:
            return 3
        return 0 if not fail else 1
    finally:
        disconnect(d)


def cmd_mats(a):
    d = connect(port=a.port)
    try:
        proj = d.odesktop.GetActiveProject()
        des = get_design(proj)
        ed = des.SetActiveEditor("3D Modeler")
        om = obj_map(ed)
        mats = {}
        for n in sorted(om):
            try:
                m = str(ed.GetPropertyValue("Geometry3DAttributeTab", n,
                                            "Material"))
            except Exception:
                m = "?"
            m = m.split(":")[0].strip() if m.startswith("$") else m
            mats.setdefault(m, []).append(n)
        for m in sorted(mats, key=lambda k: -len(mats[k])):
            print("  %-28s %4d" % (m, len(mats[m])))
        return 0
    finally:
        disconnect(d)


def cmd_jplot(a):
    """对指定 copper 实体建 J 场图（Mag_J / Vector_J），场图名 = 实体名。"""
    objs = [x.strip() for x in a.objects.split(",") if x.strip()]
    if not objs:
        print("!! 没有指定物体")
        return 2
    types = [t.strip() for t in a.types.split(",") if t.strip()]
    types = [t for t in types if t in ("Mag", "Vector")]
    if not types:
        print("!! J 类型无效（只能选 Mag / Vector）")
        return 2

    d = connect(port=a.port)
    try:
        proj = d.odesktop.GetActiveProject()
        if proj is None:
            print("!! 没有打开的工程")
            return 2
        pname = str(proj.GetName())
        des = get_design(proj)
        dname = str(des.GetName())
        ed = des.SetActiveEditor("3D Modeler")
        print("工程 : %s" % pname)
        print("设计 : %s" % dname)

        om = obj_map(ed)
        miss = [o for o in objs if o not in om]
        if miss:
            print("!! 物体不存在: %s" % ", ".join(miss))
            print("!! 未做任何修改")
            return 2

        try:
            cu = set(str(x) for x in ed.GetObjectsByMaterial("copper"))
            for o in objs:
                if o not in cu:
                    print("   [提示] %s 的材料不是 copper" % o)
        except Exception:
            pass

        from ansys.aedt.core import Maxwell3d
        from aedt_env import AEDT_VERSION
        # 与 current_integral_pipeline.pick_target 同款构造（E2E 验证过的写法）：
        # PyAEDT 1.4 的关键字是 new_desktop（不是 new_desktop_session），并要给 version/port
        m3d = Maxwell3d(project=pname, design=dname, version=AEDT_VERSION,
                        port=a.port, new_desktop=False, close_on_exit=False)
        setup = str(m3d.nominal_adaptive)

        # 内禀变量必须含 Freq（AC Magnetic 场数据按 Freq+Phase 存放），
        # 只传 Phase 的场图定位不到解数据 —— 打开永远是空的。
        # Freq 从 setup 默认内禀变量里取，Phase 用用户指定的（默认 0deg）。
        setup_name = setup.split(":")[0].strip()
        intr = {}
        for _so in m3d.setups:
            if str(_so.name) == setup_name:
                intr = dict(_so.default_intrinsics)
                break
        intr["Phase"] = a.phase
        print("内禀变量: %s" % intr)
        print("解上下文: %s" % setup)
        print("J 类型  : %s" % ", ".join(types))
        print("相位    : %s" % a.phase)
        print("-" * 74)

        # 命名规则（AEDT 场图名全局唯一，同名会互相覆盖 —— 坑 #47 实验）：
        #   Mag_J    -> 实体名
        #   Vector_J -> 实体名_V
        qmap = {"Mag": ("Mag_J", ""), "Vector": ("Vector_J", "_V")}

        oFR = des.GetModule("FieldsReporter")

        def plot_names():
            try:
                return [str(x) for x in oFR.GetFieldPlotNames()]
            except Exception:
                return []

        exist = set(plot_names())
        total_ok = total_fail = 0
        for o in objs:
            for t in types:
                q, suffix = qmap[t]
                target = o + suffix
                try:
                    if target in exist:
                        m3d.post.delete_field_plot(target)
                        print("   [删旧] %s" % target)
                    if a.surface_only:
                        # 纯表面场图：实体名自动展开全部外表面面片
                        #（FacesList -> PlotDefinition id=128，
                        # PyAEDT 自动 SurfaceOnly=true，与手动建图同路线）
                        r = m3d.post.create_fieldplot_surface(
                            o, q, setup,
                            intrinsics=intr,
                            plot_name=target, field_type=None)
                    else:
                        r = m3d.post.create_fieldplot_volume(
                            [o], q, setup,
                            intrinsics=intr,
                            plot_name=target, field_type=None)
                    if r is False:
                        total_fail += 1
                        print("   [失败] %-30s <- %s" % (target, q))
                    else:
                        total_ok += 1
                        exist.add(target)
                        print("   [OK]   %-30s <- %s%s"
                              % (target, q,
                                 "（纯表面）" if a.surface_only else ""))
                except Exception as e:
                    total_fail += 1
                    print("   [失败] %-30s <- %s : %s" % (target, q, str(e)[:70]))
            sys.stdout.flush()

        # 复核（FieldsReporter 原生枚举，不依赖 PyAEDT 缓存）
        now = set(plot_names())
        made = [o + suf for o in objs for t, (q, suf) in qmap.items() if t in types]
        missing = [m for m in made if m not in now]
        print("-" * 74)
        print("本轮合计: 成功 %d / 失败 %d（复核缺失 %d）"
              % (total_ok, total_fail, len(missing)))
        if missing:
            print("!! 未在列表中找到: %s" % missing)
        print("现有场图 (%d): %s" % (len(now), sorted(now)))

        if not a.no_save and total_ok:
            snap = _results_info(proj, dname)
            proj.Save()
            print("工程已保存")
            after = _results_info(proj, dname)
            if snap and after and snap.get("files") == after.get("files"):
                print("保存后结果目录: 完好（%d 个文件）" % after.get("files", 0))
            else:
                print("!! 保存后结果文件数变化，请检查")
        else:
            print("（未保存）")
        return 0 if total_fail == 0 else 1
    finally:
        disconnect(d)



# 后处理扫描（不作为"求解点"维度；Phase 只是相位步进）
_POST_SWEEPS = ("phase", "normalizeddistance", "theta", "phi", "pass")


_UNIT_SCALE = {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9,
               "thz": 1e12}


def _header_unit(head):
    """从表头提取单位：'Freq [MHz]' / "Freq ['GHz']" / 'Freq(GHz)'。"""
    m = re.search(r"[\[(]\s*['\"]?([A-Za-z%/]+)['\"]?\s*[\])]",
                  str(head))
    return m.group(1) if m else ""


def _to_hz(v, unit):
    """把频率数值换算到 Hz；单位未知/非频率单位时原样返回。"""
    if v is None or v != v:
        return v
    scale = _UNIT_SCALE.get(str(unit or "").strip().lower())
    return v if scale is None else v * scale


def _fmt_pt(name, raw):
    """求解点标签：能转数字 -> 智能单位（Freq）/%.6g；否则原样返回。

    参数扫描的值常带单位（如 '110.3mm'）-> 原样显示。
    """
    if isinstance(raw, (int, float)):
        v = float(raw)
    else:
        s2 = str(raw).strip()
        try:
            v = float(s2)
        except Exception:
            return s2 or "-"
    if (name or "").lower().startswith("freq"):
        a = abs(v)
        if a >= 1e9:
            return "%.6gGHz" % (v / 1e9)
        if a >= 1e6:
            return "%.6gMHz" % (v / 1e6)
        if a >= 1e3:
            return "%.6gkHz" % (v / 1e3)
        return "%.6gHz" % v
    return "%.6g" % v


def _read_lossbar_report(oReport, rname, outdir):
    """把已有报表导出 CSV 并解析，仅从该报表取数（不重算表达式）。

    oReport = des.GetModule("ReportSetup")。导出用
    ExportToFile(plot_name, path, False)（PyAEDT export_report_to_csv 底层）。

    求解点维度 = 导出表里的 **Freq / 参数列**（Phase 等后处理扫描不算），
    按该列去重建 points/series；报表只有后处理扫描时 points=[]（不提供
    选点，按单点出图），避免拿相位值冒充"求解点"。

    返回 (pool, psweep, freq_used, nrows, ncols, points, series)；
    pool/频率沿用旧行为：首列 Freq 取最高频点行、否则取首行。
    """
    path = os.path.join(outdir, "_LossBar_All_export.csv")
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
    oReport.ExportToFile(rname, path, False)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = [ln.rstrip("\r\n") for ln in f if ln.strip()]
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if len(lines) < 2:
        raise RuntimeError("导出内容为空（%d 行）" % len(lines))

    def _split(ln):
        sep = "\t" if "\t" in ln else ","
        return [c.strip().strip('"') for c in ln.split(sep)]

    heads = _split(lines[0])
    rows = [_split(ln) for ln in lines[1:]]
    if not heads or not rows:
        raise RuntimeError("表头/数据解析失败")

    def _f(x):
        try:
            return float(x)
        except Exception:
            return float("nan")

    # ---- 求解点维度：优先 Freq 列，其次其它非后处理列（参数扫描变量）
    dim_idx, dim_name = None, ""
    for j, h in enumerate(heads):
        hh = h.split()[0].strip()
        if hh.lower().startswith("freq"):
            dim_idx, dim_name = j, "Freq"
            break
    if dim_idx is None:
        for j, h in enumerate(heads):
            hh = h.split()[0].strip()
            if not hh or hh.lower() in _POST_SWEEPS:
                continue
            if hh.startswith("OhmicLoss_"):     # 数据列，不是维度
                continue
            dim_idx, dim_name = j, hh           # 其它列 = 参数扫描变量
            break

    # ---- 表达式列
    obj_cols = []
    for j in range(1, len(heads)):
        h = heads[j].split()[0].strip()
        if h.startswith("OhmicLoss_"):
            obj_cols.append((j, h[len("OhmicLoss_"):]))

    # ---- 逐行原始值
    raw_rows = []
    for r in rows:
        raw_rows.append([(r[j] if j < len(r) else "") for j in range(len(heads))])

    # ---- 求解点：按维度列去重（同值取首行）；无维度 -> 空
    # 频率维度先按表头单位换算到 Hz（导出值用的是报表显示单位，
    # 如 X 轴 MHz -> 0.5 表示 500kHz，绝不能再当 Hz）
    dim_is_freq = dim_name.lower().startswith("freq")
    dim_unit = _header_unit(heads[dim_idx]) if dim_idx is not None else ""

    def _dim_label(raw):
        if dim_is_freq:
            v = _f(raw)
            if v == v:
                return _fmt_pt("Freq", _to_hz(v, dim_unit))
        return (str(raw).strip() or "-")

    points, sel_rows = [], []
    if dim_idx is not None:
        seen = set()
        for i, r in enumerate(raw_rows):
            dv = r[dim_idx].strip() if dim_idx < len(r) else ""
            if dv and dv not in seen:
                seen.add(dv)
                sel_rows.append(i)
        points = [_dim_label(raw_rows[i][dim_idx]) for i in sel_rows]

    series = {}
    if dim_idx is not None:          # 无 Freq/参数维度 -> 不提供选点与序列
        for j, name in obj_cols:
            vals = []
            for i in sel_rows:
                v = _f(raw_rows[i][j]) if j < len(raw_rows[i]) \
                    else float("nan")
                vals.append(v if v == v else None)
            series[name] = vals

    # ---- pool / 取值行（旧行为）：首列 Freq 取最高频点、否则取首行
    var = heads[0].split()[0] if heads[0] else ""
    first_col = [_f(raw_rows[i][0]) for i in range(len(raw_rows))]
    if var.lower().startswith("freq"):
        # 首列同样是报表显示单位 -> 换算到 Hz
        _u0 = _header_unit(heads[0])
        first_col = [_to_hz(v, _u0) for v in first_col]
    if var.lower().startswith("freq"):
        psweep = "Freq"
        row_idx = None
        for i, v in enumerate(first_col):
            if v == v and (row_idx is None or v > first_col[row_idx]):
                row_idx = i
        if row_idx is None:
            raise RuntimeError("首列无有效 Freq 数值")
        freq_used = first_col[row_idx]
    else:
        psweep = "Phase" if var.lower().startswith("phase") else (var or "Freq")
        row_idx, freq_used = 0, None

    pool = {}
    for j, name in obj_cols:
        v = _f(raw_rows[row_idx][j]) if j < len(raw_rows[row_idx]) \
            else float("nan")
        if v == v:
            pool[name] = v
    return pool, psweep, freq_used, len(rows), len(obj_cols), points, series


def _find_project(d, want_proj="", want_design=""):
    """按名字找打开的工程；重名时取"含目标设计"的那份；找不到返回 None。"""
    best = None
    for p in d.odesktop.GetProjects():
        if want_proj and str(p.GetName()) != want_proj:
            continue
        if want_design:
            try:
                kids = [str(x) for x in p.GetChildNames()]
            except Exception:
                kids = []
            if want_design in kids:
                return p          # 含目标设计的同名工程优先
            if best is None:
                best = p          # 同名但没见设计的备选
        elif best is None:
            best = p
    return best


def _safe_maxwell3d(d, pname, dname, version, port):
    """挂接 Maxwell3d，杜绝坑 #43 的静默新建。

    挂接前后对工程列表快照：PyAEDT 若新建了工程，立即 CloseProject
    （不保存）回收并抛错；挂接后校验 design_name 与目标一致。
    """
    from ansys.aedt.core import Maxwell3d
    odesk = d.odesktop
    before = set(str(p.GetName()) for p in odesk.GetProjects())
    m3d = Maxwell3d(project=pname, design=dname, version=version,
                    port=port, new_desktop=False, close_on_exit=False)
    created = [str(p.GetName()) for p in odesk.GetProjects()
               if str(p.GetName()) not in before]
    for p in created:
        try:
            odesk.CloseProject(p)   # 新建且未改动 -> 不保存直接关闭
            print("   [回收] PyAEDT 静默新建的空工程 %s 已关闭（未保存）" % p)
        except Exception:
            pass
    if created:
        raise RuntimeError(
            "PyAEDT 挂接时新建了工程 %s —— 工程/设计名不匹配"
            "（同名工程请只保留需要的那份）" % created)
    if str(m3d.design_name) != dname:
        raise RuntimeError("挂接到了设计 '%s'（期望 '%s'）"
                           % (m3d.design_name, dname))
    return m3d


def cmd_barloss_all(a):
    d = connect(port=a.port)
    try:
        proj = _find_project(d, getattr(a, "project", ""),
                             getattr(a, "design", ""))
        if proj is None:
            print("!! 找不到打开的工程 %s（或其中没有设计 %s）"
                  % (getattr(a, "project", "") or "<激活>",
                     getattr(a, "design", "") or "<任意>"))
            return 2
        pname = str(proj.GetName())
        des = get_design(proj, getattr(a, "design", ""))
        dname = str(des.GetName())
        print("工程 : %s" % pname)
        print("设计 : %s" % dname)

        oFR = des.GetModule("FieldsReporter")
        target = [e.split("=")[0].strip() for e in exprs(oFR)
                  if e.split("=")[0].strip().startswith("OhmicLoss_")]
        if not target:
            print("!! 场计算器里没有 OhmicLoss_ 前缀的命名表达式"
                  "（先在 Ohmic Loss 积分 tab 创建）")
            return 2
        print("OhmicLoss_ 表达式 %d 个" % len(target))

        m3d = _safe_maxwell3d(d, pname, dname, "2026.1", a.port)
        # 内容校验：m3d 落点必须含目标表达式（防挂到重名工程的另一份副本）
        try:
            _oFR_chk = m3d.odesign.GetModule("FieldsReporter")
            _have = set(e.split("=")[0].strip() for e in exprs(_oFR_chk))
        except Exception:
            _have = set()
        _miss = [e for e in target
                 if e.split("=")[0].strip() not in _have]
        if target and len(_miss) == len(target):
            raise RuntimeError(
                "挂接到的 %s / %s 不含任何目标 OhmicLoss_* 表达式"
                "——可能存在同名工程副本，请关闭多余的那份后重试"
                % (pname, dname))
        setup = _resolve_setup(m3d, des, proj, getattr(a, "setup", ""))
        if not setup:
            print("!! 无法确定 setup（gRPC nominal_adaptive/GetSetups 不可用，"
                  "且总览页未选择求解点）——请在总览页选择求解点后重试")
            return 2
        print("解上下文: %s" % setup)

        # 坑 #52（Families 防展开）：不传 variations 时 PyAEDT 会把
        # default_intrinsics 中未指定的变量（Phase 等）设成 "All"，
        # Fields 报表按 相位点数 x 表达式数 逐点算体积分，直接卡死 AEDT。
        # 这里把除 primary sweep 外的全部 intrinsics 钉成单值，
        # 设计变量走 Nominal——与手工建 report 的 Families 设置一致。
        try:
            _sname = setup.split(":")[0].strip()
            _di = dict(m3d.design_setups[_sname].default_intrinsics)
        except Exception:
            _di = {}

        def _mk_variations(ps):
            v = {k: [x] for k, x in _di.items()}
            v[ps] = ["All"]
            return v

        variations = _mk_variations("Freq")

        def _f(v):
            try:
                return float(v)
            except Exception:
                return float("nan")

        # ---- 快速路径：LossBar_All 已存在 -> 仅从该报表直读（不再逐表达式取数）
        rname = "LossBar_All"
        try:
            reps = [str(x) for x in m3d.post.all_report_names]
        except Exception:
            reps = []
        pool = None
        psweep = None
        freq_used = None
        if rname in reps:
            print("总报表已存在: %s（复用，未重建；仅从该报表直读数据，"
                  "不再逐表达式取数）" % rname)
            try:
                oReport = des.GetModule("ReportSetup")
                pool, psweep, freq_used, _nr, _nc, points, series = \
                    _read_lossbar_report(oReport, rname,
                                         str(proj.GetPath()))
                print("报表直读 OK：%d 列 OhmicLoss（有效 %d）× %d 个求解点，"
                      "X=%s" % (_nc, len(pool), len(points), psweep))
                if not points:
                    print("   [提示] 报表里没有 Freq/参数维度列（X=%s 属后处理"
                          "扫描）—— 入栈不弹选点，损耗按单点出图" % psweep)
            except Exception as e:
                print("!! 从报表 LossBar_All 导出/解析数据失败: %s"
                      % str(e)[:160])
                print("   回退到完整取数路径（较慢）…")
                pool = None
            if pool is not None and not pool:
                print("!! 报表 LossBar_All 里没有可用的 OhmicLoss_* 数据列"
                      "（报表为空或其表达式已失效；可在 Maxwell Results 里"
                      "删除该报表后重新生成）")
                return 2
        else:
            print("[提示] 未找到总报表 LossBar_All —— "
                  "走完整取数并创建报表（首次较慢）")

        if pool is None:
            # ---- 完整路径：首次生成 / 直读失败回退（原逻辑，逐表达式取数） ----
            try:
                sd = m3d.post.get_solution_data(
                    expressions=target, setup_sweep_name=setup,
                    primary_sweep_variable="Freq", context="None",
                    variations=variations, report_category="Fields")
                try:
                    psv = list(sd.primary_sweep_values) \
                        if sd.primary_sweep_values is not None else []
                except Exception:
                    psv = []
                sweeps = [_f(v) for v in psv]
                # 频率单位换算：PyAEDT 返回的是报表显示单位下的数值
                _u = ""
                try:
                    _u = str((getattr(sd, "units_sweeps", {}) or {})
                             .get("Freq", ""))
                except Exception:
                    _u = ""
                sweeps = [_to_hz(v, _u) for v in sweeps]
            except Exception as e:
                print("[提示] X=Freq 取数异常: %s" % str(e)[:110])
                sweeps = []
            psweep = "Freq"
            if not sweeps:
                print("[提示] X=Freq 无数据（LastAdaptive 单频点通常无扫频场），"
                      "自动回退 X=Phase")
                psweep = "Phase"
                variations = _mk_variations("Phase")
                try:
                    sd = m3d.post.get_solution_data(
                        expressions=target, setup_sweep_name=setup,
                        primary_sweep_variable="Phase", context="None",
                        variations=variations, report_category="Fields")
                    try:
                        psv = list(sd.primary_sweep_values) \
                            if sd.primary_sweep_values is not None else []
                    except Exception:
                        psv = []
                    sweeps = [_f(v) for v in psv]
                except Exception as e:
                    print("!! X=Phase 回退取数异常: %s" % str(e)[:110])
                    return 2
            if not sweeps:
                print("!! 取数失败（primary sweep 为空；设计可能没有已求解数据）")
                return 2
            if psweep == "Freq":
                idx = sweeps.index(max(sweeps))
                freq_used = sweeps[idx]
                print("取频点: Freq = %g（扫频共 %d 点，取最高频）"
                      % (freq_used, len(sweeps)))
            else:
                idx = 0
                freq_used = None
            pool = {}
            series = {}
            nbad = 0
            for e in target:
                got = sd.get_expression_data(e)
                if isinstance(got, (tuple, list)) and len(got) == 2 \
                        and not isinstance(got[0], (int, float)):
                    raw = got[1]
                else:
                    raw = got
                try:
                    seq = [_f(v) for v in
                           (list(raw) if raw is not None else [])]
                except Exception:
                    seq = []
                obj = e[len("OhmicLoss_"):]
                series[obj] = [v if v == v else None for v in seq]
                if not seq or any(v != v for v in seq):
                    print("   [跳过] %s 无有效数据" % e)
                    nbad += 1
                    continue
                if psweep == "Phase":
                    vmax, vmin = max(seq), min(seq)
                    if vmax and (vmax - vmin) > abs(vmax) * 1e-3:
                        print("   [提示] %s 随 Phase 波动 %.2g%%（表达式含相位项？），"
                              "取首点" % (e, (vmax - vmin) / abs(vmax) * 100))
                    pool[obj] = seq[0]
                else:
                    j = min(idx, len(seq) - 1)
                    pool[obj] = seq[j]
            if not pool:
                print("!! 所有表达式均无有效数据")
                return 2
            print("取数 OK：%d 个表达式 × %d 个 %s 点（成功 %d，跳过 %d）"
                  % (len(target), len(sweeps), psweep, len(pool), nbad))

            # 总报表 LossBar_All：存在即复用，绝不重建（省资源）
            rname = "LossBar_All"
            try:
                reps = [str(x) for x in m3d.post.all_report_names]
            except Exception:
                reps = []
            if rname in reps:
                print("总报表已存在: %s（复用，未重建；如需更新报表列请先在 "
                      "Maxwell Results 里删除它再重新生成）" % rname)
            else:
                made = None
                for ptype in ("Data Table", "Rectangular Plot"):
                    try:
                        m3d.post.create_report(
                            expressions=target, setup_sweep_name=setup,
                            primary_sweep_variable=psweep,
                            plot_type=ptype, report_category="Fields",
                            context="None", plot_name=rname,
                            variations=variations)
                        made = ptype
                        break
                    except Exception as e:
                        print("   [报表 %s 失败] %s" % (ptype, str(e)[:110]))
                if made:
                    print("总报表已创建: %s（%s，Maxwell Results 里可查看）"
                          % (rname, made))
                else:
                    print("[提示] 报表创建失败（不影响缓存取数）")
                    rname = ""
            if psweep.lower() in _POST_SWEEPS:
                points, series = [], {}
                print("[提示] 该设计只有 %s 扫描（无 Freq/参数维度）——"
                      "入栈不弹选点；如需按频率/参数对比，请先求解对应扫描。"
                      % psweep)
            else:
                points = [_fmt_pt(psweep, v) for v in sweeps]

        total = sum(pool.values())
        print("")
        print("===== OhmicLoss 总表（解: %s，X=%s）=====" % (setup, psweep))
        keys = sorted(pool)
        for o in keys[:10]:
            print("  %-30s %.6g W" % (o, pool[o]))
        if len(keys) > 10:
            print("  ...（其余 %d 个已进入 GUI 缓存，日志省略）"
                  % (len(keys) - 10))
        print("  %-30s %.6g W  （%d 个求和）" % ("SUM_TOTAL", total,
                                                len(pool)))
        print("@@LOSSBARALL@@" + json.dumps(
            {"report": rname, "sol": setup, "sweep": psweep,
             "freq": freq_used, "project": pname, "design": dname,
             "pool": pool, "total": total,
             "points": points, "series": series}, ensure_ascii=False))

        if not a.no_save:
            proj.Save()
            print("工程已保存")
        else:
            print("（未保存）")
        ri = _results_info(proj, dname)
        print("结果目录: %d 个文件，%.1f MB" % (ri["files"], ri["mb"]))
        if ri["files"] == 0:
            print("!! 警告：结果目录为空！请立即检查")
            return 3
        return 0
    finally:
        disconnect(d)


def cmd_temperature(a):
    # 对指定实体执行 Maxwell 的 Set Object Temperature。
    # 底层 = oDesign.SetObjectTemperature（与 Maxwell 菜单
    # Fields -> Set Object Temperature 同一命令）。
    # 读取：API 只有 Set 没有 Get（坑 #50），温度从工程文件温度表解析，
    # 以最近一次保存为准。
    objs = [x.strip() for x in a.objects.split(",") if x.strip()]
    if not objs:
        print("!! 没有指定物体")
        return 2

    d = connect(port=a.port)
    try:
        proj = d.odesktop.GetActiveProject()
        if proj is None:
            print("!! 没有打开的工程")
            return 2
        pname = str(proj.GetName())
        des = get_design(proj)
        dname = str(des.GetName())
        ed = des.SetActiveEditor("3D Modeler")
        print("工程 : %s" % pname)
        print("设计 : %s" % dname)

        om = obj_map(ed)
        miss = [o for o in objs if o not in om]
        if miss:
            print("!! 物体不存在: %s" % ", ".join(miss))
            print("!! 未做任何修改")
            return 2

        def mat_of(n):
            try:
                m = str(ed.GetPropertyValue("Geometry3DAttributeTab", n,
                                            "Material")).strip().strip('"')
                return m.split(":")[0].strip() if m.startswith("$") else m
            except Exception:
                return "?"

        fpath = os.path.join(str(proj.GetPath()), pname + ".aedt")
        temps_file = _temps_from_file(str(proj.GetPath()), pname, dname)
        print("温度表来源: %s（以最近一次保存为准）" % fpath)

        if a.read_only:
            print("只读模式：仅读取当前温度（不做任何修改）")
            for o in objs:
                print("    %-30s %s"
                      % (o, temps_file.get(o) or "（未设置）"))
            print("@@TEMPS@@" + json.dumps(
                {o: temps_file.get(o) for o in objs}, ensure_ascii=False))
            return 0

        t = (a.temp or "").strip()
        if not t:
            print("!! 没有指定温度（--temp）")
            return 2
        if t[-1].isalpha():
            tval = t                              # 已带单位，如 105cel
        else:
            try:
                float(t)
            except Exception:
                print("!! 温度无效: %r（示例：22 或 105cel）" % a.temp)
                return 2
            tval = t + "cel"

        print("温度    : %s（%d 个实体）" % (tval, len(objs)))
        print("开关    : IncludeTemperatureDependence=%s  EnableFeedback=%s"
              % (bool(a.dep), bool(a.feedback)))
        print("赋值前  :")
        for o in objs:
            print("    %-30s mat=%-10s %s"
                  % (o, mat_of(o), temps_file.get(o) or "（未设置）"))
        print("-" * 74)

        temps = []
        for o in objs:
            temps.extend([o, tval])
        arg = ["NAME:TemperatureSettings",
               "IncludeTemperatureDependence:=", bool(a.dep),
               "EnableFeedback:=", bool(a.feedback),
               "Temperatures:=", temps]
        try:
            des.SetObjectTemperature(arg)
        except Exception as e:
            print("!! SetObjectTemperature 调用失败: %s" % str(e)[:160])
            return 3
        print("SetObjectTemperature 调用完成")

        if a.no_save:
            print("（未保存 —— 文件复核不可用，请以 Maxwell 菜单")
            print("          Fields -> Set Object Temperature 对话框为准）")
            print("@@TEMPS@@" + json.dumps(
                {o: temps_file.get(o) for o in objs}, ensure_ascii=False))
            return 0

        snap = _results_info(proj, dname)
        proj.Save()
        print("工程已保存")
        after = _results_info(proj, dname)
        if snap and after and snap.get("files") == after.get("files"):
            print("保存后结果目录: 完好（%d 个文件）" % after.get("files", 0))
        else:
            print("!! 保存后结果文件数变化，请立即检查")
            return 3

        temps_new = _temps_from_file(str(proj.GetPath()), pname, dname)
        print("赋值后  :")
        hit = 0
        for o in objs:
            v = temps_new.get(o)
            if v == tval:
                hit += 1
            print("    [%s] %-30s %s"
                  % ("OK" if v == tval else "??", o, v or "（未设置）"))
        print("-" * 74)
        print("复核    : %d/%d 个实体温度表已为 %s" % (hit, len(objs), tval))
        print("提示    : 温度只在材料带温度系数（thermal modifier）时影响损耗；")
        print("          属求解设置修改，重新求解后才体现在结果里。")
        print("@@TEMPS@@" + json.dumps(
            {o: temps_new.get(o) for o in objs}, ensure_ascii=False))
        return 0
    finally:
        disconnect(d)


def _pin_variation(variation, pin):
    """把总览页所选求解点 "参数=值" 覆盖进扁平 intrinsics 列表。

    variation 形如 ["Freq:=", "650kHz", "Phase:=", "0deg"]（Name:=/值 成对）；
    匹配到同名 intrinsic 则覆盖其值，否则追加到末尾。
    """
    out = list(variation)
    if not pin or "=" not in pin:
        return out
    pn, pv = pin.split("=", 1)
    tag = pn.strip() + ":="
    pv = pv.strip()
    done = False
    i = 0
    while i < len(out):
        if i + 1 < len(out) and str(out[i]).strip() == tag:
            out[i + 1] = pv
            done = True
            i += 2
        else:
            i += 1
    if not done:
        out += [tag, pv]
    return out


def cmd_irms(a):
    """读入栈剖面的电流【有效值】。

    峰值 = 场计算器 Integrate(Surface(片), CmplxMag(Scalar?(<Jx,Jy,Jz>)))
    （用户 2026-09-10 手验的写法），有效值 = 峰值 / sqrt(2)。
    求值在 AEDT 内部 IronPython 完成（CalculatorWrite → .fld），
    不建 Maxwell 报表、不建命名表达式、不动几何。
    输出一行 @@CURBAR@@{...} 供 GUI 画柱状图。
    """
    items = []
    for tok in a.items.split(","):
        tok = tok.strip()
        if not tok:
            continue
        parts = tok.split(":")
        if len(parts) != 3:
            print("!! --items 片段格式应为 label:sheet:scalar，得到: %s" % tok)
            continue
        lab, sh, sc = (parts[0].strip(), parts[1].strip(), parts[2].strip())
        if sc not in ("ScalarX", "ScalarY", "ScalarZ"):
            print("!! 标量分量不合法: %s（只能是 ScalarX/Y/Z）" % sc)
            continue
        items.append([lab, sh, sc])
    if not items:
        print("!! 没有可计算的剖面")
        return 2

    d = connect(port=a.port)
    try:
        proj = _find_project(d, getattr(a, "project", ""),
                             getattr(a, "design", ""))
        if proj is None:
            print("!! 找不到打开的工程 %s（或其中没有设计 %s）"
                  % (getattr(a, "project", "") or "<激活>",
                     getattr(a, "design", "") or "<任意>"))
            return 2
        pname = str(proj.GetName())
        des = get_design(proj, getattr(a, "design", ""))
        dname = str(des.GetName())
        if a.design and a.design != dname:
            print("!! 当前激活设计是 %s，与界面上的 %s 不一致"
                  % (dname, a.design))
            return 2
        print("工程 : %s" % pname)
        print("设计 : %s" % dname)
        print("剖面 : %d 个" % len(items))
        for lab, sh, sc in items:
            print("    %-14s %-28s %s" % (lab, sh, sc))

        # setup 名 + intrinsics 变体（默认取 setup 的 default_intrinsics）
        from ansys.aedt.core import Maxwell3d
        m3d = Maxwell3d(project=pname, design=dname, version="2026.1",
                        port=a.port, new_desktop=False, close_on_exit=False)
        setup = _resolve_setup(m3d, des, proj, getattr(a, "setup", ""))
        if not setup:
            print("!! 无法确定 setup（gRPC 不可用且总览页未选择求解点）")
            return 2
        variation = []
        try:
            variation = list(m3d.post._check_intrinsics(
                None, setup, return_list=True))
        except Exception as e:
            print("[提示] 取 intrinsics 失败(%s)，改用 setup 默认值"
                  % str(e)[:80])
            try:
                sn = setup.split(":")[0].strip()
                di = dict(m3d.design_setups[sn].default_intrinsics)
                for k, v in di.items():
                    variation += [str(k) + ":=",
                                  str(v[0] if isinstance(v, (list, tuple))
                                      else v)]
            except Exception as e2:
                print("[提示] default_intrinsics 也失败: %s" % str(e2)[:80])
        # 总览页所选求解点（可多条："全部参数首值"组合 -> 逐个覆盖）
        _pins = getattr(a, "var", None) or []
        if isinstance(_pins, str):
            _pins = [_pins] if _pins else []
        for _pin in _pins:
            variation = _pin_variation(variation, _pin)
            print("变体覆盖: %s（总览页所选求解点）" % _pin)
        print("解上下文: %s" % setup)
        print("变体    : %s" % variation)

        body = (IRMS_TEMPLATE
                .replace("__OUT__", IRMS_OUT)
                .replace("__FLD__", IRMS_FLD)
                .replace("__SETUP__", setup)
                .replace("__VARIATION__", json.dumps(variation))
                .replace("__ITEMS__", json.dumps(items)))
        if os.path.isfile(IRMS_OUT):
            try:
                os.remove(IRMS_OUT)
            except Exception:
                pass
        with open(IRMS_PY, "w") as fh:
            fh.write(body)
        print("RunScript: %s" % IRMS_PY)
        d.odesktop.RunScript(IRMS_PY)
        time.sleep(0.3)

        pool, bad = {}, []
        if os.path.isfile(IRMS_OUT):
            with open(IRMS_OUT, "r") as fh:
                for ln in fh:
                    ln = ln.rstrip()
                    print("   " + ln)
                    if ln.startswith("  DATA "):
                        seg = ln[7:].strip().split("|")
                        if len(seg) == 3:
                            lab = seg[0].strip()
                            meta = dict(
                                (it[0], it) for it in items).get(lab)
                            pool[lab] = dict(
                                peak=float(seg[1]), rms=float(seg[2]),
                                sheet=(meta[1] if meta else ""),
                                scalar=(meta[2] if meta else ""))
        for lab, sh, sc in items:
            if lab not in pool:
                bad.append(lab)
        if not pool:
            print("!! 全部取值失败（看上面的 IronPython 日志）")
            return 2
        if bad:
            print("[提示] %d 个剖面没取到值: %s" % (len(bad), ", ".join(bad)))
        print("@@CURBAR@@" + json.dumps(
            dict(pool=pool, project=pname, design=dname, setup=setup,
                 variation=variation, factor=IRMS_RMS_FACTOR),
            ensure_ascii=False))
        return 0
    finally:
        disconnect(d)



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=GRPC_PORT)
    sub = ap.add_subparsers(dest="cmd", required=True)

    # --port 放在子命令前后都行：子命令里用 SUPPRESS 默认，
    # 没显式给就不覆盖顶层解析出来的值（否则默认值会把顶层值冲掉）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", type=int, default=argparse.SUPPRESS,
                        help="gRPC 端口，放在子命令前后都可以")

    sub.add_parser("scan", parents=[common]).set_defaults(fn=cmd_scan)
    sub.add_parser("mats", parents=[common]).set_defaults(fn=cmd_mats)

    p = sub.add_parser("ohmic", parents=[common])
    p.add_argument("--objects", required=True)
    p.add_argument("--prefix", default="OhmicLoss_")
    p.add_argument("--batch", type=int, default=20)
    p.add_argument("--no-save", action="store_true")
    p.set_defaults(fn=cmd_ohmic)

    p = sub.add_parser("dropvars", parents=[common])
    p.add_argument("--names", required=True, help="逗号分隔的变量名")
    p.add_argument("--no-save", action="store_true")
    p.set_defaults(fn=cmd_dropvars)

    p = sub.add_parser("delsheets", parents=[common])
    p.add_argument("--objects", required=True, help="逗号分隔的片体名")
    p.add_argument("--no-save", action="store_true")
    p.set_defaults(fn=cmd_delsheets)

    p = sub.add_parser("delreports", parents=[common])
    p.add_argument("--names", required=True, help="逗号分隔的报表名")
    p.add_argument("--no-save", action="store_true")
    p.set_defaults(fn=cmd_delreports)

    p = sub.add_parser("delfplots", parents=[common])
    p.add_argument("--names", required=True, help="逗号分隔的场图名")
    p.add_argument("--no-save", action="store_true")
    p.set_defaults(fn=cmd_delfplots)

    p = sub.add_parser("oldprefix", parents=[common])
    p.add_argument("--objects", required=True, help="逗号分隔的片体名")
    p.add_argument("--no-save", action="store_true")
    p.set_defaults(fn=cmd_oldprefix)

    p = sub.add_parser("jplot", parents=[common])
    p.add_argument("--objects", required=True, help="逗号分隔的实体名")
    p.add_argument("--types", default="Mag,Vector",
                   help="J 类型，逗号分隔，可选 Mag / Vector")
    p.add_argument("--phase", default="0deg", help="相位，默认 0deg")
    p.add_argument("--surface-only", action="store_true",
                   help="建纯表面场图（create_fieldplot_surface，引用体"
                        "全部外表面面片；不勾则建体场图）")
    p.add_argument("--no-save", action="store_true")
    p.set_defaults(fn=cmd_jplot)

    p = sub.add_parser("temp", parents=[common])
    p.add_argument("--objects", required=True, help="逗号分隔的实体名")
    p.add_argument("--temp", default="",
                   help="温度：摄氏度数字或带单位（如 22 或 105cel）；"
                        "--read-only 时可省")
    p.add_argument("--read-only", action="store_true",
                   help="只读取当前温度，不赋值、不保存")
    p.add_argument("--dep", type=int, default=1,
                   help="IncludeTemperatureDependence 1/0，默认 1")
    p.add_argument("--feedback", type=int, default=0,
                   help="EnableFeedback 1/0，默认 0")
    p.add_argument("--no-save", action="store_true")
    p.set_defaults(fn=cmd_temperature)

    p = sub.add_parser("barloss-all", parents=[common])
    p.add_argument("--setup", default="",
                   help="总览页所选 setup 名（默认自动解析）")
    p.add_argument("--project", default="",
                   help="校验/定位用：期望的工程名（重名工程自动消歧）")
    p.add_argument("--design", default="",
                   help="校验/定位用：期望的设计名")
    p.add_argument("--no-save", action="store_true")
    p.set_defaults(fn=cmd_barloss_all)

    p = sub.add_parser("irms", parents=[common])
    p.add_argument("--items", required=True,
                   help="逗号分隔的 label:sheet:ScalarX|Y|Z")
    p.add_argument("--project", default="", help="校验用：期望的工程名")
    p.add_argument("--design", default="", help="校验用：期望的设计名")
    p.add_argument("--setup", default="",
                   help="总览页所选 setup 名（默认 nominal_adaptive）")
    p.add_argument("--var", action="append", default=[],
                   help="总览页所选求解点，可重复：参数=值（如 Freq=650kHz）")
    p.set_defaults(fn=cmd_irms)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    main()
