# -*- coding: utf-8 -*-
r"""
【电流积分一条龙】剖面 -> 场计算器电流积分 -> Maxwell 内 Fields Report (Rectangular Plot)

触发语（用户原话的几种说法）:
    "我想看 Sec_74 的电流积分"
    "帮我看一下 Sec_8/26/32 的电流随 phase 变化"
    "给 Sec_2 和 Sec_66 出电流曲线"
    "我想看某个 copper 实体的电流积分"

==> 一律只跑这一条命令，不要再分步、不要再问坐标系/剖切方向。

用户约定（2026-09-02 明确）:
  1. **坐标系用户已提前选好** —— 脚本只读并打印激活坐标系，**绝不切换**。
  2. 一个命令走完：剖面(Non-model) -> 场计算器 AtPhase 电流积分 -> 建 Maxwell 报表。
  3. 报表必须建在 **Maxwell 里**（Results -> Create Fields Report -> Rectangular Plot），
     不是外部 matplotlib 图片；PNG 只作聊天窗口里的预览。
  4. 旧剖面 / 弃用片一律**改名**为 OLD_ / OLD_DROP_ 前缀（改名不影响结果文件；
     删除 Model 几何会毁结果）。
  5. 同名旧变量自动删除后重建，无需人工干预。
  6. 全程**大小指纹**护栏：结果目录体积指纹有任何变化就拒绝保存。
  7. 报表 Context→Geometry 必须 None、Primary Sweep=Phase（2026-09-03 明确）：
     PyAEDT 的 create_report / get_solution_data 不传 context 且设计里存在
     polyline 时，会自动把 modeler.line_names[0] 当成 Fields 报表 Geometry
     —— 这就是 Geometry=polyline 的来源。两处都已显式传 context="None" 规避。

场计算器栈（法向 X 的 YZ 剖面为例）:
    EnterQty("J") -> CalcOp("ScalarX") -> EnterScalarFunc("Phase")
    -> CalcOp("AtPhase") -> EnterSurf(<片>) -> CalcOp("Integrate")
    -> AddNamedExpression("I_sec_2", "Fields")
得到: I_sec_2 = Integrate(Surface(Sec_2_Section1), AtPhase(ScalarX(<Jx,Jy,Jz>), Phase))

用法:
    python current_integral_pipeline.py --objects Sec_74,Sec_16
    python current_integral_pipeline.py --objects Sec_2 --plane YZ
    python current_integral_pipeline.py --objects Sec_2,Sec_66 --dry-run
    python current_integral_pipeline.py --objects Sec_8 --project A4_20_..._0901
"""
import argparse
import csv
import math
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from aedt_env import connect, disconnect, AEDT_VERSION   # noqa: E402
from section_cs import (                                 # noqa: E402
    PLANE_INFO, AXIS_NAME, OLD_PREFIX,
    obj_map, model_flag, rename, drop_var, exprs,
    diff_results, do_one,
)

JUNK_PREFIX = ("Calculator Expressions Plot", "Calculator Expressions Table")


# ----------------------------------------------------------------- 取数/拟合
def to_float(v):
    """PyAEDT 1.4.0 的 get_expression_data 可能给 complex / str / numpy 标量。"""
    try:
        return float(v.real) if isinstance(v, complex) else float(v)
    except Exception:
        try:
            return float(str(v).strip())
        except Exception:
            return float("nan")


def fit_sinusoid(ph, y):
    """最小二乘拟合 y = A*cos(theta - phi) + dc，返回 (A, phi_deg, dc)。

    ⚠️ 必须用它取相位。直接取 CSV 极值会误判：采样点恰好落在 0/180 附近时，
    两条同相曲线会被读成"相差 180°"（2026-09-02 实际踩过）。
    """
    ph, y = list(ph), list(y)
    if len(ph) < 4:
        return None
    # 0..360 闭区间最后一个点与首点重复，去掉避免偏置
    if len(ph) > 4 and abs((ph[-1] - ph[0]) - 360.0) < 1e-6:
        ph, y = ph[:-1], y[:-1]
    w = 2.0 * math.pi / 360.0
    sa = sb = saa = sab = sbb = 0.0
    for t, v in zip(ph, y):
        c, s = math.cos(w * t), math.sin(w * t)
        sa += c * v
        sb += s * v
        saa += c * c
        sab += c * s
        sbb += s * s
    det = saa * sbb - sab * sab
    if abs(det) < 1e-12:
        return None
    a = (sa * sbb - sb * sab) / det
    b = (-sa * sab + sb * saa) / det
    return (math.hypot(a, b),
            math.degrees(math.atan2(b, a)) % 360.0,
            sum(y) / len(y))


def auto_report_name(objects):
    nums = [o.split("_")[-1] for o in objects]
    if len(nums) <= 4:
        return "I_sec_" + "_".join(nums) + "_vs_Phase"
    return "I_sec_%s_etc%d_vs_Phase" % (nums[0], len(nums))


# ----------------------------------------------------------------- 结果护栏
def results_dir(m3d):
    return os.path.join(str(m3d.project_path), str(m3d.project_name) +
                        ".aedtresults", str(m3d.design_name) + ".results")


def snap_results(m3d):
    """{相对路径: (size, mtime)}，用大小指纹比对（坑 #34）。"""
    rd = results_dir(m3d)
    if not os.path.isdir(rd):
        return None
    out = {}
    for root, _dirs, files in os.walk(rd):
        for f in files:
            p = os.path.join(root, f)
            try:
                st = os.stat(p)
                out[os.path.relpath(p, rd)] = (st.st_size, int(st.st_mtime))
            except Exception:
                out[os.path.relpath(p, rd)] = (-2, 0)
    return out


def backup(m3d):
    src = os.path.join(str(m3d.project_path), str(m3d.project_name) + ".aedt")
    if not os.path.isfile(src):
        return None
    dst = src.replace(".aedt", "_backup_pipe_%s.aedt" % time.strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(src, dst)
    print("   已备份 -> %s (%.1f MB)"
          % (os.path.basename(dst), os.path.getsize(dst) / 1e6))
    return dst


# ----------------------------------------------------------------- 工程定位
def pick_target(d, objects, want_project, want_design, port):
    """返回可用的 Maxwell3d 实例，失败返回 None。

    坑 #40：AEDT 可同时开多个工程，用户界面的"激活工程"可能已经切走 ——
            静默打到错误工程上（2026-09-02 冒烟测试当场复现）。
    坑 #42：`proj.GetActiveDesign()` 在 gRPC 下**时通时断**（同一个工程
            第一次通、切一次就不通），不能作为定位手段。
    → 一律用 `Maxwell3d(project=..., design=None)`，它会自己激活工程并
      给出 `design_name` / `modeler.object_names`，稳定可靠。
    → 多于 1 个工程且未指定 --project 时，按"哪个工程含全部目标物体"自动挑；
      命中 0 个或多个则明确中止并列出候选，绝不猜。
    """
    from ansys.aedt.core import Maxwell3d

    names = [str(p.GetName()) for p in d.odesktop.GetProjects()]
    print("已打开工程 (%d): %s" % (len(names), ", ".join(names)))

    if want_project and want_project not in names:
        print("!! 工程 '%s' 未打开（现有: %s）" % (want_project, names))
        return None
    pool = [want_project] if want_project else names
    multi = len(pool) > 1
    if multi:
        print("!! 开着 %d 个工程且未指定 --project —— 按'含全部目标物体'自动定位:" % len(names))

    projs = {str(p.GetName()): p for p in d.odesktop.GetProjects()}

    def design_of(nm):
        """优先拿"真正的当前设计"；GetActiveDesign 失效时退回 None（坑 #42）。

        坑 #43：Maxwell3d(design=<不存在的名字>) 会**静默新建**设计，不报错。
        所以取到的名字必须先跟 GetChildNames() 对一遍，不在就退回 None。
        """
        if want_design:
            return want_design
        try:
            got = str(projs[nm].GetActiveDesign().GetName())
        except Exception:
            return None
        try:
            if got not in [str(x) for x in projs[nm].GetChildNames()]:
                return None
        except Exception:
            return None
        return got

    def open(nm, dsg):
        try:
            return Maxwell3d(project=nm, design=dsg or None, version=AEDT_VERSION,
                             port=port, new_desktop=False, close_on_exit=False)
        except Exception:
            if not dsg:
                raise
            return Maxwell3d(project=nm, design=None, version=AEDT_VERSION,
                             port=port, new_desktop=False, close_on_exit=False)

    hits = []
    for nm in pool:
        try:
            m = open(nm, design_of(nm))
            have = set(str(x) for x in m.modeler.object_names)
            miss = [o for o in objects if o not in have]
            if multi or want_project:
                print("   探测 %-50s / %-18s 缺: %s"
                      % (nm, m.design_name, ", ".join(miss) if miss else "无"))
            if not miss:
                hits.append(m)
        except Exception as e:
            print("   探测 %-50s 失败: %s" % (nm, str(e)[:100]))

    if len(hits) == 1:
        m = hits[0]
        if multi:
            print("   -> 选中 %s / %s" % (m.project_name, m.design_name))
        try:
            d.odesktop.SetActiveProject(str(m.project_name))
        except Exception:
            pass
        return m
    if not hits:
        print("!! 没有任何候选工程/设计同时含 %s —— 中止" % ", ".join(objects))
        return None
    print("!! %d 个候选都含全部目标物体: %s —— 请用 --project / --design 指定"
          % (len(hits), [(m.project_name, m.design_name) for m in hits]))
    return None


def suggest(have, obj, n=8):
    """目标物体找不到时给出同前缀候选名，省一轮来回。"""
    pre = obj.rsplit("_", 1)[0] if "_" in obj else obj[:3]
    return sorted(x for x in have if x.startswith(pre))[:n]


# ----------------------------------------------------------------- 报表
def all_reports(m3d):
    try:
        return sorted(str(x) for x in m3d.post.all_report_names)
    except Exception:
        return []


def cleanup_junk(m3d, keep):
    """删掉 get_solution_data() 偷偷建的 Calculator Expressions Plot/Table。"""
    gone = []
    for n in all_reports(m3d):
        if n in keep:
            continue
        if n.startswith(JUNK_PREFIX):
            try:
                m3d.post.delete_report(n)
                gone.append(n)
            except Exception as e:
                print("   [警告] 删除垃圾报表 %s 失败: %s" % (n, str(e)[:80]))
    if gone:
        print("   已清理垃圾报表: %s" % gone)
    return gone


def plot_png(path, sweeps, series, title):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        for f in ("Microsoft YaHei", "SimHei", "DejaVu Sans"):
            try:
                matplotlib.font_manager.findfont(f, fallback_to_default=False)
                plt.rcParams["font.sans-serif"] = [f]
                break
            except Exception:
                continue
        plt.rcParams["axes.unicode_minus"] = False
        colors = ("#c0392b", "#2471a3", "#1e8449", "#d35400",
                  "#8e44ad", "#16a085", "#c2185b", "#607d8b")
        fig, ax = plt.subplots(figsize=(9.8, 5.2), dpi=150)
        for i, (e, vals) in enumerate(series.items()):
            ax.plot(sweeps[:len(vals)], vals, lw=1.9, color=colors[i % len(colors)],
                    label="%s (peak %.4g A)" % (e, max(abs(v) for v in vals)))
        ax.axhline(0, color="#888", lw=0.8)
        ax.set_xlabel("Phase (deg)")
        ax.set_ylabel("Current (A)")
        ax.set_title(title, fontsize=11)
        ax.grid(True, alpha=0.3, ls=":")
        ax.legend(loc="best", fontsize=9)
        fig.tight_layout()
        fig.savefig(path, dpi=150)
        plt.close(fig)
        return path
    except Exception as e:
        print("   [提示] PNG 预览失败（不影响 Maxwell 报表）: %s" % str(e)[:110])
        return None


# ----------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(
        description="电流积分一条龙：剖面 -> 场计算器积分 -> Maxwell Fields Report")
    ap.add_argument("--objects", required=True, help="逗号分隔的实体名，如 Sec_2,Sec_66")
    ap.add_argument("--project", default="", help="默认自动定位（按含全部目标物体）")
    ap.add_argument("--design", default="", help="默认用该工程的当前设计")
    ap.add_argument("--plane", default="ZX", choices=["ZX", "YZ", "XY"],
                    help="剖切面，默认 ZX（法向 Y / ScalarY）")
    ap.add_argument("--span-axis", default="", choices=["", "X", "Y", "Z"],
                    help="挑'最大片'用的轴，默认由剖切面自动决定")
    ap.add_argument("--report", default="", help="报表名，默认自动生成")
    ap.add_argument("--sweep", default="Phase")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report-only", action="store_true",
                     help="表达式已在场计算器：跳过剖面/积分，直接建报表+取数")
    ap.add_argument("--no-report", action="store_true",
                     help="只建剖面 + 场计算器表达式，不建报表/取数"
                          "（给【电流柱状图】用）")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--no-png", action="store_true")
    ap.add_argument("--port", type=int, default=50051)
    args = ap.parse_args()

    try:  # 全程实时输出：即使中途卡死，日志也能看到卡在哪一句
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    objects = [s.strip() for s in args.objects.split(",") if s.strip()]
    if not objects:
        print("!! --objects 为空")
        return 2
    plane = args.plane.upper()
    varnames = ["I_sec_" + o.split("_")[-1] for o in objects]
    report = args.report or auto_report_name(objects)

    d = connect(port=args.port)
    try:
        m3d = pick_target(d, objects, args.project, args.design, args.port)
        if m3d is None:
            return 2
        pname, dname = str(m3d.project_name), str(m3d.design_name)
        ed = m3d.modeler.oeditor
        oFR = m3d.odesign.GetModule("FieldsReporter")
        print("工程 %s | 设计 %s" % (pname, dname))

        # ---- 坐标系：只读，绝不切换 ----
        try:
            cs = ed.GetActiveCoordinateSystem()
        except Exception:
            cs = "?"
        try:
            ncs = len(ed.GetCoordinateSystems())
        except Exception:
            ncs = -1
        pinfo = PLANE_INFO[plane]
        span_axis = ({"X": 0, "Y": 1, "Z": 2}[args.span_axis]
                     if args.span_axis else pinfo["axis"])
        print("激活坐标系: %s （共 %d 个）—— 用户已选好，脚本不切换" % (cs, ncs))
        print("剖切平面  : %s（法向 %s，场计算器取 %s）"
              % (plane, pinfo["normal"], pinfo["scalar"]))
        print("选片判据  : 面内 %s 跨度最大的那片；弃片改名 OLD_DROP_*（不删）"
              % AXIS_NAME[span_axis])
        print("目标      : %s -> %s" % (", ".join(objects), ", ".join(varnames)))
        print("报表      : %s（Rectangular Plot, X=%s）" % (report, args.sweep))

        setup = str(m3d.nominal_adaptive)
        print("解上下文  : %s" % setup)

        snap_before = snap_results(m3d)
        print("结果目录  : %s" % results_dir(m3d))
        print("结果快照  : %d 个文件" % (len(snap_before) if snap_before else -1))

        baseline = obj_map(ed)
        print("物体总数  : %d" % len(baseline))
        miss = [o for o in objects if o not in baseline]
        if miss:
            for o in miss:
                print("!! 物体不存在: %s" % o)
                cands = suggest(baseline, o)
                if cands:
                    print("   同前缀候选: %s" % ", ".join(cands))
            print("!! 中止（物体名不对，未做任何修改）")
            return 2

        print("")
        if args.dry_run:
            for obj, v in zip(objects, varnames):
                print("  [dry-run] %s -> %s" % (obj, v))
            print("（--dry-run，未修改）")
            return 0

        if args.report_only:
            print("=" * 74)
            print("report-only  跳过剖面/积分，直接建报表（表达式应已在场计算器里）")
            have = [e.split("=")[0].strip() for e in exprs(oFR)]
            missing = [v for v in varnames if v not in have]
            if missing:
                print("!! 场计算器缺表达式: %s" % missing)
                print("   先跑完整流程补积分，或核对 --objects 是否与上次一致")
                return 2
            ok_list = [(o, v, "") for o, v in zip(objects, varnames)]
            print("表达式齐全: %s" % ", ".join(varnames))
        else:
            # ================= 步骤 1/4：清旧（改名 + 删变量） =================
            print("=" * 74)
            print("步骤 1/4  清理旧剖面与旧变量（改名 / 删表达式，不动结果）")
            for obj, varname in zip(objects, varnames):
                olds = sorted(n for n in baseline
                              if n.startswith(obj + "_Section")
                              and not n.startswith(OLD_PREFIX))
                for o in olds:
                    new = OLD_PREFIX + o
                    ok, msg = rename(ed, o, new)
                    print("  改名 %-34s -> %-40s %s"
                          % (o, new, "OK" if ok else "失败: " + msg))
                have = [e.split("=")[0].strip() for e in exprs(oFR)]
                if varname in have:
                    ok, msg = drop_var(m3d.odesign, oFR, varname)
                    print("  删变量 %-20s %s" % (varname, "OK" if ok else "失败: " + msg))

            if not args.no_backup:
                backup(m3d)

            # ================= 步骤 2/4：剖面 + 场计算器积分 =================
            print("=" * 74)
            print("步骤 2/4  建 Non-model 剖面 + 场计算器电流积分")
            baseline2 = obj_map(ed)
            ok_list, fail_list = [], []
            for obj, varname in zip(objects, varnames):
                print("  %-12s -> %s" % (obj, varname))
                ok, keep, msg = do_one(m3d, ed, oFR, obj, plane, varname,
                                       False, span_axis, discard="rename")
                if ok:
                    ok_list.append((obj, varname, keep))
                    print("      [OK]   %s" % msg)
                else:
                    fail_list.append((obj, msg))
                    print("      [失败] %s" % msg)
                sys.stdout.flush()

            print("-" * 74)
            print("成功 %d / 失败 %d" % (len(ok_list), len(fail_list)))
            added = sorted(set(obj_map(ed)) - set(baseline2))
            bad_new = [n for n in added if model_flag(ed, n) == "true"]
            print("新增物体 %d: %s" % (len(added), added))
            if bad_new:
                print("!! 新增物体中有 Model=true: %s —— 拒绝保存" % bad_new)
                return 3
            if not ok_list:
                print("!! 全部失败，跳过建报表")
                return 3

            if args.no_report:
                print("=" * 74)
                print("no-report：只建剖面 + 场计算器表达式，跳过报表/取数")
                print("剖面片    : %s" % ", ".join(k for _, _, k in ok_list))
                _safe, _msgs = diff_results(snap_before, snap_results(m3d))
                print("保存前结果目录: %s" % ("完好" if _safe else "!! 异常 !!"))
                for _m in _msgs:
                    print("   %s" % _m)
                if args.no_save:
                    print("（--no-save，未保存）")
                    return 0
                if not _safe:
                    print("!! 结果目录有变化，拒绝保存")
                    return 3
                m3d.save_project()
                _post = snap_results(m3d)
                _safe2, _msgs2 = diff_results(snap_before, _post)
                print("保存完成 | 保存后结果目录: %s"
                      % ("完好（大小指纹一致，%d 个文件）" % len(_post)
                         if _safe2 else "!! 已损坏 !!"))
                for _m in _msgs2:
                    print("   %s" % _m)
                return 0 if _safe2 else 3

        # ================= 步骤 3/4：建 Maxwell Fields Report =================
        print("=" * 74)
        print("步骤 3/4  在 Maxwell 里建 Fields Report")
        try:
            _lns = m3d.modeler.line_names
        except Exception:
            _lns = []
        print("设计内 polyline: %d 个 %s（>0 时旧版 PyAEDT 会自动拿第一条当报表 Geometry）"
              % (len(_lns), list(_lns)[:3]))
        expr_list = [v for _, v, _ in ok_list]
        # 坑 #52（Families 防展开）：钉除主扫描外全部 intrinsics 为单值，
        # 防 PyAEDT 把 Phase/Freq 等设成 "All" 导致报表笛卡尔展开卡死。
        try:
            _sname = setup.split(":")[0].strip()
            _di = dict(m3d.design_setups[_sname].default_intrinsics)
        except Exception:
            _di = {}
        variations = {k: [x] for k, x in _di.items()}
        variations[args.sweep] = ["All"]
        have0 = all_reports(m3d)
        print("已有报表 (%d): %s" % (len(have0), have0))
        try:
            if report in have0:
                m3d.post.delete_report(report)
                print("   同名旧报表已覆盖: %s" % report)
            print(">> 正在调用 create_report(context=None)…")
            m3d.post.create_report(
                expressions=expr_list,
                setup_sweep_name=setup,
                primary_sweep_variable=args.sweep,
                plot_type="Rectangular Plot",
                report_category="Fields",
                context="None",  # 显式空几何；不传时 PyAEDT 在设计有 polyline 会拿 line_names[0] 当 Geometry
                plot_name=report,
                variations=variations,
            )
            print("   报表已建: %s" % report)
        except Exception as e:
            print("!! 建报表失败: %s" % str(e)[:160])

        # ================= 步骤 4/4：取数 + 拟合 + 清垃圾 =================
        print("=" * 74)
        print("步骤 4/4  取数 / 正弦拟合 / 清理临时报表")
        series, sweeps = {}, []
        try:
            sd = m3d.post.get_solution_data(
                expressions=expr_list, setup_sweep_name=setup,
                primary_sweep_variable=args.sweep, context="None",
                variations=variations)
            sweeps = [to_float(v) for v in sd.primary_sweep_values]
            for e in expr_list:
                got = sd.get_expression_data(e)
                raw = got[1] if isinstance(got, (tuple, list)) else got
                series[e] = [to_float(v) for v in raw]
        except Exception as ex:
            print("   [提示] 取数失败（报表已建，可手动在 Maxwell 里打开）: %s"
                  % str(ex)[:140])
        cleanup_junk(m3d, keep=set(have0) | {report})

        if series and sweeps:
            fits = {}
            print("")
            print("  %-14s %12s %12s %11s %13s"
                  % ("变量", "峰值 A", "RMS A", "相位 deg", "直流偏置 A"))
            print("  " + "-" * 66)
            for e in expr_list:
                y = series[e]
                peak = max(abs(v) for v in y)
                f = fit_sinusoid(sweeps, y)
                if f:
                    A, phi, dc = f
                    fits[e] = (A, phi)
                    print("  %-14s %12.4f %12.4f %11.2f %13.4f"
                          % (e, A, A / 2 ** 0.5, phi, dc))
                else:
                    print("  %-14s %12.4f %12s %11s %13s" % (e, peak, "-", "-", "-"))

            if len(fits) >= 2:
                ks = [e for e in expr_list if e in fits]
                base = fits[ks[0]][0]
                print("")
                for k in ks[1:]:
                    A, phi = fits[k]
                    dphi = ((phi - fits[ks[0]][1] + 180.0) % 360.0) - 180.0
                    print("  幅度比 %s/%s = %.4f (%+.2f%%) | 相位差 %+.2f deg"
                          % (k, ks[0], A / base, (A / base - 1) * 100, dphi))

            # 方向自检：幅值异常小 == 剖切面方向大概率选错
            mx = max((t[0] for t in fits.values()), default=0.0)
            warn = []
            for e in expr_list:
                if e not in fits:
                    continue
                A = fits[e][0]
                if A < 0.01:
                    warn.append((e, A, "幅值几乎为零 —— 剖切面方向很可能选错"))
                elif mx > 0 and A < 0.05 * mx:
                    warn.append((e, A, "比同批其它实体小 20 倍以上，检查剖切位置"))
            if warn:
                print("")
                print("  !! 方向自检告警:")
                for e, A, why in warn:
                    print("     %-14s A=%.4g  %s" % (e, A, why))
                print("     若确实选错：重跑时加 --plane %s（旧剖面会自动改名，可放心重来）"
                      % ("YZ" if plane != "YZ" else "ZX"))

            csv_path = os.path.join(HERE, report + ".csv")
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["Phase_deg"] + expr_list)
                for i, ph in enumerate(sweeps):
                    w.writerow([ph] + [series[e][i] for e in expr_list])
            print("\n  已导出 CSV: %s" % csv_path)

            if not args.no_png:
                png = plot_png(os.path.join(HERE, report + ".png"), sweeps,
                               {e: series[e] for e in expr_list},
                               "Current vs Phase | %s / %s" % (pname, dname))
                if png:
                    print("  已出预览图: %s（仅供聊天窗口查看，正式报表在 Maxwell 里）" % png)

        # ================= 保存（护栏） =================
        print("=" * 74)
        safe, msgs = diff_results(snap_before, snap_results(m3d))
        print("保存前结果目录: %s" % ("完好" if safe else "!! 异常 !!"))
        for m in msgs:
            print("   %s" % m)

        if args.no_save:
            print("（--no-save，未保存；报表已在工程内存里）")
            return 0
        if not safe:
            print("!! 结果目录有变化，拒绝保存")
            return 3

        m3d.save_project()
        snap_post = snap_results(m3d)
        safe2, msgs2 = diff_results(snap_before, snap_post)
        print("保存完成 | 保存后结果目录: %s"
              % ("完好（大小指纹一致，%d 个文件）" % len(snap_post) if safe2
                 else "!! 已损坏 !!"))
        for m in msgs2:
            print("   %s" % m)
        print("最终报表 (%d): %s" % (len(all_reports(m3d)), all_reports(m3d)))
        if report in all_reports(m3d):
            print(">>> 在 Maxwell 里打开: Results -> %s" % report)
        return 0 if safe2 else 3
    finally:
        disconnect(d)


if __name__ == "__main__":
    sys.exit(main())
