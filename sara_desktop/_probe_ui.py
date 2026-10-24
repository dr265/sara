import os, time, subprocess, json
import uiautomation as auto

subprocess.Popen(["explorer.exe", r"C:\Users\HP"])
time.sleep(3)

print("=== TOP WINDOWS ===")
root = auto.GetRootControl()
for w in root.GetChildren():
    try:
        if w.ControlTypeName in ("WindowControl", "PaneControl"):
            print(repr(w.Name), "|", w.ControlTypeName, "|", w.ClassName, "|", w.BoundingRectangle)
    except Exception as e:
        pass

print("\n=== FOREGROUND CONTROL ===")
fg = auto.GetForegroundControl()
print(repr(fg.Name), fg.ControlTypeName, fg.ClassName)

# Find Explorer window by class or name
win = None
for w in root.GetChildren():
    try:
        if w.ClassName == "CabinetWClass" or "File Explorer" in (w.Name or ""):
            win = w
            break
    except Exception:
        pass
print("\n=== EXPLORER WINDOW ===", win)
if win:
    win.SetActive()
    time.sleep(0.5)

    def dump(ctrl, depth=0, maxdepth=4):
        if depth > maxdepth:
            return
        for c in ctrl.GetChildren():
            try:
                name = c.Name or ""
                if name or c.ControlTypeName in ("EditControl", "ButtonControl", "ListItemControl", "TreeItemControl"):
                    print("  " * depth + f"{c.ControlTypeName} name={name!r} auto={c.AutomationId!r} class={c.ClassName!r} rect={c.BoundingRectangle}")
            except Exception:
                continue
            dump(c, depth + 1, maxdepth)

    dump(win, 0, 4)

print("\n=== COM EXPLORER WINDOWS ===")
try:
    import comtypes.client
    shell = comtypes.client.CreateObject("Shell.Application")
    for i in range(shell.Windows().Count):
        try:
            w = shell.Windows().Item(i)
            print("HWND", w.HWND, "| location:", w.LocationURL, "| name:", w.LocationName)
            try:
                print("   folder path:", w.Document.Folder.Self.Path)
                sel = w.Document.SelectedItems()
                print("   selected:", [sel.Item(j).Name for j in range(sel.Count)])
            except Exception as e:
                print("   detail err:", e)
        except Exception as e:
            print("win err:", e)
except Exception as e:
    print("COM ERR:", type(e).__name__, e)
