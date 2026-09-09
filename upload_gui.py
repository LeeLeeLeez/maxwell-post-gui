# -*- coding: utf-8 -*-
"""
upload_gui.py —— 图形界面上传器（双击即可，不用敲命令 / 不用 bat）

用法：
    双击本文件；或双击桌面快捷方式「上传 GitHub」。
    填 Token → 点【开始上传】。

Token 获取：https://github.com/settings/tokens
            Generate new token (classic) → 勾 repo
"""
import os
import queue
import sys
import threading
import traceback
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import gh_upload  # noqa: E402

TOKEN_FILE = os.path.join(HERE, ".gh_token")
ACCENT = "#2563EB"


class Uploader(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("上传 Maxwell 后处理 GUI → GitHub")
        self.geometry("660x560")
        self.minsize(560, 460)
        self.configure(bg="#F5F7FA")
        try:
            ico = os.path.join(HERE, "maxwell_m.ico")
            if os.path.isfile(ico):
                self.iconbitmap(ico)
        except Exception:
            pass

        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("Accent.TButton", background=ACCENT, foreground="#FFFFFF",
                     borderwidth=0, padding=6)
        st.map("Accent.TButton", background=[("active", "#1D4ED8")])

        self.q = queue.Queue()
        self.v_tok = tk.StringVar()
        self.v_repo = tk.StringVar(value="maxwell-post-gui")
        self.v_msg = tk.StringVar()
        self.v_pub = tk.IntVar(value=1)
        self.v_remember = tk.IntVar(value=0)

        if os.path.isfile(TOKEN_FILE):
            try:
                saved = open(TOKEN_FILE, encoding="utf-8").read().strip()
                if saved:
                    self.v_tok.set(saved)
                    self.v_remember.set(1)
            except Exception:
                pass

        pad = {"padx": 12, "pady": 6}
        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")

        ttk.Label(top, text="GitHub Token",
                  font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        e_tok = ttk.Entry(top, textvariable=self.v_tok, show="•", width=52)
        e_tok.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(8, 0))
        ttk.Button(top, text="去获取", width=8,
                   command=lambda: webbrowser.open(
                       "https://github.com/settings/tokens")).grid(row=0, column=3, padx=(6, 0))

        ttk.Label(top, text="仓库名").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(top, textvariable=self.v_repo, width=30).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Label(top, text="提交说明（可留空）").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(top, textvariable=self.v_msg, width=52).grid(
            row=2, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(8, 0))

        row3 = ttk.Frame(top)
        row3.grid(row=3, column=1, columnspan=3, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Radiobutton(row3, text="公开（才能收 star）", variable=self.v_pub,
                        value=1).pack(side="left")
        ttk.Radiobutton(row3, text="私有", variable=self.v_pub,
                        value=0).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(row3, text="记住 Token（存到本地 .gh_token）",
                        variable=self.v_remember).pack(side="left", padx=(18, 0))
        top.columnconfigure(1, weight=1)

        bar = ttk.Frame(self, padding=(12, 0))
        bar.pack(fill="x")
        self.btn = ttk.Button(bar, text="开始上传", style="Accent.TButton",
                              command=self.start)
        self.btn.pack(side="left")
        ttk.Button(bar, text="先体检（不联网）",
                   command=self.check).pack(side="left", padx=(8, 0))
        self.status = ttk.Label(bar, text="待上传 %d 个文件" % len(gh_upload.FILES),
                                foreground="#64748B")
        self.status.pack(side="left", padx=(14, 0))

        self.txt = scrolledtext.ScrolledText(self, wrap="word", height=18,
                                             font=("Consolas", 10))
        self.txt.pack(fill="both", expand=True, padx=12, pady=10)
        self.txt.insert("end", "填好 Token 后点【开始上传】。\n"
                               "首次运行会自动建库，之后每次都是一个新 commit。\n\n")
        self.txt.configure(state="disabled")

        self.after(100, self._pump)

    # ---------- 日志 ----------
    def _log(self, s=""):
        self.q.put(("log", str(s)))

    def _pump(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.txt.configure(state="normal")
                    self.txt.insert("end", data + "\n")
                    self.txt.see("end")
                    self.txt.configure(state="disabled")
                elif kind == "done":
                    self.btn.configure(state="normal")
                    self.status.configure(text="完成", foreground="#16A34A")
                    messagebox.showinfo("上传完成", data)
                elif kind == "err":
                    self.btn.configure(state="normal")
                    self.status.configure(text="失败", foreground="#DC2626")
                    self._log("\n" + data)
                    messagebox.showerror("上传失败", data[:600])
        except queue.Empty:
            pass
        self.after(120, self._pump)

    # ---------- 动作 ----------
    def check(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert("end", "=== 本地文件体检 ===\n")
        tot = 0
        ok = True
        for f in gh_upload.FILES:
            try:
                d = gh_upload.read_plain(f)
                tot += len(d)
                self.txt.insert("end", "  OK   %-30s %8d B\n" % (f, len(d)))
            except SystemExit as e:
                ok = False
                self.txt.insert("end", "  FAIL %-30s %s\n" % (f, str(e)[:80]))
        self.txt.insert("end", "\n合计 %d 个文件 / %.1f KB   ——  %s\n"
                        % (len(gh_upload.FILES), tot / 1024.0,
                           "全部明文，可以上传" if ok else "有密文，拒绝上传"))
        self.txt.configure(state="disabled")

    def start(self):
        token = self.v_tok.get().strip()
        repo = self.v_repo.get().strip() or "maxwell-post-gui"
        msg = self.v_msg.get().strip()
        public = (self.v_pub.get() == 1)
        if not token:
            messagebox.showwarning("缺 Token", "先填 GitHub Token（勾 repo 权限那个）。")
            return
        if self.v_remember.get():
            try:
                open(TOKEN_FILE, "w", encoding="utf-8").write(token)
            except Exception:
                pass
        elif os.path.isfile(TOKEN_FILE):
            try:
                os.remove(TOKEN_FILE)
            except Exception:
                pass

        self.btn.configure(state="disabled")
        self.status.configure(text="上传中…", foreground=ACCENT)
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

        def work():
            try:
                url = gh_upload.do_upload(token, repo, msg, public=public, log=self._log)
                self.q.put(("done", url))
            except SystemExit as e:
                self.q.put(("err", str(e)))
            except Exception:
                self.q.put(("err", traceback.format_exc()))

        threading.Thread(target=work, daemon=True).start()


def main():
    Uploader().mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        err = traceback.format_exc()
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("启动失败", err[:900])
        except Exception:
            print(err)
