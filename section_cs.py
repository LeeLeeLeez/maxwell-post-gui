# -*- coding: utf-8 -*-
"""
在**当前工作坐标系**（用户已选好，脚本不切换）下建 Non-model 剖面 → 分离体
→ 场计算器电流积分。

与 section_current_nm.py 的区别：
  1. 不动坐标系，用用户当前激活的那个（打印出来供确认）
  2. 可把"用不到的旧剖面"改名（--rename-old），改名不影响结果文件，
     比删除安全（删除 section 会导致 .results/ 被清空）
  3. 可删除旧的命名表达式（--drop-vars），只删表达式不动几何，不影响网格
  4. 全程结果目录快照护栏，任何文件消失/变小就拒绝保存

场计算器栈序（法向 Y 的 ZX 剖面）：
    EnterQty("J") -> CalcOp("ScalarY") -> EnterScalarFunc("Phase")
    -> CalcOp("AtPhase") -> EnterSurf(<片>) -> CalcOp("Integrate")
    -> AddNamedExpression("I_sec_73", "Fields")

用法:
    python section_cs.py --objects Sec_73 --rename-old --drop-vars
    python section_cs.py --objects Sec_74,Sec_16 --rename-old --drop-vars --no-save
"""
import argparse
import os
import shutil
import sys
import time

from aedt_env import connect, disconnect, get_design
GROUPS = ("Solids", "Sheets", "Unclassified", "Non Model", "Lines", "Points")
OLD_PREFIX = "OLD_"

# 剖切面 -> (法向, 场计算器标量, "最大片"判据所用的面内轴)
#   面内轴：ZX 面含 X/Z 取 X；YZ 面含 Y/Z 取 Y；XY 面含 X/Y 取 X
PLANE_INFO = {
    "ZX": dict(normal="Y", scalar="ScalarY", axis=0),
    "YZ": dict(normal="X", scalar="ScalarX", axis=1),
    "XY": dict(normal="Z", scalar="ScalarZ", axis=0),
}
AXIS_NAME = ("X", "Y", "Z")


def obj_map(ed):
    got = {}
    for g in GROUPS:
        try:
            for x in ed.GetObjectsInGroup(g):
                got.setdefault(str(x), g)
        except Exception:
            pass
    return got


def model_flag(ed, n):
    try:
        return str(ed.GetPropertyValue("Geometry3DAttributeTab", n, "Model")).lower()
    except Exception:
        return "?"


def rename(ed, old, new):
    try:
        ed.ChangeProperty(["NAME:AllTabs",
                           ["NAME:Geometry3DAttributeTab",
                            ["NAME:PropServers", old],
                            ["NAME:ChangedProps",
                             ["NAME:Name", "Value:=", new]]]])
        return True, ""
    except Exception as e:
        return False, str(e)[:110]


def drop_var(des, oFR, name):
    """删除命名表达式。只删表达式，不动几何，不影响网格。
    AEDT 正确方法名是 FieldsReporter.DeleteNamedExpr(name)（不是
    RemoveNamedExpression / DeleteNamedExpression）。"""
    last = "无可用方法"
    for owner, meth in ((oFR, "DeleteNamedExpr"),
                        (oFR, "RemoveNamedExpression"),
                        (des, "DeleteNamedExpression")):
        fn = getattr(owner, meth, None)
        if fn is None:
            continue
        try:
            fn(name)
            return True, "%s()" % meth
        except Exception as e:
            last = "%s() -> %s" % (meth, str(e)[:70])
    return False, last


def exprs(oFR):
    try:
        return [str(e) for e in oFR.GetFieldsCalculatorExpressions()]
    except Exception:
        return []


def x_span(m3d, n):
    try:
        b = [float(v) for v in m3d.modeler[n].bounding_box]
        return b[3] - b[0]
    except Exception:
        return -1.0


def span_of(m3d, n, axis):
    """指定轴（0=X 1=Y 2=Z）上的包围盒跨度，用于挑'最大的那片'。"""
    try:
        b = [float(v) for v in m3d.modeler[n].bounding_box]
        return b[axis + 3] - b[axis]
    except Exception:
        return -1.0


def snap_results(proj, dname):
    rd = os.path.join(str(proj.GetPath()), str(proj.GetName()) + ".aedtresults",
                      dname + ".results")
    if not os.path.isdir(rd):
        return None, rd
    out = {}
    for root, dirs, files in os.walk(rd):
        for f in files:
            p = os.path.join(root, f)
            try:
                st = os.stat(p)
                out[os.path.relpath(p, rd)] = (st.st_size, int(st.st_mtime))
            except Exception:
                out[os.path.relpath(p, rd)] = (-2, 0)
    return out, rd


def diff_results(b, a):
    """按**大小指纹**比对（坑 #34：Save 会把结果文件版本号重整 V67→V0，
    文件名会整体变，但体积与 mtime 不变）。只在指纹不一致时才报异常。"""
    if b is None or a is None:
        return False, ["结果目录不存在"]
    fb = sorted(v[0] for v in b.values())
    fa = sorted(v[0] for v in a.values())
    msgs = []
    if len(fb) != len(fa):
        msgs.append("文件数变化 %d -> %d" % (len(fb), len(fa)))
    if fb != fa:
        only_b = sorted(set(fb) - set(fa))
        only_a = sorted(set(fa) - set(fb))
        if only_b:
            msgs.append("消失的尺寸(前6): %s" % only_b[:6])
        if only_a:
            msgs.append("新增的尺寸(前6): %s" % only_a[:6])
    # 重命名（版本重整）单独提示，不算损坏
    renamed = len(set(b) - set(a))
    if renamed and not msgs:
        msgs.append("（文件名有 %d 个变化，但大小指纹一致 —— 属版本号重整，正常）"
                    % renamed)
    return (not msgs or (len(msgs) == 1 and msgs[0].startswith("（文件名"))), msgs


def backup(proj):
    src = os.path.join(str(proj.GetPath()), str(proj.GetName()) + ".aedt")
    if not os.path.isfile(src):
        return None
    dst = src.replace(".aedt", "_backup_cs_%s.aedt" % time.strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(src, dst)
    print("   已备份 -> %s (%.1f MB)"
          % (os.path.basename(dst), os.path.getsize(dst) / 1e6))
    return dst


def do_one(m3d, ed, oFR, obj, plane, varname, dry, span_axis=None,
           discard="delete"):
    # 0. 变量已存在？
    exist = [e.split("=")[0].strip() for e in exprs(oFR)]
    if varname in exist:
        return None, None, "变量 %s 已存在，跳过（先加 --drop-vars）" % varname

    before = obj_map(ed)
    if obj not in before:
        return False, None, "物体不存在: %s" % obj

    if dry:
        return None, None, "[dry-run] 会在当前坐标系下对 %s 剖 %s 面并建 %s" % (
            obj, plane, varname)

    # 1. 建剖面（NonModel）
    try:
        ed.Section(
            ["NAME:Selections", "Selections:=", obj,
             "NewPartsModelFlag:=", "NonModel"],
            ["NAME:SectionToParameters", "CreateNewObjects:=", True,
             "SectionPlane:=", plane, "SectionCrossObject:=", False])
    except Exception as e:
        return False, None, "Section 失败: %s" % str(e)[:130]

    after = obj_map(ed)
    new = sorted(set(after) - set(before))
    if not new:
        return False, None, "未产生新物体（剖切面与该实体无交集）"
    bad = [n for n in new if model_flag(ed, n) != "false"]
    if bad:
        return False, None, "新物体不是 Non-model: %s —— 已中止" % bad

    # 2. 分离体
    base = new[0]
    sep_note = ""
    try:
        ed.SeparateBody(
            ["NAME:Selections", "Selections:=", base,
             "NewPartsModelFlag:=", "NonModel"],
            ["CreateGroupsForNewObjects:=", False])
    except Exception as e:
        sep_note = "SeparateBody 提示: %s" % str(e)[:90]

    after2 = obj_map(ed)
    pieces = sorted(set(after2) - set(before))
    bad = [n for n in pieces if model_flag(ed, n) != "false"]
    if bad:
        return False, None, "分离后不是 Non-model: %s —— 已中止" % bad

    # 3. 保留面内跨度最大的一片（判据轴由剖切面决定，可用 --span-axis 覆盖）
    axis = PLANE_INFO[plane]["axis"] if span_axis is None else span_axis
    scored = sorted(((span_of(m3d, n, axis), n) for n in pieces), reverse=True)
    keep = scored[0][1]
    info = []
    for s, n in scored:
        bb = [round(float(v), 4) for v in m3d.modeler[n].bounding_box]
        info.append("      片 %-38s %s跨=%.4f bbox=%s%s"
                    % (n, AXIS_NAME[axis], s, bb,
                       "   <== 保留" if n == keep else ""))
    for n in pieces:
        if n != keep:
            if discard == "rename":
                new = "OLD_DROP_" + n
                ok, msg = rename(ed, n, new)
                print("      [弃片改名] %s -> %s %s"
                      % (n, new, "OK" if ok else "失败: " + msg))
                continue
            try:
                ed.Delete(["NAME:Selections", "Selections:=", n])
            except Exception as e:
                print("      [警告] 删除 %s 失败: %s" % (n, str(e)[:90]))

    if sep_note:
        print("      %s" % sep_note)
    for line in info:
        print(line)

    # 空几何检测（判据轴或另一条面内轴都退化了才算空）
    other = 2 if axis != 2 else 1          # 面内的第二条轴
    if span_of(m3d, keep, axis) <= 0.001 and span_of(m3d, keep, other) <= 0.001:
        return False, keep, "剖出的片为空（bbox 退化），该位置无材料"

    # 4. 场计算器（标量分量 = 剖切面法向）
    scalar = PLANE_INFO[plane]["scalar"]
    seq = [
        ('CalcStack("clear")', lambda: oFR.CalcStack("clear")),
        ('EnterQty("J")', lambda: oFR.EnterQty("J")),
        ('CalcOp("%s")' % scalar, (lambda sc=scalar: (lambda: oFR.CalcOp(sc)))()),
        ('EnterScalarFunc("Phase")', lambda: oFR.EnterScalarFunc("Phase")),
        ('CalcOp("AtPhase")', lambda: oFR.CalcOp("AtPhase")),
        ('EnterSurf(%s)' % keep, lambda: oFR.EnterSurf(keep)),
        ('CalcOp("Integrate")', lambda: oFR.CalcOp("Integrate")),
        ('AddNamedExpression(%s)' % varname,
         lambda: oFR.AddNamedExpression(varname, "Fields")),
    ]
    for label, fn in seq:
        try:
            fn()
        except Exception as e:
            try:
                oFR.CalcStack("clear")
            except Exception:
                pass
            return False, keep, "场计算器 %s 失败: %s" % (label, str(e)[:130])

    got = None
    for e in exprs(oFR):
        if e.startswith(varname):
            got = e
    want = ("%s = Integrate(Surface(%s), AtPhase(%s(<Jx,Jy,Jz>), Phase))"
            % (varname, keep, scalar))
    if got != want:
        return False, keep, "表达式形式不符\n         得到: %s\n         应为: %s" % (got, want)
    return True, keep, got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--objects", required=True)
    ap.add_argument("--project", default="Winding_Sim")
    ap.add_argument("--plane", default="ZX", choices=["ZX", "YZ", "XY"])
    ap.add_argument("--span-axis", default="", choices=["", "X", "Y", "Z"],
                    help="挑'最大片'用的轴，默认由剖切面自动决定")
    ap.add_argument("--rename-old", action="store_true",
                    help="把已存在的 <obj>_Section* 改名为 OLD_<obj>_Section*")
    ap.add_argument("--drop-vars", action="store_true",
                    help="删除已存在的 I_sec_<n> 命名表达式")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    objects = [s.strip() for s in args.objects.split(",") if s.strip()]
    plane = args.plane.upper()

    d = connect()
    try:
        names = [str(p.GetName()) for p in d.odesktop.GetProjects()]
        if args.project not in names:
            print("!! 工程不存在: %s（现有: %s）" % (args.project, names))
            return 2
        d.odesktop.SetActiveProject(args.project)
        proj = d.odesktop.GetActiveProject()
        des = get_design(proj)
        pname, dname = str(proj.GetName()), str(des.GetName())

        from ansys.aedt.core import Maxwell3d
        m3d = Maxwell3d(project=pname, design=dname, version="2026.1",
                        port=50051, new_desktop=False, close_on_exit=False)
        ed = m3d.modeler.oeditor
        oFR = des.GetModule("FieldsReporter")

        try:
            cs = ed.GetActiveCoordinateSystem()
        except Exception:
            cs = "?"
        print("工程 %s | 设计 %s" % (pname, dname))
        print("当前激活坐标系: %s  （脚本不切换）" % cs)
        pinfo = PLANE_INFO[plane]
        span_axis = ({"X": 0, "Y": 1, "Z": 2}[args.span_axis]
                     if args.span_axis else pinfo["axis"])
        print("剖切平面: %s（法向 %s，场计算器取 %s）"
              % (plane, pinfo["normal"], pinfo["scalar"]))
        print("选片判据: 面内 %s 跨度最大的那片" % AXIS_NAME[span_axis])

        snap_before, rd = snap_results(proj, dname)
        print("结果目录: %s" % rd)
        print("结果快照: %d 个文件" % (len(snap_before) if snap_before else -1))
        print("")

        baseline = obj_map(ed)
        print("物体总数: %d" % len(baseline))

        # ---- 预处理：改名旧剖面 / 删旧变量 ----
        if args.rename_old or args.drop_vars:
            print("-" * 70)
            for obj in objects:
                num = obj.split("_")[-1]
                varname = "I_sec_" + num

                if args.rename_old:
                    olds = sorted(n for n in baseline
                                  if n.startswith(obj + "_Section")
                                  and not n.startswith(OLD_PREFIX))
                    for o in olds:
                        new = OLD_PREFIX + o
                        ok, msg = rename(ed, o, new)
                        print("  改名 %-34s -> %-38s %s"
                              % (o, new, "OK" if ok else "失败: " + msg))

                if args.drop_vars:
                    have = [e.split("=")[0].strip() for e in exprs(oFR)]
                    if varname in have:
                        ok, msg = drop_var(des, oFR, varname)
                        print("  删变量 %-24s %s" % (varname, "OK " + msg if ok
                                                     else "失败: " + msg))
                    else:
                        print("  删变量 %-24s 不存在，无需删" % varname)
            print("")

        baseline2 = obj_map(ed)
        if not args.no_backup and not args.dry_run:
            backup(proj)

        print("=" * 74)
        ok_list, fail_list, skip_list = [], [], []
        for obj in objects:
            num = obj.split("_")[-1]
            varname = "I_sec_" + num
            print("  %-10s -> %s" % (obj, varname))
            ok, keep, msg = do_one(m3d, ed, oFR, obj, plane, varname,
                                   args.dry_run, span_axis)
            if ok is None:
                skip_list.append((obj, msg))
                print("      [跳过] %s" % msg)
            elif ok:
                ok_list.append((obj, varname, keep))
                print("      [OK]   %s" % msg)
            else:
                fail_list.append((obj, msg))
                print("      [失败] %s" % msg)
            sys.stdout.flush()

        print("=" * 74)
        final = obj_map(ed)
        added = sorted(set(final) - set(baseline2))
        bad_new = [n for n in added if model_flag(ed, n) == "true"]
        print("本次新增物体 %d: %s" % (len(added), added))
        if bad_new:
            print("!! 新增物体中有 Model=true: %s —— 拒绝保存" % bad_new)
            return 3
        print("成功 %d / 失败 %d / 跳过 %d"
              % (len(ok_list), len(fail_list), len(skip_list)))
        for o, m in fail_list:
            print("   失败 %-10s %s" % (o, m))

        # ---- 保存前护栏 ----
        snap_after, _ = snap_results(proj, dname)
        safe, msgs = diff_results(snap_before, snap_after)
        print("结果目录对比: %s" % ("安全，无文件消失/变小" if safe else "!! 异常 !!"))
        for m in msgs:
            print("   %s" % m)

        if args.dry_run:
            print("（--dry-run，未修改）")
            return 0
        if args.no_save:
            print("（--no-save，未保存）")
            return 0
        if not safe:
            print("!! 结果目录有变化，拒绝保存")
            return 3

        proj.Save()
        snap_post, _ = snap_results(proj, dname)
        safe2, msgs2 = diff_results(snap_before, snap_post)
        print("保存完成 | 保存后结果目录: %s"
              % ("完好" if safe2 else "!! 已损坏 !!"))
        for m in msgs2:
            print("   %s" % m)
        return 0 if safe2 else 3
    finally:
        disconnect(d)


if __name__ == "__main__":
    sys.exit(main())
