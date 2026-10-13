# Two-minute demo

1. Start `run.ps1` and open the local URL.
2. Load **Order export** and inspect the file. Expected result: 12 records, 7 accepted, 5 quarantined.
3. Inspect row 1. Show `00123` unchanged, whitespace trimming, exact `1250000.5`, and ISO date conversion.
4. Filter **Quarantined** and inspect rows 4, 5 and 6. Show the duplicate ID, invalid amount, and impossible date. The originals remain available.
5. Switch the table between **Original** and **Proposed**.
6. Change a rule. Export becomes disabled until the scan is refreshed.
7. Export the ZIP. Open the audit and quarantine files. Account for every data row.
8. Load **JSON inventory**. Show the decimal `9007199254740993.25` without rounding.
9. Load **Ambiguous dates**. With unambiguous-only parsing, 3 rows are accepted and 2 quarantined. Change date order to DMY and rescan: 4 accepted, 1 quarantined.
10. Run the same order example through the CLI. A quality failure returns code 1 while the repair command still creates the audit ZIP.

Describe the engine as deterministic and schema-driven. Do not call it an LLM agent or claim autonomous recovery across arbitrary pipelines.
