# Contributing

Report the smallest synthetic input that reproduces a failure. Include the schema, locale and expected row-level output. Do not post private datasets or credentials.

Preserve identifiers and original values. Require explicit rules for conversions and header mappings. Every rejected row needs a reason and a traceable original record. Do not silently drop records, infer dates, or merge fields.

Run `python -m pytest` and `npm run build` in `frontend/`. For UI changes, exercise the affected workflow at desktop and phone widths. The core should stay usable without FastAPI or a model provider.

Use Conventional Commit messages. Explain the concrete trigger, behavior change, and validation in pull requests. Add a regression test only when it protects a meaningful behavior.
