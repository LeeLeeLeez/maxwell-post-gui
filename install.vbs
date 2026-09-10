' Maxwell 后处理 GUI —— 一键安装（双击入口）
' 说明：部分公司电脑禁止双击执行 .bat，这个 .vbs 走 wscript 启动，
'       若也不行，就手动在终端里跑：python install.py
Option Explicit

Dim sh, fso, here, cmd
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here

cmd = "cmd /k chcp 65001 >nul & " & _
      "(where python >nul 2>nul && python install.py " & _
      "|| (where py >nul 2>nul && py -3 install.py " & _
      "|| echo [ERROR] Python not found - install from https://www.python.org/downloads/))"

sh.Run cmd, 1, False
