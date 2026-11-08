# Technical Debt & Performance Ledger: datarescue

Crosschecked 2026-11-08 WIB. All code diffs below are proposal-only and have not changed production source. Source references and removed patch lines were checked against this working tree. Runtime speedups remain unmeasured unless evidence is explicitly cited.

Reviewed on 2026-11-08 WIB against the current working tree on disk. All line numbers are 1-based and verified directly against the current production files. Every diff below is a non-destructive engineering proposal, not an applied production edit.

## TD-01 — Serialize `active_schema.model_dump()` and `active_options.model_dump()` once per `scan()`

- **Location**: `datarescue/core.py:492-497`
- **Analysis**: `scan()` calls `_compute_plan_id(digest, active_schema, active_options)` (which runs `schema.model_dump()` and `options.model_dump()`) and immediately calls `active_schema.model_dump()` and `active_options.model_dump()` a second time at `core.py:537-538` when building the returned `ScanResult`.

```diff
diff --git a/datarescue/core.py b/datarescue/core.py
--- a/datarescue/core.py
+++ b/datarescue/core.py
@@ -489,9 +489,9 @@
     }
 
 
-def _compute_plan_id(source_sha256: str, schema: Schema, options: Options) -> str:
+def _compute_plan_id(source_sha256: str, schema_dict: dict[str, Any], options_dict: dict[str, Any]) -> str:
     canonical_plan = json.dumps(
-        {"source": source_sha256, "schema": schema.model_dump(), "options": options.model_dump()},
+        {"source": source_sha256, "schema": schema_dict, "options": options_dict},
         sort_keys=True,
     )
     return hashlib.sha256(canonical_plan.encode("utf-8")).hexdigest()
@@ -522,7 +522,9 @@
         for row_number, original in enumerate(originals, 1)
     ]
     digest = hashlib.sha256(data).hexdigest()
-    plan_id = _compute_plan_id(digest, active_schema, active_options)
+    schema_dump = active_schema.model_dump()
+    options_dump = active_options.model_dump()
+    plan_id = _compute_plan_id(digest, schema_dump, options_dump)
     safe_basename = filename.replace("\\", "/").split("/")[-1]
     return {
         "filename": safe_basename,
@@ -534,8 +536,8 @@
         "columns": columns,
         "source_columns": source_columns,
         "header_changes": header_changes,
-        "schema": active_schema.model_dump(),
-        "options": active_options.model_dump(),
+        "schema": schema_dump,
+        "options": options_dump,
         "drift": drift,
         "records": records,
         "summary": _summarize_records(records),
```

- **Validation**: Compute `schema_dump = active_schema.model_dump()` and `options_dump = active_options.model_dump()` once in `scan()`, pass both into `_compute_plan_id`, and run `.\.venv\Scripts\python.exe -m pytest -q`.

## TD-02 — Cache immutable bundled example payloads or return defensive copies in `/api/examples/{example_id}`

- **Location**: `datarescue/api.py:150-161`
- **Analysis**: Bundled example files (`orders.csv`, `inventory.json`, `dates.csv` and their schemas) are static package assets, yet every `GET /api/examples/{example_id}` performs two synchronous filesystem opens/reads.

```diff
diff --git a/datarescue/api.py b/datarescue/api.py
--- a/datarescue/api.py
+++ b/datarescue/api.py
@@ -3,6 +3,7 @@
 from __future__ import annotations
 
 import asyncio
+from functools import lru_cache
 import json
 import logging
 from collections.abc import Awaitable, Callable, MutableMapping
@@ -147,18 +148,21 @@
     ]
 
 
+@lru_cache(maxsize=len(ALLOWED_EXAMPLES))
+def _read_bundled_example(example_id: str, filename: str) -> tuple[str, str]:
+    data_path = ROOT / "examples" / filename
+    schema_path = ROOT / "examples" / f"{example_id}.schema.json"
+    return data_path.read_text(encoding="utf-8"), schema_path.read_text(encoding="utf-8")
+
+
 @app.get("/api/examples/{example_id}")
 def example(example_id: str) -> ExamplePayload:
     filename = ALLOWED_EXAMPLES.get(example_id)
     if filename is None:
         raise HTTPException(404, "Example not found.")
-    data_path = ROOT / "examples" / filename
-    schema_path = ROOT / "examples" / f"{example_id}.schema.json"
     try:
-        with data_path.open("r", encoding="utf-8") as data_handle:
-            content = data_handle.read()
-        with schema_path.open("r", encoding="utf-8") as schema_handle:
-            schema_payload: dict[str, Any] = json.load(schema_handle)
+        content, raw_schema = _read_bundled_example(example_id, filename)
+        schema_payload: dict[str, Any] = json.loads(raw_schema)
     except (OSError, json.JSONDecodeError) as exc:
         logger.error("Failed to read bundled example %r: %s", example_id, exc)
         raise HTTPException(500, "Bundled example could not be loaded.") from exc
```

- **Validation**: Clear `_read_bundled_example.cache_clear()` after editing bundled assets in a long-running development process; verify mutated response dicts cannot pollute the cache (`json.loads(raw_schema)` creates a fresh dict), and run `.\.venv\Scripts\python.exe -m pytest -q`.
