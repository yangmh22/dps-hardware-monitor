import sys

print("exe", sys.executable)
try:
    import monitor  # type: ignore
    print("monitor_import_ok", getattr(monitor, "__file__", None))
except Exception as e:
    print("monitor_import_fail", repr(e))

