## 2025-02-14 - Optimized _strings recursive flattening
**Learning:** List comprehensions calling themselves recursively to unpack deeply nested JSON tree cause a lot of overhead creating intermediate lists in Python. By using a closure appending to a single result list in the outer scope, we gain a ~2x performance speedup.
**Action:** Favor closure-based tree traversals over recursive list comprehensions for complex flattening of deeply nested trees.
