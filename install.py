# -*- coding: utf-8 -*-
"""
install.py —— Maxwell 后处理 GUI 一键安装器（给新电脑用）

用法（Windows）：
    双击 install.bat           ← 推荐
    或  python install.py

它会做完这些事：
    [1/6] 检查 Python 版本与 tkinter
    [2/6] 创建虚拟环境 env/
    [3/6] 升级 pip
    [4/6] 安装依赖（可选：完整 / 最小 / 跳过）
    [5/6] 写 aedt_gui_config.json，把 python 指到新建的 venv
    [6/6] 创建桌面快捷方式

装完后 GUI 的默认解释器路径 env\\Scripts\\python.exe 天然命中，不用再手动配置。
"""
import os
import subprocess
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, "env")
VPY = os.path.join(VENV, "Scripts", "python.exe")
VPYW = os.path.join(VENV, "Scripts", "pythonw.exe")
GUI = os.path.join(HERE, "aedt_gui.py")
ICO = os.path.join(HERE, "maxwell_m.ico")
CFG = os.path.join(HERE, "aedt_gui_config.json")

MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(s=""):
    print(s, flush=True)


def ask(prompt, default=""):
    try:
        v = input(prompt).strip()
    except EOFError:
        return default
    return v or default


def run(cmd, **kw):
    return subprocess.run(cmd, **kw)


def find_python():
    """挑一个 >=3.9 且带 tkinter 的解释器"""
    import shutil
    cands = []
    for exe in ("py", "python", "python3"):
        p = shutil.which(exe)
        if p:
            cands.append(p)
    if sys.executable and getattr(sys, "frozen", False) is False:
        cands.insert(0, sys.executable)
    for p in cands:
        try:
            r = subprocess.run([p, "-c",
                                "import sys,tkinter;print(sys.version_info[:2])"],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0 and r.stdout.strip():
                ver = eval(r.stdout.strip())
                if ver >= (3, 9):
                    return p, ".".join(map(str, ver))
        except Exception:
            continue
    return None, None


def main():
    log("=" * 62)
    log("  Maxwell 后处理 GUI —— 一键安装")
    log("=" * 62)
    log("")

    # ---------- 1. Python ----------
    log("[1/6] 检查 Python ...")
    py, ver = find_python()
    if not py:
        log("   !! 没找到 Python 3.9+（或没装 tkinter）。")
        log("   请先到 https://www.python.org/downloads/ 下载安装，")
        log("   安装时务必勾选 Add to PATH 和 tcl/tk and IDLE。")
        try:
            import webbrowser
            webbrowser.open("https://www.python.org/downloads/")
        except Exception:
            pass
        input("\n按回车退出...")
        return 1
    log("   OK  %s  (Python %s, tkinter 可用)" % (py, ver))

    # ---------- 2. venv ----------
    log("[2/6] 创建虚拟环境 env ...")
    if os.path.isfile(VPY):
        log("   已存在，跳过")
    else:
        r = run([py, "-m", "venv", VENV], cwd=HERE)
        if r.returncode != 0 or not os.path.isfile(VPY):
            log("   !! venv 创建失败")
            input("\n按回车退出...")
            return 1
        log("   OK  %s" % VPY)

    # ---------- 3. pip ----------
    log("[3/6] 升级 pip ...")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONIOENCODING"] = "utf-8"
    run([VPY, "-m", "pip", "install", "--upgrade", "pip", "-q",
         "-i", MIRROR], cwd=HERE, env=env)
    log("   OK")

    # ---------- 4. 依赖 ----------
    log("[4/6] 安装依赖")
    log("   1) 完整：numpy + pyaedt（AEDT 相关 tab 可用，首次约 3-5 分钟）")
    log("   2) 最小：只装 numpy（仅 matrix等效 tab 可用，不连 AEDT 也能算）")
    log("   3) 跳过")
    choice = ask("   请选择 [1]: ", "1")

    if choice == "1":
        pkgs = ["numpy", "pyaedt"]
    elif choice == "2":
        pkgs = ["numpy"]
    else:
        pkgs = []

    for mirror in (MIRROR, None):
        if not pkgs:
            break
        cmd = [VPY, "-m", "pip", "install"] + pkgs
        if mirror:
            cmd += ["-i", mirror]
        log("   $ " + " ".join(cmd))
        r = run(cmd, cwd=HERE, env=env)
        if r.returncode == 0:
            log("   OK  依赖安装完成")
            break
        log("   !! 这一次失败了%s" % ("（国内镜像），改用官方源重试..." if mirror else ""))
    else:
        log("   !! 依赖安装失败。若在公司网络后面，试试给 pip 配代理或私有源。")

    # ---------- 5. 配置 ----------
    log("[5/6] 写配置文件 aedt_gui_config.json ...")
    try:
        json.dump({"python": VPY}, open(CFG, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        log("   OK  python = %s" % VPY)
    except Exception as e:
        log("   !! 写配置失败：%s（也可在 GUI 顶部「环境设置」里手填）" % e)

    # ---------- 6. 快捷方式 ----------
    log("[6/6] 创建桌面快捷方式 ...")
    target = VPYW if os.path.isfile(VPYW) else VPY
    ps = (
        '$ws = New-Object -ComObject WScript.Shell;'
        '$s = $ws.CreateShortcut($ws.SpecialFolders("Desktop") + '
        '"\\Maxwell Post GUI.lnk");'
        '$s.TargetPath = "%s";'
        '$s.Arguments = \'\\"%s\\"\';'
        '$s.WorkingDirectory = "%s";'
        '$s.Description = "Maxwell winding post-processor";'
        '%s'
        '$s.Save()'
    ) % (target.replace("\\", "\\\\"), GUI.replace("\\", "\\\\"),
         HERE.replace("\\", "\\\\"),
         ('$s.IconLocation = "%s";' % ICO.replace("\\", "\\\\"))
         if os.path.isfile(ICO) else "")
    r = run(["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True)
    if r.returncode == 0:
        log("   OK  桌面已生成 Maxwell Post GUI")
    else:
        log("   !! 快捷方式没建成功（不影响使用）。")
        log("      手动办法：把 %s 右键发送到桌面快捷方式即可。" % GUI)

    # ---------- 完成 ----------
    log("")
    log("=" * 62)
    log("  安装完成")
    log("=" * 62)
    log("  启动方式：双击桌面 Maxwell Post GUI，或运行 run_aedt_gui.bat")
    log("  提醒：使用 AEDT 相关 tab 前，先打开 AEDT 并把目标设计设为激活。")
    log("        只用 matrix等效 的话，无需 AEDT。")
    log("")
    if ask("  现在启动 GUI？[Y/n] ", "Y").upper().startswith("Y"):
        try:
            subprocess.Popen([VPY, GUI], cwd=HERE, env=env)
        except Exception as e:
            log("启动失败：%s" % e)
    input("\n按回车退出...")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        input("\n出错了，按回车退出...")
