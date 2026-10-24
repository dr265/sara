import time, urllib.parse
import uiautomation as auto

auto.SetGlobalSearchTimeout(5)

def explorer_windows():
    out = []
    for w in auto.GetRootControl().GetChildren():
        try:
            if w.ClassName == "CabinetWClass":
                out.append(w)
        except Exception:
            pass
    return out

def addr(win):
    try:
        c = win.Control(searchDepth=12, ControlType=auto.ControlType.EditControl, Name="Address Bar")
        return c
    except Exception:
        return None

def val(ctrl):
    try:
        return ctrl.GetValuePattern().Value
    except Exception as e:
        return f"<no value: {e}>"

wins = explorer_windows()
print("windows:", [(w.Name, hex(w.NativeWindowHandle)) for w in wins])
win = None
for w in wins:
    if w.Name.startswith("HP"):
        win = w
if win is None and wins:
    win = wins[-1]
print("chosen:", win.Name if win else None)
if not win:
    raise SystemExit

win.SetActive()
time.sleep(0.4)
a = addr(win)
print("address control:", a)
print("address value:", val(a) if a else None)

# Find the file list container and dump items deeper
try:
    lv = win.Control(searchDepth=10, AutomationId="listview")
    print("listview:", lv)
    def dump(ctrl, depth=0, maxdepth=3):
        if depth > maxdepth: return
        for c in ctrl.GetChildren():
            try:
                print("  "*depth + f"{c.ControlTypeName} name={c.Name!r} auto={c.AutomationId!r} class={c.ClassName!r} rect={c.BoundingRectangle}")
            except Exception:
                continue
            dump(c, depth+1, maxdepth)
    dump(lv, 0, 3)
except Exception as e:
    print("listview err:", e)

# Try clicking an item named Public by UIA search
print("\n--- find Public ---")
try:
    item = win.Control(searchDepth=12, Name="Public", ControlType=auto.ControlType.ListItemControl)
    print("found item:", item, item.BoundingRectangle)
    try:
        item.GetSelectionItemPattern().Select()
        print("selected via SelectionItem")
    except Exception as e:
        print("select err:", e)
        try:
            item.Click(simulateMove=False)
            print("clicked fallback")
        except Exception as e2:
            print("click err:", e2)
    time.sleep(0.3)
    # double click to open
    try:
        r = item.BoundingRectangle
        auto.Click(r.xcenter(), r.ycenter(), waitTime=0.2)
        auto.Click(r.xcenter(), r.ycenter(), waitTime=0.2)
        print("double clicked Public")
    except Exception as e:
        print("dblclick err:", e)
    time.sleep(1.5)
    print("after open address:", val(addr(win)))
except Exception as e:
    print("Public not found:", e)

# Navigate via address bar
print("\n--- navigate via address bar ---")
a = addr(win)
if a:
    a.SetFocus()
    time.sleep(0.2)
    try:
        a.GetValuePattern().SetValue(r"C:\Users\HP")
    except Exception as e:
        print("setvalue err:", e)
    time.sleep(0.3)
    auto.SendKeys("{Enter}", waitTime=0.2)
    time.sleep(1.5)
    print("address now:", val(a))
    print("window name:", win.Name)

print("\n--- COM ---")
try:
    import comtypes.client
    shell = comtypes.client.CreateObject("Shell.Application")
    for i in range(shell.Windows().Count):
        w = shell.Windows().Item(i)
        try:
            print("HWND", w.HWND, "URL", w.LocationURL, "name", w.LocationName)
        except Exception as e:
            print("err", e)
        try:
            sel = w.Document.SelectedItems()
            print("  selected:", [sel.Item(j).Name for j in range(sel.Count)])
        except Exception as e:
            print("  sel err:", type(e).__name__, e)
except Exception as e:
    print("COM ERR:", e)
