# autostart.py
# "Start with Windows": the scheduled task JanomeJP4Panel (see deploy/install_autostart.ps1) managed from the
# panel's Settings. Two kinds of task:
#   Logon    starts when the user logs in; created without administrator rights.
#   Startup  starts at boot as SYSTEM, before anyone logs in; needs administrator rights. The panel cannot
#            give itself those, so it opens the normal Windows permission (UAC) window on the PC's screen and the
#            person at the PC approves it. Nothing is bypassed.
# English comments only

import os
import subprocess
import sys

TASK_NAME = "JanomeJP4Panel"
MODES = ("Logon", "Startup")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def supported():
    return sys.platform == "win32"


def is_admin():
    if not supported():
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def find_script(base_dir, name):
    """The install/uninstall script: in deploy/ of the source tree, or next to the exe of the packaged program."""
    for folder in (base_dir, os.path.join(base_dir, "deploy")):
        path = os.path.join(folder, name)
        if os.path.exists(path):
            return path
    return None


def _powershell(args, run=subprocess.run, timeout=60):
    return run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"] + args,
               capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)


def status(run=subprocess.run):
    """{'supported', 'installed', 'mode', 'state', 'is_admin'} - reading the task needs no administrator rights."""
    info = {"supported": supported(), "installed": False, "mode": None, "state": None, "is_admin": is_admin()}
    if not info["supported"]:
        return info
    command = (f"$t = Get-ScheduledTask -TaskName {TASK_NAME} -ErrorAction SilentlyContinue; "
               "if ($t) { $t.Principal.UserId + '|' + $t.State }")
    try:
        out = _powershell(["-Command", command], run=run, timeout=20).stdout.strip()
    except Exception:
        return info
    if "|" in out:
        user, state = out.splitlines()[-1].split("|", 1)
        info.update(installed=True, state=state.strip(),
                    mode="Startup" if user.strip().upper().split("\\")[-1] == "SYSTEM" else "Logon")
    return info


def _elevated(script, script_args, run):
    """Runs a script through the Windows permission (UAC) window; returns at once, the person approves on the PC."""
    items = ",".join(f"'{a}'" for a in ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", f'"{script}"'] + script_args)
    return _powershell(["-Command", f"Start-Process powershell -Verb RunAs -ArgumentList {items}"], run=run, timeout=30)


def enable(mode, base_dir, run=subprocess.run):
    """
    Creates the task. Returns {'ok', 'message', 'pending'}: pending=True means the permission window is open
    on the PC and the result will show in status() once it is approved.
    """
    if mode not in MODES:
        return {"ok": False, "message": f"mode must be one of {MODES}", "pending": False}
    if not supported():
        return {"ok": False, "message": "Start with Windows is only available on Windows.", "pending": False}
    script = find_script(base_dir, "install_autostart.ps1")
    if not script:
        return {"ok": False, "message": "install_autostart.ps1 was not found next to the program.", "pending": False}
    if mode == "Startup" and not is_admin():
        result = _elevated(script, ["-Mode", "Startup"], run)
        if result.returncode != 0:
            return {"ok": False, "message": "The administrator permission was not given.", "pending": False}
        return {"ok": True, "pending": True,
                "message": "Approve the Windows permission window on the PC's screen. It was opened there."}
    result = _powershell(["-File", script, "-Mode", mode], run=run)
    if result.returncode != 0:
        return {"ok": False, "pending": False, "message": (result.stderr or result.stdout).strip()[:300] or "Failed."}
    return {"ok": True, "pending": False, "message": f"Done: the panel will start {'when the computer starts' if mode == 'Startup' else 'when you log in'}."}


def disable(base_dir, run=subprocess.run):
    """Removes the task (the running panel keeps running). A boot-time task belongs to SYSTEM: asks permission."""
    if not supported():
        return {"ok": False, "message": "Start with Windows is only available on Windows.", "pending": False}
    script = find_script(base_dir, "uninstall_autostart.ps1")
    if not script:
        return {"ok": False, "message": "uninstall_autostart.ps1 was not found next to the program.", "pending": False}
    result = _powershell(["-File", script, "-KeepRunning"], run=run)
    if result.returncode == 0:
        return {"ok": True, "pending": False, "message": "Turned off."}
    if not is_admin():
        result = _elevated(script, ["-KeepRunning"], run)
        if result.returncode == 0:
            return {"ok": True, "pending": True,
                    "message": "Approve the Windows permission window on the PC's screen. It was opened there."}
    return {"ok": False, "pending": False, "message": (result.stderr or result.stdout).strip()[:300] or "Failed."}
