"""
Optional first-launch prompt (Windows only) offering to create a Desktop
icon and/or Start Menu entry for the packaged .exe, so someone who received
a prebuilt copy of the app (see the RUN/ hand-off folder / README's
"Building a standalone executable") doesn't have to keep navigating back to
that folder -- they can just click an icon like any other installed
program, the same way start.bat already lets them.

Only ever offered once (see settings.py's shortcut_prompt_shown flag) and
only when running as the frozen PyInstaller build -- asking this every time
a developer runs `python run.py` from source would be pointless (there's no
stable .exe path to point a shortcut at) and annoying. Desktop/Start Menu
shortcuts are a Windows Explorer concept specifically (macOS's .app bundle
is already a double-clickable icon with no extra step needed; Linux desktop
environments vary too much to target generically), so this is a no-op on
any other platform.
"""

import os
import subprocess
import sys

from . import settings

SHORTCUT_NAME = "Politician Trades Tracker.lnk"

_POWERSHELL_TIMEOUT_SECONDS = 10

# The prompt dialog itself has no fixed timeout -- it's fine for it to sit
# there until a human answers it (see _prompt_dialog_script's ShowDialog()).


def _run_powershell(script, timeout=_POWERSHELL_TIMEOUT_SECONDS):
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=timeout,
    )


def _shortcut_target_dirs():
    """Returns (desktop_dir, start_menu_programs_dir) for the CURRENT user,
    via PowerShell's [Environment]::GetFolderPath -- correct even when
    Desktop/Start Menu have been redirected (OneDrive Known Folder Move, a
    roaming profile, etc.), which a hardcoded '%USERPROFILE%\\Desktop'
    guess would get wrong."""
    result = _run_powershell(
        "[Environment]::GetFolderPath('Desktop'); [Environment]::GetFolderPath('Programs')"
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 2:
        raise RuntimeError(f"unexpected GetFolderPath output: {result.stdout!r} {result.stderr!r}")
    return lines[0], lines[1]


def _escape_ps_literal(s):
    """Escapes a string for safe interpolation inside a PowerShell
    single-quoted literal -- the only special case is an embedded single
    quote itself, doubled to escape it (PowerShell's equivalent of \\')."""
    return s.replace("'", "''")


def _create_shortcut(target_dir, exe_path):
    """Creates one .lnk in target_dir pointing at exe_path, launching the
    app exactly the same way double-clicking the exe itself would (same
    working directory, so the app's data/ folder still lands next to the
    exe rather than wherever the shortcut happens to be invoked from).
    Returns True on success; never raises."""
    exe_dir = os.path.dirname(exe_path)
    lnk_path = os.path.join(target_dir, SHORTCUT_NAME)
    script = (
        "$WshShell = New-Object -ComObject WScript.Shell\n"
        f"$Shortcut = $WshShell.CreateShortcut('{_escape_ps_literal(lnk_path)}')\n"
        f"$Shortcut.TargetPath = '{_escape_ps_literal(exe_path)}'\n"
        f"$Shortcut.WorkingDirectory = '{_escape_ps_literal(exe_dir)}'\n"
        f"$Shortcut.IconLocation = '{_escape_ps_literal(exe_path)}'\n"
        "$Shortcut.Description = 'Politician Trades Tracker'\n"
        "$Shortcut.Save()\n"
    )
    proc = _run_powershell(script)
    return proc.returncode == 0


# Small WinForms dialog: a label plus one checkbox per shortcut location,
# both pre-checked (saying yes to both is the common case), OK/Cancel.
# Prints "DESKTOP=True/False" and "STARTMENU=True/False" to stdout so the
# Python side can read back which boxes were checked when OK was clicked
# (Cancel/closing the window is reported as both False, same as unchecking
# everything). Built with WinForms via a PowerShell subprocess rather than
# bundling Tkinter into the frozen build, reusing the same "shell out to
# PowerShell for Windows-native UI" approach _create_shortcut already uses.
_PROMPT_DIALOG_SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$form = New-Object System.Windows.Forms.Form
$form.Text = 'Politician Trades Tracker'
$form.Size = New-Object System.Drawing.Size(300, 190)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.TopMost = $true

$label = New-Object System.Windows.Forms.Label
$label.Text = 'Would you like to add a shortcut to:'
$label.Location = New-Object System.Drawing.Point(15, 15)
$label.Size = New-Object System.Drawing.Size(260, 20)
$form.Controls.Add($label)

$cbDesktop = New-Object System.Windows.Forms.CheckBox
$cbDesktop.Text = 'Desktop'
$cbDesktop.Location = New-Object System.Drawing.Point(30, 45)
$cbDesktop.Size = New-Object System.Drawing.Size(200, 20)
$cbDesktop.Checked = $true
$form.Controls.Add($cbDesktop)

$cbStartMenu = New-Object System.Windows.Forms.CheckBox
$cbStartMenu.Text = 'Start Menu'
$cbStartMenu.Location = New-Object System.Drawing.Point(30, 70)
$cbStartMenu.Size = New-Object System.Drawing.Size(200, 20)
$cbStartMenu.Checked = $true
$form.Controls.Add($cbStartMenu)

$okButton = New-Object System.Windows.Forms.Button
$okButton.Text = 'OK'
$okButton.Location = New-Object System.Drawing.Point(110, 110)
$okButton.DialogResult = [System.Windows.Forms.DialogResult]::OK
$form.Controls.Add($okButton)
$form.AcceptButton = $okButton

$cancelButton = New-Object System.Windows.Forms.Button
$cancelButton.Text = 'Cancel'
$cancelButton.Location = New-Object System.Drawing.Point(195, 110)
$cancelButton.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$form.Controls.Add($cancelButton)
$form.CancelButton = $cancelButton

$result = $form.ShowDialog()

if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
    Write-Output "DESKTOP=$($cbDesktop.Checked)"
    Write-Output "STARTMENU=$($cbStartMenu.Checked)"
} else {
    Write-Output "DESKTOP=False"
    Write-Output "STARTMENU=False"
}
"""


def _prompt_dialog():
    """Shows the checkbox dialog and returns (want_desktop, want_start_menu)
    booleans. No timeout -- a human may take a while to answer, and there's
    nothing useful to do while waiting anyway."""
    proc = _run_powershell(_PROMPT_DIALOG_SCRIPT, timeout=None)
    want_desktop = "DESKTOP=True" in proc.stdout
    want_start_menu = "STARTMENU=True" in proc.stdout
    return want_desktop, want_start_menu


def maybe_prompt_for_shortcuts():
    """Shows a one-time checkbox prompt offering Desktop and/or Start Menu
    shortcuts, if all of these hold:
      - running on Windows (this whole feature is a no-op elsewhere)
      - running as the frozen/packaged .exe (not `python run.py` from source)
      - the user hasn't already been asked (see settings.shortcut_prompt_shown)
    Never raises -- a shortcut-creation hiccup should never block the app
    from starting normally, so every failure mode here just quietly no-ops
    rather than surfacing an error."""
    try:
        if sys.platform != "win32" or not getattr(sys, "frozen", False):
            return
        if settings.load_settings().get("shortcut_prompt_shown"):
            return

        exe_path = sys.executable
        want_desktop, want_start_menu = _prompt_dialog()
        if want_desktop or want_start_menu:
            desktop_dir, start_menu_dir = _shortcut_target_dirs()
            if want_desktop:
                _create_shortcut(desktop_dir, exe_path)
            if want_start_menu:
                _create_shortcut(start_menu_dir, exe_path)
        settings.save_settings({"shortcut_prompt_shown": True})
    except Exception:
        pass
