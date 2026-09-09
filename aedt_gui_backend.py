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
    barloss-all                 一次性读取场计算器全部 OhmicLoss_* 表达式
                                （PyAEDT get_solution_data，X=Freq 自动回退
                                Phase；坑 #46）+ 建/复用总报表 LossBar_All
                                （存在即复用，绝不重建），
                                输出 @@LOSSBARALL@@{pool...} 供 GUI 缓存；
                                之后入栈实体出图纯 GUI 本地，零 AEDT 交互
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

        try:
            setups = [str(x) for x in des.GetModule("AnalysisSetup").GetSetups()]
        except Exception:
            setups = []

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
                    expressions=ex, temps=temps,
                    results=ri, setups=setups)
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



def cmd_barloss_all(a):
    d = connect(port=a.port)
    try:
        proj = d.odesktop.GetActiveProject()
        if proj is None:
            print("!! 没有打开的工程")
            return 2
        pname = str(proj.GetName())
        des = get_design(proj)
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

        # PyAEDT 报表通道（post.get_solution_data / create_report，
        # 电流积分 pipeline 同款已验证路径；坑 #46：context 必须 "None"）
        from ansys.aedt.core import Maxwell3d
        m3d = Maxwell3d(project=pname, design=dname, version="2026.1",
                        port=a.port, new_desktop=False, close_on_exit=False)
        setup = str(m3d.nominal_adaptive)
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

        try:
            sd = m3d.post.get_solution_data(
                expressions=target, setup_sweep_name=setup,
                primary_sweep_variable="Freq", context="None",
                variations=variations)
            try:
                psv = list(sd.primary_sweep_values) \
                    if sd.primary_sweep_values is not None else []
            except Exception:
                psv = []
            sweeps = [_f(v) for v in psv]
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
                    variations=variations)
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
             "pool": pool, "total": total}, ensure_ascii=False))

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
    p.add_argument("--no-save", action="store_true")
    p.set_defaults(fn=cmd_barloss_all)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    main()
