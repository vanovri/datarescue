# Decisions

## Repair authority

The user selects a schema and normalizations, sees a preview, and approves export. The engine normalizes only declared types and selected whitespace rules. It never updates the source file. Schema drift is detected; header repairs require a precise source-to-target mapping. Target collisions abort the scan.

## Precision and identifiers

Strings stay strings unless configured otherwise. Decimal parsing uses `Decimal`, not `float`. Native JSON decimal tokens carry a marker until conversion so the locale cannot reinterpret `1.234` as `1234`. Normalized decimal values serialize as strings, including JSON exports. An integer rule can remove leading zeros, so identifiers should use the string rule.

## Quarantine

Each input record receives one decision. A record with a failed rule enters quarantine with its original fields, proposal, and reasons. Only an accepted record reserves a unique key. The first accepted occurrence wins deterministically; later duplicates are not silently removed. No imputation occurs.

## Preview identity

The source SHA-256 identifies input bytes. The plan digest combines that hash with a canonical schema and options. Export requires both, preventing changes between preview and application. No server session or uploaded-file database is needed.

## Export transformations

CSV formula escaping is separate from data normalization. It applies to header strings and nonnumeric text in exported CSV, including quarantine files. Every escape appears in `export_changes`; JSON retains exact text. The quarantine CSV uses the source header names even when accepted rows use mapped names.

## Distribution

One Python core serves both interfaces. React is built once and served by FastAPI on the same localhost origin. The supported web package is the source checkout with compiled frontend assets. The ZIP contains those assets for convenient preview; a clean Git clone rebuilds them. No model provider or database is required.

## Scope

Two MiB, 10,000 logical records, and 100 columns bound the intended workload. The upload request has a file limit plus a 64 KiB metadata allowance. Duplicate keys in JSON, duplicate CSV headers, inconsistent row widths, nested JSON, and malformed encodings stop processing before information can disappear.
