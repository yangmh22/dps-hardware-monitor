import sys

print("exe", sys.executable)
try:
    import psutil  # type: ignore
    print("psutil_ok", getattr(psutil, "__version__", "unknown"))
except Exception as e:
    print("psutil_fail", repr(e))

try:
    import rich  # type: ignore
    print("rich_ok", getattr(rich, "__version__", "unknown"))
except Exception as e:
    print("rich_fail", repr(e))

