## Existing files

### src/calc/ops.py
```python
def add(a: int, b: int) -> int:
    return a + b


def sub(a: int, b: int) -> int:
    return a - b
```

### src/calc/__init__.py
```python
from .ops import add, sub

__all__ = ["add", "sub"]
```

## Diff

### src/calc/ops.py
```diff
diff --git a/src/calc/ops.py b/src/calc/ops.py
--- a/src/calc/ops.py
+++ b/src/calc/ops.py
@@ -1,5 +1,5 @@
 def add(a: int, b: int) -> int:
-    return a + b
+    return a - b


 def sub(a: int, b: int) -> int:
     return a - b
```

### src/calc/__init__.py
```diff
diff --git a/src/calc/__init__.py b/src/calc/__init__.py
--- a/src/calc/__init__.py
+++ b/src/calc/__init__.py
@@ -1,3 +1,4 @@
 from .ops import add, sub

-__all__ = ["add", "sub"]
+__all__ = ["add", "sub", "mul"]
+# NOTE: `mul` not yet implemented
```

## Task

Review the diff in `src/calc/ops.py` for correctness. Output one of:

- `VERDICT: ok` if the change is intentional and correct
- `VERDICT: bug — <one-line reason>` if the change introduces a defect

Do not read the listed files via shell or filesystem tools; the bundle
above is authoritative.
