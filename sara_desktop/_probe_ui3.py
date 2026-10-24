import time
import uiautomation as auto
import comtypes.client

auto.SetGlobalSearchTimeout(3)

wins = [w for w in auto.GetRootControl().GetChildren() if getattr(w, "ClassName", "") == "CabinetWClass"]
win = next((w for w in wins if w.Name.startswith("HP")), wins[-1] if wins else None)
print("window:", win.Name if win else None)
if not win:
    raise SystemExit
win.SetActive(); time.sleep(0.3)

addr = win.Control(searchDepth=12, ControlType=auto.ControlType.EditControl, Name="Address Bar")
print("\nADDR TYPE:", type(addr))
methods = [m for m in dir(addr) if "alue" in m or "Pattern" in m or m in ("SetValue", "SendKeys", "Click", "SetFocus")]
print("ADDR METHODS:", methods)
for attr in ("ValuePattern", "GetValuePattern", "GetPattern", "SetValue"):
    try:
        v = getattr(addr, attr)
        print(attr, "->", v)
    except Exception as e:
        print(attr, "ERR", e)

try:
    pat = addr.GetPattern(auto.PatternId.ValuePattern)
    print("PATTERN:", pat)
    print("PATTERN VALUE:", pat.Value)
    pat.SetValue(r"C:\Users\HP")
    print("set ok")
except Exception as e:
    print("pattern err:", type(e).__name__, e)

print("\nCOM NAV TEST")
shell = comtypes.client.CreateObject("Shell.Application")
shellwin = None
for i in range(shell.Windows().Count):
    w = shell.Windows().Item(i)
    print(" win", w.HWND, w.LocationURL)
    if w.HWND == win.NativeWindowHandle:
        shellwin = w
print("matched:", bool(shellwin))
if shellwin:
    for m in ("Navigate2", "Navigate", "Refresh", "Document"):
        print(" has", m, hasattr(shellwin, m))
    try:
        shellwin.Navigate2(r"C:\Users\HP\Public")
        time.sleep(1.5)
        print("after Navigate2:", shellwin.LocationURL, shellwin.LocationName)
    except Exception as e:
        print("Navigate2 err:", type(e).__name__, e)
    try:
        items = shellwin.Document.Folder.Items()
        print("items:", items.Count, [items.Item(i).Name for i in range(min(12, items.Count))])
    except Exception as e:
        print("items err:", type(e).__name__, e)
    try:
        shellwin.Navigate2(r"C:\Users\HP")
        time.sleep(1.0)
    except Exception:
        pass

print("\nCOM FOLDER ITEMS AT HP")
try:
    items = shellwin.Document.Folder.Items()
    names = [items.Item(i).Name for i in range(items.Count)]
    print("count:", items.Count)
    print("has Public:", "Public" in names, "| has HP:", "HP" in names)
    print("last 10:", names[-10:])
except Exception as e:
    print("folder items err:", type(e).__name__, e)

print("\nUIA SCROLL TEST")
try:
    lv = win.Control(searchDepth=10, AutomationId="listview")
    inner = lv.Control(searchDepth=2, ControlType=auto.ControlType.ListControl)
    print("inner:", inner)
    sp = inner.GetScrollPattern()
    print("scroll pattern:", sp, "vert%:", sp.VerticalScrollPercent, "viewable:", sp.VerticallyScrollable)
    sp.SetScrollPercent(-1, 80)
    time.sleep(0.8)
    found = win.Control(searchDepth=14, Name="Public", ControlType=auto.ControlType.ListItemControl)
    print("Public after scroll:", found, found.BoundingRectangle)
except Exception as e:
    print("scroll err:", type(e).__name__, e)
