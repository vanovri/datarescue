"""Exercise real browser flows against the running local application."""

import argparse
import io
import json
import tempfile
import zipfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8763")
    parser.add_argument("--channel", default="msedge")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    errors, checks = [], []
    with sync_playwright() as playwright, tempfile.TemporaryDirectory() as temporary:
        browser = playwright.chromium.launch(channel=args.channel)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url, wait_until="domcontentloaded")
        expect(page.get_by_text("Local server connected")).to_be_visible()
        expect(page.get_by_role("heading", name="Every repair has a reason.")).to_be_visible()
        checks.append("initial local connection and empty state")

        page.get_by_role("button", name="Order export", exact=False).click()
        page.get_by_role("button", name="Inspect file", exact=True).click()
        expect(page.locator(".metrics .good strong")).to_have_text("7")
        expect(page.locator(".metrics .bad strong")).to_have_text("5")
        expect(page.get_by_role("cell", name="00123", exact=True)).to_be_visible()
        page.get_by_role("button", name="Inspect row 1", exact=True).click()
        expect(page.locator(".row-detail")).to_contain_text("1250000.5")
        page.get_by_role("button", name="Close", exact=True).click()
        page.screenshot(path=str(root / "docs" / "workbench.png"), full_page=True)
        checks.append("order example, exact values, identifier preservation and row audit")

        page.get_by_role("button", name="Quarantined", exact=True).click()
        expect(page.locator("tbody tr")).to_have_count(5)
        page.get_by_role("button", name="Inspect row 4", exact=True).click()
        expect(page.locator(".row-detail")).to_contain_text("earlier accepted row")
        page.get_by_role("button", name="Close", exact=True).click()
        page.get_by_role("button", name="All rows", exact=True).click()
        page.get_by_label("Table values", exact=True).select_option("original")
        expect(page.get_by_role("cell", name="1.250.000,50", exact=True)).to_be_visible()
        page.get_by_label("Table values", exact=True).select_option("proposed")
        checks.append("quarantine filter and original/proposed views")

        with page.expect_download() as event:
            page.get_by_role("button", name="Approve & export ZIP", exact=True).click()
        target = Path(temporary) / "results.zip"
        event.value.save_as(target)
        with zipfile.ZipFile(target) as archive:
            assert len(archive.namelist()) == 6
            assert len(json.loads(archive.read("data.clean.json"))) == 7
            assert len(json.loads(archive.read("data.quarantine.json"))) == 5
            assert len(json.loads(archive.read("report.json"))["export_changes"]) == 1
        checks.append("downloaded ZIP: all 12 records accounted for and formula escape audited")

        page.get_by_role("button", name="JSON inventory", exact=False).click()
        page.get_by_role("button", name="Inspect file", exact=True).click()
        expect(page.get_by_role("cell", name="9007199254740993.25", exact=True)).to_be_visible()
        expect(page.locator(".metrics .good strong")).to_have_text("4")
        checks.append("JSON decimal beyond safe binary-float precision")

        page.get_by_role("button", name="Ambiguous dates", exact=False).click()
        page.get_by_role("button", name="Inspect file", exact=True).click()
        expect(page.locator(".metrics .good strong")).to_have_text("3")
        page.get_by_label("Date order for date", exact=True).select_option("dmy")
        expect(page.get_by_role("button", name="Approve & export ZIP", exact=True)).to_be_disabled()
        expect(page.get_by_text("Rules changed. Scan again", exact=False)).to_be_visible()
        page.get_by_role("button", name="Scan with updated rules", exact=True).click()
        expect(page.locator(".metrics .good strong")).to_have_text("4")
        checks.append("ambiguous dates require a decision; changed rules block stale export")

        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=str(root / "docs" / "mobile.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 1000})
        checks.append("390px mobile containment and readable stacked layout")

        source = b"id,qty\n001,4\n002,broken\n"
        page.get_by_label("Choose data file", exact=True).set_input_files({"name": "drift.csv", "mimeType": "text/csv", "buffer": source})
        page.get_by_role("button", name="Inspect file", exact=True).click()
        page.get_by_text("Explicit header mappings", exact=True).click()
        page.get_by_label("Rename qty", exact=True).fill("quantity")
        page.get_by_label("Type for quantity", exact=True).select_option("integer")
        page.get_by_role("button", name="Scan with updated rules", exact=True).click()
        expect(page.get_by_role("columnheader", name="quantity", exact=True)).to_be_visible()
        expect(page.locator(".metrics .good strong")).to_have_text("1")
        expect(page.locator(".metrics .bad strong")).to_have_text("1")
        page.get_by_label("Table values", exact=True).select_option("original")
        expect(page.get_by_role("columnheader", name="qty", exact=True)).to_be_visible()
        checks.append("explicit header mapping and original-header provenance")

        source = ("id,name\n" + "".join(f"{index:03},User {index}\n" for index in range(1, 81))).encode()
        page.get_by_label("Choose data file", exact=True).set_input_files({"name": "pages.csv", "mimeType": "text/csv", "buffer": source})
        page.get_by_role("button", name="Inspect file", exact=True).click()
        expect(page.locator("tbody tr")).to_have_count(25)
        page.get_by_role("button", name="Next", exact=True).click()
        expect(page.get_by_role("button", name="Inspect row 26", exact=True)).to_be_visible()
        checks.append("bounded 25-row pagination")

        page.get_by_label("Choose data file", exact=True).set_input_files({"name": "bad.csv", "mimeType": "text/csv", "buffer": b"id,id\n1,2\n"})
        page.get_by_role("button", name="Inspect file", exact=True).click()
        expect(page.get_by_role("alert")).to_contain_text("Duplicate CSV headers")
        checks.append("readable malformed-input rejection")
        assert not errors, errors
        checks.append("no browser runtime errors")
        browser.close()
    report = {"url": args.url, "channel": args.channel, "checks_passed": len(checks), "checks": checks, "browser_errors": errors}
    (root / "docs" / "browser-validation.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
