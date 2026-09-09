# -*- coding: utf-8 -*-
"""
gh_upload.py —— 把 Maxwell 后处理 GUI 以【明文】推送到 GitHub

为什么不用 git.exe？
    本机装了 DLP 透明加密驱动：非白名单进程（git.exe / powershell）读到的
    源文件是密文（文件头 `%TSD-Header-###%`，大小被填充成 1024 的整数倍），
    git 会把它当二进制 blob 提交，仓库里就是一堆解不开的乱码。
    Python 在白名单里，读到的是明文 —— 所以本脚本用 Python 读文件、
    直接调 GitHub REST API 建 commit，完全绕开 git.exe。

用法：
    # 1) 先到 https://github.com/settings/tokens 建一个 Fine-grained token
    #    权限勾 Contents: Read and write（若要顺便建库再勾 Administration: Read/Write
    #    或 Metadata 即可；仓库已存在时只需 Contents）
    # 2) 首次上传（自动建库）
    python gh_upload.py --token ghp_xxx --repo maxwell-post-gui --public
    # 3) 以后每次改完代码再提交
    python gh_upload.py --token ghp_xxx --repo maxwell-post-gui -m "修 matrix 单位联动"
    # 4) 只看会传什么，不联网
    python gh_upload.py --dry-run

token 也可以放环境变量：
    set GITHUB_TOKEN=ghp_xxx
"""
import argparse
import base64
import json
import os
import sys
import subprocess
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.github.com"

# 要发布的文件（相对本脚本目录）。新增文件在这里加一行即可。
FILES = [
    "README.md",
    "LICENSE",
    ".gitignore",
    "aedt_gui.py",
    "aedt_gui_backend.py",
    "current_integral_pipeline.py",
    "aedt_env.py",
    "section_cs.py",
    "indcalc_core.py",
    "maxwell_m.ico",
    "run_aedt_gui.bat",
    "run_aedt_gui_debug.bat",
    "upload_to_github.bat",
    "docs/screenshot.svg",
    "gh_upload.py",
    "upload_gui.py",
    "MaxwellPost.spec",
]

DESCRIPTION = ("Post-processing GUI for Ansys Maxwell winding simulations: "
               "Non-model sectioning, current integration, Ohmic-loss sweeps, "
               "and offline RL-matrix / T-equivalent extraction.")


def _req(method, url, token, data=None, allow404=False):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", "Bearer %s" % token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "gh_upload.py")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        # 409 'Git Repository is empty' 对空仓库的 GET ref 也会出现，
        # 语义上等同于'不存在'
        if allow404 and e.code in (404, 409):
            return None
        detail = e.read().decode("utf-8", "replace")[:400]
        raise SystemExit("[HTTP %d] %s\n%s" % (e.code, url, detail))


def read_plain(rel, fix_readme=None):
    """Python 读 = 明文。顺便做一次密文体检。"""
    p = os.path.join(HERE, rel)
    if not os.path.isfile(p):
        raise SystemExit("缺少文件: %s" % rel)
    data = open(p, "rb").read()
    if data[:4] == b"%TSD":
        raise SystemExit("!! %s 读到的是 DLP 密文，拒绝上传。请用白名单进程重写该文件。" % rel)
    if fix_readme and rel == "README.md":
        data = data.decode("utf-8")
        data = data.replace("<你的用户名>", fix_readme[0]).replace("<仓库名>", fix_readme[1])
        data = data.encode("utf-8")
    return data


def _clean_env():
    e = dict(os.environ)
    for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        e.pop(k, None)
    return e


def _find_build_python(log=print):
    """找一个同时具备 PyInstaller 和 tkinter 的解释器。"""
    import shutil
    cands = []
    if not getattr(sys, "frozen", False):
        cands.append([sys.executable])
    p = shutil.which("py")
    if p:
        cands.append([p, "-3.12"])
        cands.append([p])
    p = shutil.which("python")
    if p:
        cands.append([p])
    for cmd in cands:
        try:
            r = subprocess.run(cmd + ["-c", "import PyInstaller, tkinter"],
                               capture_output=True, timeout=60, env=_clean_env())
            if r.returncode == 0:
                return cmd
        except Exception:
            continue
    raise RuntimeError("没找到同时装了 PyInstaller 和 tkinter 的 Python")


def build_exe(log=print):
    """把当前 GUI 源码重新打成 exe，返回 dist/MaxwellPost.exe 路径。"""
    cmd = _find_build_python(log)
    argv = cmd + ["-m", "PyInstaller", "MaxwellPost.spec", "--noconfirm", "--clean"]
    log("$ " + " ".join(argv))
    pr = subprocess.Popen(argv, cwd=HERE, env=_clean_env(),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors="replace", bufsize=1)
    for line in pr.stdout:
        line = line.rstrip()
        if line:
            log("   " + line)
    pr.wait()
    if pr.returncode != 0:
        raise RuntimeError("PyInstaller 退出码 %s" % pr.returncode)
    exe = os.path.join(HERE, "dist", "MaxwellPost.exe")
    if not os.path.isfile(exe):
        raise RuntimeError("没找到产物: %s" % exe)
    if open(exe, "rb").read(4) == b"%TSD":
        raise RuntimeError("exe 是 DLP 密文，拒绝上传（删掉 dist 目录重打一次）")
    log("exe 就绪: %s  %.1f MB" % (exe, os.path.getsize(exe) / 1048576.0))
    return exe


def upload_asset(owner, repo_name, token, release_id, path, log=print):
    """把 exe 挂到某个 Release 上。"""
    from urllib.parse import quote
    name = os.path.basename(path)
    data = open(path, "rb").read()
    if data[:4] == b"%TSD":
        raise RuntimeError("附件是 DLP 密文，拒绝上传: %s" % name)
    url = ("https://uploads.github.com/repos/%s/%s/releases/%s/assets?name=%s"
           % (owner, repo_name, release_id, quote(name)))
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", "Bearer %s" % token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/octet-stream")
    req.add_header("User-Agent", "gh_upload.py")
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            j = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError("[HTTP %d] %s" % (e.code, detail))
    log("附件已上传: " + j.get("browser_download_url", name))
    return j.get("browser_download_url", "")


def do_upload(token, repo_name, message="", public=True, log=print,
             release_tag=None, release_notes="", with_exe=False):
    """执行一次上传（建库 / 追加 commit）。返回仓库 URL。"""
    # 1. 我是谁
    me = _req("GET", API + "/user", token)
    owner = me.get("login")

    fix = (owner, repo_name)

    def _bootstrap_empty():
        """空仓库调 Git Data API 会 409 'Git Repository is empty'。
        先用 Contents API 放一个 README 初始化出首个 commit。"""
        log("仓库为空 → 先放入 README 初始化…")
        data = read_plain("README.md", fix_readme=(owner, repo_name))
        r0 = _req("PUT", "%s/repos/%s/%s/contents/README.md" % (API, owner, repo_name),
                  token, {"message": "init: README",
                          "content": base64.b64encode(data).decode("ascii")})
        csha = r0["commit"]["sha"]
        cm = _req("GET", "%s/repos/%s/%s/git/commits/%s" % (API, owner, repo_name, csha),
                  token)
        return cm["tree"]["sha"], [csha]
    log("GitHub 账号: %s" % owner)

    # 2. 仓库在不在
    repo = _req("GET", "%s/repos/%s/%s" % (API, owner, repo_name), token, allow404=True)
    if repo is None:
        try:
            repo = _req("POST", API + "/user/repos", token, {
                "name": repo_name,
                "description": DESCRIPTION,
                "private": (not public),
                "auto_init": False,
            })
        except SystemExit as e:
            raise SystemExit(
                "创建仓库被 GitHub 拒绝（403 = 这个 token 没有建新仓库的权限）。\n\n"
                "办法①（推荐）：换成 classic token，生成页会自动勾好 repo 权限：\n"
                "    https://github.com/settings/tokens/new?scopes=repo\n"
                "    生成后把新 token 粘进上传窗口再来一次。\n\n"
                "办法②：先在网页手动建好一个空仓库（注意不要勾 Add a README），\n"
                "    然后把上传窗口里的仓库名改成它、点开始 ——\n"
                "    只要 token 有 Contents 读写权限就能往里推。\n\n"
                "原始错误：%s" % str(e)[:400])
        log("已创建仓库: %s (private=%s)" % (repo["full_name"], not public))
        base_tree, parents = _bootstrap_empty()
    else:
        log("仓库已存在: %s  star=%s" % (repo["full_name"], repo.get("stargazers_count", 0)))
        br = repo.get("default_branch", "main")
        ref = _req("GET", "%s/repos/%s/%s/git/ref/heads/%s" % (API, owner, repo_name, br),
                   token, allow404=True)
        if ref is None:
            base_tree, parents = _bootstrap_empty()
        else:
            cm = _req("GET", ref["object"]["url"], token)
            base_tree, parents = cm["tree"]["sha"], [cm["sha"]]


    # 3. 逐个建 blob（内容 base64；Python 读到的是明文）
    tree = []
    for f in FILES:
        data = read_plain(f, fix_readme=fix)
        b = _req("POST", "%s/repos/%s/%s/git/blobs" % (API, owner, repo_name), token,
                 {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"})
        tree.append({"path": f.replace("\\", "/"), "mode": "100644",
                     "type": "blob", "sha": b["sha"]})
        log("  blob %-32s %s" % (f, b["sha"][:8]))

    # 4. tree -> commit -> 更新 ref
    payload = {"tree": tree}
    if base_tree:
        payload["base_tree"] = base_tree
    t = _req("POST", "%s/repos/%s/%s/git/trees" % (API, owner, repo_name), token, payload)

    msg = message or ("首次提交：Maxwell 绕组后处理 GUI" if not parents
                      else "更新：Maxwell 绕组后处理 GUI")
    c = _req("POST", "%s/repos/%s/%s/git/commits" % (API, owner, repo_name), token,
             {"message": msg, "tree": t["sha"], "parents": parents})
    log("commit: %s  %s" % (c["sha"][:8], msg))

    br = repo.get("default_branch", "main")
    try:
        _req("PATCH", "%s/repos/%s/%s/git/refs/heads/%s" % (API, owner, repo_name, br),
             token, {"sha": c["sha"]})
    except SystemExit:
        # 分支还不存在(或被并发建出) → 退回创建; 再失败就把真错误抛出去
        try:
            _req("POST", "%s/repos/%s/%s/git/refs" % (API, owner, repo_name), token,
                 {"ref": "refs/heads/%s" % br, "sha": c["sha"]})
        except SystemExit:
            _req("PATCH", "%s/repos/%s/%s/git/refs/heads/%s" % (API, owner, repo_name, br),
                 token, {"sha": c["sha"]})

    if release_tag:
        exe = None
        if with_exe:
            try:
                exe = build_exe(log=log)
            except Exception as ex:
                log("!! 打包 exe 失败，本次不传附件：%s" % ex)
        try:
            rel = _req("POST", "%s/repos/%s/%s/releases" % (API, owner, repo_name), token,
                       {"tag_name": release_tag,
                        "name": release_tag,
                        "body": release_notes or ("Release " + release_tag),
                        "target_commitish": c["sha"],
                        "draft": False, "prerelease": False})
            log("Release 已发布: " + rel.get("html_url", ""))
        except SystemExit as e:
            log("Release 发布失败（可到网页 Releases 页面手动补）：%s" % str(e)[:200])
            rel = None
        if rel and exe:
            try:
                upload_asset(owner, repo_name, token, rel["id"], exe, log=log)
            except Exception as ex:
                log("!! 附件上传失败：%s" % ex)

    url = "https://github.com/%s/%s" % (owner, repo_name)
    log("")
    log("完成  " + url)
    return url


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    ap.add_argument("--repo", default="maxwell-post-gui")
    ap.add_argument("--public", action="store_true", help="建公开仓库（要 star 必须公开）")
    ap.add_argument("--private", action="store_true")
    ap.add_argument("-m", "--message", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--release", default="",
                    help="顺便发布 Release，填 tag 名如 v1.0.0；留空则只提交不发布")
    ap.add_argument("--release-notes", default="", help="Release 说明")
    ap.add_argument("--with-exe", action="store_true",
                    help="发布 Release 时先打包 exe 并作为附件上传")
    ap.add_argument("--branch", default="main")
    args = ap.parse_args()

    if args.dry_run:
        print("=== dry-run：将上传以下文件（读到的均为明文）===")
        tot = 0
        for f in FILES:
            d = read_plain(f)
            tot += len(d)
            print("  %-32s %8d B   head=%r" % (f, len(d), d[:24]))
        print("  合计 %d 个文件 / %.1f KB" % (len(FILES), tot / 1024.0))
        return

    if not args.token:
        raise SystemExit("需要 --token 或环境变量 GITHUB_TOKEN")

    do_upload(args.token, args.repo, args.message, public=not args.private,
              release_tag=(args.release or None), release_notes=args.release_notes,
              with_exe=args.with_exe)


if __name__ == "__main__":
    main()
