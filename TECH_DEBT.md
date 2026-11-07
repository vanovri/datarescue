# Technical Debt & Performance Ledger: datarescue

Reviewed on 2026-11-07 against the current working tree on disk. All line numbers are 1-based and verified directly against the current production files. Every diff below is a non-destructive engineering proposal, not an applied production edit.

## TD-01 — Serialize `active_schema.model_dump()` and `active_options.model_dump()` once per `scan()`

- **Location**: `datarescue/core.py:492-497`
- **Analysis**: `scan()` calls `_compute_plan_id(digest, active_schema, active_options)` (which runs `schema.model_dump()` and `options.model_dump()`) and immediately calls `active_schema.model_dump()` and `active_options.model_dump()` a second time at `core.py:537-538` when building the returned `ScanResult`.

```diff
--- a/datarescue/core.py
+++ b/datarescue/core.py
@@ -489,9 +489,13 @@
     }
 
 
-def _compute_plan_id(source_sha256: str, schema: Schema, options: Options) -> str:
+def _compute_plan_id(
+    source_sha256: str,
+    schema_dict: dict[str, Any],
+    options_dict: dict[str, Any],
+) -> str:
     canonical_plan = json.dumps(
-        {"source": source_sha256, "schema": schema.model_dump(), "options": options.model_dump()},
+        {"source": source_sha256, "schema": schema_dict, "options": options_dict},
         sort_keys=True,
     )
     return hashlib.sha256(canonical_plan.encode("utf-8")).hexdigest()
```

- **Validation**: Compute `schema_dump = active_schema.model_dump()` and `options_dump = active_options.model_dump()` once in `scan()`, pass both into `_compute_plan_id`, and run `.\.venv\Scripts\python.exe -m pytest -q`.

## TD-02 — Cache immutable bundled example payloads or return defensive copies in `/api/examples/{example_id}`

- **Location**: `datarescue/api.py:150-161`
- **Analysis**: Bundled example files (`orders.csv`, `inventory.json`, `dates.csv` and their schemas) are static package assets, yet every `GET /api/examples/{example_id}` performs two synchronous filesystem opens/reads.

```diff
--- a/datarescue/api.py
+++ b/datarescue/api.py
@@ -147,18 +147,21 @@
     ]
 
 
+@functools.lru_cache(maxsize=len(ALLOWED_EXAMPLES))
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

- **Validation**: Import `functools`, verify mutated response dicts cannot pollute the cache (`json.loads(raw_schema)` creates a fresh dict), and run `.\.venv\Scripts\python.exe -m pytest -q`.
