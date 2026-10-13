import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

const initialOptions = { locale: "id-ID", trim: true, spreadsheet_safe: true };
const text = (value) =>
  value === undefined ? "(missing)" : value === null ? "null" : String(value);
const download = (blob, filename) => {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
async function responseError(response) {
  const body = await response.json().catch(() => ({}));
  throw new Error(
    typeof body.detail === "string"
      ? body.detail
      : `Request failed (${response.status}).`,
  );
}

function App() {
  const [file, setFile] = useState(null);
  const [schema, setSchema] = useState({ columns: {}, key: [] });
  const [options, setOptions] = useState(initialOptions);
  const [result, setResult] = useState(null);
  const [previewSettings, setPreviewSettings] = useState("");
  const [examples, setExamples] = useState([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("all");
  const [view, setView] = useState("proposed");
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState(null);
  const [dragging, setDragging] = useState(false);
  const [online, setOnline] = useState(false);
  const fileInput = useRef(null),
    schemaInput = useRef(null);
  const settings = JSON.stringify({ schema, options });
  const dirty = !!result && settings !== previewSettings;
  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      fetch("/api/examples", { signal: controller.signal }),
      fetch("/api/health", { signal: controller.signal }),
    ])
      .then(async ([sampleResponse, health]) => {
        if (!sampleResponse.ok || !health.ok)
          throw new Error("Local server unavailable.");
        setExamples(await sampleResponse.json());
        setOnline(true);
      })
      .catch((reason) => {
        if (reason.name !== "AbortError") setError(reason.message);
      });
    return () => controller.abort();
  }, []);
  const records = useMemo(
    () =>
      result?.records.filter(
        (row) => filter === "all" || row.status === filter,
      ) || [],
    [result, filter],
  );
  const visibleRows = records.slice(page * 25, (page + 1) * 25);
  const columns = result
    ? [
        ...new Set([
          ...result.source_columns.map((name) => schema.rename?.[name] || name),
          ...Object.keys(schema.columns),
        ]),
      ]
    : Object.keys(schema.columns);
  const tableColumns = result
    ? view === "original"
      ? result.source_columns
      : result.columns
    : [];
  const activeRow = result?.records.find((row) => row.row === selected);

  function chooseFile(nextFile) {
    if (!nextFile) return;
    setFile(nextFile);
    setSchema({ columns: {}, key: [] });
    setResult(null);
    setError("");
    setSelected(null);
    setPage(0);
  }
  function updateRule(column, patch) {
    setSchema((current) => {
      const next = { ...current, columns: { ...current.columns } };
      if (patch.type === "raw") {
        delete next.columns[column];
        next.key = next.key.filter((key) => key !== column);
      } else
        next.columns[column] = {
          type: "string",
          required: false,
          date_format: "iso",
          ...next.columns[column],
          ...patch,
        };
      return next;
    });
  }
  function toggleKey(column, checked) {
    setSchema((current) => ({
      ...current,
      columns: {
        ...current.columns,
        [column]: current.columns[column] || { type: "string", required: true },
      },
      key: checked
        ? [...current.key.filter((key) => key !== column), column]
        : current.key.filter((key) => key !== column),
    }));
  }
  function renameHeader(source, target) {
    setSchema((current) => {
      const previous = current.rename?.[source] || source;
      const next = {
        ...current,
        columns: { ...current.columns },
        rename: { ...current.rename, [source]: target },
        key: current.key.map((name) => (name === previous ? target : name)),
      };
      if (current.columns[previous]) {
        next.columns[target] = current.columns[previous];
        if (target !== previous) delete next.columns[previous];
      }
      return next;
    });
  }
  async function loadExample(id) {
    setBusy("example");
    setError("");
    try {
      const response = await fetch(`/api/examples/${id}`);
      if (!response.ok) await responseError(response);
      const sample = await response.json();
      chooseFile(
        new File([sample.content], sample.filename, {
          type: sample.filename.endsWith(".json")
            ? "application/json"
            : "text/csv",
        }),
      );
      setSchema(sample.schema);
      setOptions(initialOptions);
    } catch (reason) {
      setError(reason.message);
    } finally {
      setBusy("");
    }
  }
  async function importSchema(event) {
    const incoming = event.target.files?.[0];
    if (!incoming) return;
    try {
      const parsed = JSON.parse(await incoming.text());
      if (
        !parsed.columns ||
        typeof parsed.columns !== "object" ||
        Array.isArray(parsed.columns) ||
        !Array.isArray(parsed.key || [])
      )
        throw new Error(
          "Expected a schema with columns and an optional key array.",
        );
      const types = ["string", "integer", "number", "date", "boolean"];
      if (
        Object.values(parsed.columns).some(
          (rule) => !rule || !types.includes(rule.type),
        )
      )
        throw new Error("Every column needs a supported type.");
      setSchema({ ...parsed, key: parsed.key || [] });
      setError("");
    } catch (reason) {
      setError(`Schema: ${reason.message}`);
    }
    event.target.value = "";
  }
  function requestBody() {
    const form = new FormData();
    form.append("file", file);
    form.append("schema_json", JSON.stringify(schema));
    form.append("options_json", JSON.stringify(options));
    return form;
  }
  async function inspect() {
    if (!file) return;
    setBusy("scan");
    setError("");
    try {
      const response = await fetch("/api/scan", {
        method: "POST",
        body: requestBody(),
      });
      if (!response.ok) await responseError(response);
      const analysis = await response.json();
      setResult(analysis);
      setPreviewSettings(settings);
      setSelected(null);
      setPage(0);
      setFilter("all");
    } catch (reason) {
      setError(reason.message);
      setResult(null);
    } finally {
      setBusy("");
    }
  }
  async function exportResults() {
    setBusy("export");
    setError("");
    try {
      const body = requestBody();
      body.append("expected_sha256", result.sha256);
      body.append("expected_plan_id", result.plan_id);
      const response = await fetch("/api/export", { method: "POST", body });
      if (!response.ok) await responseError(response);
      download(await response.blob(), "datarescue-results.zip");
    } catch (reason) {
      setError(reason.message);
    } finally {
      setBusy("");
    }
  }
  return (
    <>
      <header className="topbar">
        <a className="brand" href="/">
          <span className="brandmark">dr</span>datarescue
          <span className="version">/ 0.1</span>
        </a>
        <span className={`server ${online ? "connected" : ""}`}>
          <i />
          {online ? "Local server connected" : "Connecting to local server"}
        </span>
      </header>
      <main>
        <section className="intro">
          <div>
            <p className="eyebrow">THE LOCAL DATA REPAIR WORKBENCH</p>
            <h1>
              Messy data.
              <br />
              <span>Clear decisions.</span>
            </h1>
            <p className="lede">
              Inspect CSV and JSON, review each change, and export the rows that
              meet your rules. Keep a complete audit.
            </p>
          </div>
          <div className="process" aria-label="Workflow">
            <div>
              <b>01</b>
              <span>Inspect your file</span>
            </div>
            <div>
              <b>02</b>
              <span>Review the repairs</span>
            </div>
            <div>
              <b>03</b>
              <span>Export with evidence</span>
            </div>
            <p>Runs on your computer. No cloud upload.</p>
          </div>
        </section>
        {error ? (
          <div className="error" role="alert">
            {error}
          </div>
        ) : null}
        <section className="workspace">
          <aside className="settings">
            <div className="section-heading">
              <span className="eyebrow">SOURCE FILE</span>
              <span className="mono">01 / INPUT</span>
            </div>
            <div
              className={`dropzone ${dragging ? "dragging" : ""}`}
              onDragOver={(event) => {
                event.preventDefault();
                if (!busy) setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(event) => {
                event.preventDefault();
                setDragging(false);
                if (!busy) chooseFile(event.dataTransfer.files[0]);
              }}
            >
              <span className="file-symbol">
                {file ? "FILE" : "CSV / JSON"}
              </span>
              <strong>{file?.name || "Start with a file"}</strong>
              <p>
                {file
                  ? `${(file.size / 1024).toFixed(1)} KiB · original preserved`
                  : "Drop a file here, or choose from your computer."}
              </p>
              <button
                className="button secondary"
                disabled={!!busy}
                onClick={() => fileInput.current.click()}
              >
                {file ? "Replace file" : "Choose file"}
              </button>
              <input
                className="sr-only"
                ref={fileInput}
                type="file"
                accept=".csv,.tsv,.json"
                aria-label="Choose data file"
                onChange={(event) => {
                  chooseFile(event.target.files?.[0]);
                  event.target.value = "";
                }}
              />
              <small>UTF-8 · up to 2 MiB · 10,000 rows</small>
            </div>
            <div className="examples">
              <span className="eyebrow">TRY A SYNTHETIC EXAMPLE</span>
              {examples.map((sample) => (
                <button
                  key={sample.id}
                  disabled={!!busy}
                  onClick={() => loadExample(sample.id)}
                >
                  <strong>{sample.name}</strong>
                  <span>{sample.description}</span>
                  <b aria-hidden="true">↗</b>
                </button>
              ))}
            </div>
            <div className="rules-heading">
              <h2>Repair rules</h2>
              <div>
                <button
                  className="text-button"
                  onClick={() => schemaInput.current.click()}
                  disabled={!!busy}
                >
                  Import schema
                </button>
                <input
                  ref={schemaInput}
                  className="sr-only"
                  type="file"
                  accept=".json"
                  aria-label="Import schema"
                  onChange={importSchema}
                />
              </div>
            </div>
            <label className="field">
              <span>Number format for text values</span>
              <select
                value={options.locale}
                disabled={!!busy}
                onChange={(event) =>
                  setOptions((current) => ({
                    ...current,
                    locale: event.target.value,
                  }))
                }
              >
                <option value="id-ID">Indonesia · 1.250,50</option>
                <option value="en-US">US · 1,250.50</option>
              </select>
            </label>
            <label className="check">
              <input
                type="checkbox"
                checked={options.trim}
                disabled={!!busy}
                onChange={(event) =>
                  setOptions((current) => ({
                    ...current,
                    trim: event.target.checked,
                  }))
                }
              />
              <span>Trim surrounding whitespace</span>
            </label>
            <label className="check">
              <input
                type="checkbox"
                checked={options.spreadsheet_safe}
                disabled={!!busy}
                onChange={(event) =>
                  setOptions((current) => ({
                    ...current,
                    spreadsheet_safe: event.target.checked,
                  }))
                }
              />
              <span>Escape spreadsheet formulas in CSV</span>
            </label>
            <p className="help">
              JSON numbers use JSON notation. Column types stay unchanged until
              you declare a rule.
            </p>
            {result ? (
              <details className="header-mappings">
                <summary>Explicit header mappings</summary>
                {result.source_columns.map((source) => (
                  <label className="field" key={source}>
                    <span>{source} → output name</span>
                    <input
                      aria-label={`Rename ${source}`}
                      disabled={!!busy}
                      value={schema.rename?.[source] ?? source}
                      onChange={(event) =>
                        renameHeader(source, event.target.value)
                      }
                    />
                  </label>
                ))}
              </details>
            ) : null}
            {columns.length ? (
              <div className="column-rules">
                {columns.map((column) => {
                  const rule = schema.columns[column];
                  return (
                    <div className="column-rule" key={column}>
                      <label>
                        <span className="column-name" title={column}>
                          {column}
                        </span>
                        <select
                          aria-label={`Type for ${column}`}
                          disabled={!!busy}
                          value={rule?.type || "raw"}
                          onChange={(event) =>
                            updateRule(column, { type: event.target.value })
                          }
                        >
                          <option value="raw">Keep original</option>
                          <option value="string">Text / identifier</option>
                          <option value="number">Exact decimal</option>
                          <option value="integer">Integer</option>
                          <option value="date">Date</option>
                          <option value="boolean">Boolean</option>
                        </select>
                      </label>
                      {rule?.type === "date" ? (
                        <label className="date-rule">
                          Date order
                          <select
                            aria-label={`Date order for ${column}`}
                            disabled={!!busy}
                            value={rule.date_format || "iso"}
                            onChange={(event) =>
                              updateRule(column, {
                                date_format: event.target.value,
                              })
                            }
                          >
                            <option value="iso">ISO only</option>
                            <option value="dmy">Day / month / year</option>
                            <option value="mdy">Month / day / year</option>
                            <option value="auto">Only unambiguous</option>
                          </select>
                        </label>
                      ) : null}
                      <div className="row-flags">
                        <label className="check">
                          <input
                            type="checkbox"
                            disabled={!!busy || !rule}
                            checked={!!rule?.required}
                            onChange={(event) =>
                              updateRule(column, {
                                required: event.target.checked,
                              })
                            }
                          />
                          Required
                        </label>
                        <label className="check">
                          <input
                            type="checkbox"
                            disabled={!!busy}
                            checked={schema.key.includes(column)}
                            onChange={(event) =>
                              toggleKey(column, event.target.checked)
                            }
                          />
                          Unique key
                        </label>
                      </div>
                    </div>
                  );
                })}
                <button
                  className="text-button"
                  onClick={() =>
                    download(
                      new Blob([JSON.stringify(schema, null, 2)], {
                        type: "application/json",
                      }),
                      "schema.json",
                    )
                  }
                >
                  Download schema
                </button>
              </div>
            ) : null}
            <button
              className="button primary scan"
              disabled={!file || !!busy}
              onClick={inspect}
            >
              {busy === "scan"
                ? "Inspecting…"
                : dirty
                  ? "Scan with updated rules"
                  : "Inspect file"}
              <span aria-hidden="true">↗</span>
            </button>
          </aside>
          <div className="review">
            <div className="section-heading">
              <span className="eyebrow">REVIEW WORKSPACE</span>
              <span className="mono">02 / DECISIONS</span>
            </div>
            {!result ? (
              <div className="empty-review">
                <div className="empty-grid">
                  <span />
                  <span />
                  <span />
                  <span />
                  <span />
                  <span />
                  <span />
                  <span />
                  <span />
                </div>
                <h2>Every repair has a reason.</h2>
                <p>
                  Load an example or inspect your file. You will see proposed
                  changes, validation errors, and the rows that need attention.
                </p>
                <div className="empty-notes">
                  <span>Identifiers retain leading zeros</span>
                  <span>Ambiguous dates stay unresolved</span>
                  <span>Rejected rows remain in the export</span>
                </div>
              </div>
            ) : (
              <>
                {dirty ? (
                  <div className="notice" role="status">
                    Rules changed. Scan again to refresh the preview and enable
                    export.
                  </div>
                ) : null}
                <div className="metrics">
                  <div>
                    <span>Total rows</span>
                    <strong>{result.summary.total}</strong>
                  </div>
                  <div className="good">
                    <span>Accepted</span>
                    <strong>{result.summary.accepted}</strong>
                  </div>
                  <div className="bad">
                    <span>Quarantined</span>
                    <strong>{result.summary.quarantined}</strong>
                  </div>
                  <div>
                    <span>Accepted-row changes</span>
                    <strong>{result.summary.applied_changes}</strong>
                  </div>
                </div>
                <div
                  className="quality-bar"
                  aria-label={`${result.summary.accepted} accepted of ${result.summary.total} rows`}
                >
                  <span
                    style={{
                      width: `${(result.summary.accepted / result.summary.total) * 100}%`,
                    }}
                  />
                </div>
                {result.drift.length ? (
                  <div className="drift">
                    {result.drift.map((item) => (
                      <span
                        key={`${item.code}-${item.column}`}
                        className={item.severity}
                      >
                        {item.code === "missing_column"
                          ? "Missing"
                          : "Retained extra"}
                        : {item.column}
                      </span>
                    ))}
                  </div>
                ) : null}
                <div className="table-toolbar">
                  <div className="tabs" aria-label="Filter rows">
                    {[
                      ["all", "All rows"],
                      ["accepted", "Accepted"],
                      ["quarantine", "Quarantined"],
                    ].map(([value, label]) => (
                      <button
                        key={value}
                        className={filter === value ? "active" : ""}
                        aria-pressed={filter === value}
                        onClick={() => {
                          setFilter(value);
                          setPage(0);
                        }}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                  <label>
                    View
                    <select
                      aria-label="Table values"
                      value={view}
                      onChange={(event) => setView(event.target.value)}
                    >
                      <option value="proposed">Proposed</option>
                      <option value="original">Original</option>
                    </select>
                  </label>
                </div>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Row</th>
                        <th>Decision</th>
                        {tableColumns.map((column) => (
                          <th key={column}>{column}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {visibleRows.map((record) => (
                        <tr
                          key={record.row}
                          className={selected === record.row ? "selected" : ""}
                        >
                          <td>
                            <button
                              className="row-button"
                              aria-label={`Inspect row ${record.row}`}
                              onClick={() => setSelected(record.row)}
                            >
                              {String(record.row).padStart(2, "0")}
                            </button>
                          </td>
                          <td>
                            <span className={`badge ${record.status}`}>
                              {record.status === "accepted"
                                ? "Accepted"
                                : "Quarantine"}
                            </span>
                          </td>
                          {tableColumns.map((column) => (
                            <td
                              key={column}
                              className={
                                view === "proposed" &&
                                record.changes.some(
                                  (change) => change.column === column,
                                )
                                  ? "changed"
                                  : ""
                              }
                              title={text(record[view][column])}
                            >
                              {text(record[view][column])}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {!visibleRows.length ? (
                    <p className="no-rows">No rows in this category.</p>
                  ) : null}
                </div>
                <div className="pagination">
                  <span>
                    {records.length} rows · select a row number to inspect
                  </span>
                  <div>
                    <button
                      disabled={page === 0}
                      onClick={() => setPage((current) => current - 1)}
                    >
                      Previous
                    </button>
                    <span>
                      {page + 1} / {Math.max(1, Math.ceil(records.length / 25))}
                    </span>
                    <button
                      disabled={(page + 1) * 25 >= records.length}
                      onClick={() => setPage((current) => current + 1)}
                    >
                      Next
                    </button>
                  </div>
                </div>
                {activeRow ? (
                  <section className="row-detail">
                    <div className="rules-heading">
                      <h2>Row {activeRow.row} audit</h2>
                      <button
                        className="text-button"
                        onClick={() => setSelected(null)}
                      >
                        Close
                      </button>
                    </div>
                    {activeRow.errors.map((item, index) => (
                      <p className="row-error" key={index}>
                        <b>{item.column}</b> · {item.message}
                      </p>
                    ))}
                    {activeRow.changes.map((change, index) => (
                      <div className="change-detail" key={index}>
                        <b>{change.column}</b>
                        <code>
                          {JSON.stringify(change.before) ?? "(missing)"}
                        </code>
                        <span>→</span>
                        <code>{JSON.stringify(change.after)}</code>
                      </div>
                    ))}
                    {!activeRow.changes.length && !activeRow.errors.length ? (
                      <p>This row meets the selected rules without changes.</p>
                    ) : null}
                    {activeRow.status === "quarantine" ? (
                      <p className="help">
                        Proposals for this row are not applied. The quarantine
                        export retains its original values.
                      </p>
                    ) : null}
                  </section>
                ) : null}
                <section className="export-panel">
                  <div>
                    <span className="eyebrow">03 / EXPORT WITH EVIDENCE</span>
                    <h2>
                      Keep the good rows.
                      <br />
                      Keep the full story.
                    </h2>
                    <p>
                      ZIP includes clean CSV + JSON, quarantined rows, the
                      schema, and an audit report. CSV formula escaping appears
                      in the report.
                    </p>
                  </div>
                  <button
                    className="button primary"
                    disabled={!!busy || dirty}
                    onClick={exportResults}
                  >
                    {busy === "export" ? "Exporting…" : "Approve & export ZIP"}
                    <span aria-hidden="true">↓</span>
                  </button>
                </section>
                <div className="source-proof">
                  <span>Source SHA-256</span>
                  <code>{result.sha256}</code>
                </div>
              </>
            )}
          </div>
        </section>
        <section className="how">
          <div>
            <span className="eyebrow">ALSO WORKS IN YOUR TERMINAL</span>
            <h2>
              One engine.
              <br />
              Two ways to work.
            </h2>
          </div>
          <div>
            <code>
              datarescue scan examples/orders.csv
              <br /> --schema examples/orders.schema.json
            </code>
            <p>
              Use the CLI in scripts and CI. Exit code 1 identifies quarantined
              rows. Exit code 2 identifies input or configuration errors.
            </p>
          </div>
        </section>
      </main>
      <footer>
        <span>datarescue / local data repair</span>
        <span>Rule-based engine · CSV / TSV / flat JSON · v0.1.0</span>
      </footer>
    </>
  );
}

createRoot(document.getElementById("root")).render(<App />);
