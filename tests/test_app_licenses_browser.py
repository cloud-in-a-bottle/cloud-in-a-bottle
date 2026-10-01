import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Page
from playwright.sync_api import expect
from test_accessibility import WCAG_AA_TAGS
from test_accessibility import stack as stack

from compute_space.tests.local_stack import LocalStack
from compute_space.tests.local_stack import complete_setup


@pytest.mark.parametrize("width", [1280, 390])
def test_licenses_from_preview_to_app_pages(
    page: Page, stack: LocalStack, tmp_path: Path, output_path: str, width: int
) -> None:
    owner = complete_setup(stack)
    page.context.add_cookies(
        [{"name": cookie.name, "value": cookie.value, "url": stack.router_url} for cookie in owner.cookies]
    )
    page.set_viewport_size({"width": width, "height": 900})
    cases = [
        ("split", "AGPL-3.0-only", "MIT"),
        ("legacy", None, None),
        ("custom", "LicenseRef-" + "custom" * 40, "MIT OR Apache-2.0"),
        (
            "html",
            "<script>window.licenseInjected = true</script>",
            '<img src=x onerror="window.licenseInjected = true">',
        ),
    ]
    for index, (suffix, license_value, packaging_license) in enumerate(cases):
        name = f"license-{suffix}"
        fields = "".join(
            f"{field} = {json.dumps(value)}\n"
            for field, value in [("license", license_value), ("packaging_license", packaging_license)]
            if value is not None
        )
        raw = (
            f'[app]\nname = "{name}"\nversion = "1.0.0"\n'
            + fields
            + '[runtime.container]\nimage = "Dockerfile"\nport = 8000\n'
        )
        repo = tmp_path / name
        repo.mkdir()
        filename = "openhost.toml" if suffix == "legacy" else "cloudinabottle.toml"
        (repo / filename).write_text(raw)
        (repo / "Dockerfile").write_text("FROM scratch\n")

        page.goto(f"{stack.router_url}/add_app")
        page.locator("#repo-url").fill(repo.as_uri())
        with page.expect_response("**/api/clone_and_get_app_info") as preview_response:
            page.locator("#deploy-btn").click()
        response = preview_response.value
        assert response.ok, response.text()
        preview = response.json()
        try:
            assert preview["manifest"]["license"] == (license_value or "")
            assert preview["manifest"]["packaging_license"] == (packaging_license or "")
            expect(page.locator("#confirm-section")).to_be_visible()
            for label, value in [("Application license", license_value), ("Packaging license", packaging_license)]:
                row = page.locator("#manifest-table tr").filter(has=page.get_by_text(label, exact=True))
                expect(row.locator("td").nth(1)).to_have_text(value or "Not specified")
            expect(page.locator("#manifest-table script, #manifest-table img")).to_have_count(0)
            assert page.evaluate("window.licenseInjected === undefined")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(Path(output_path) / f"preview-{suffix}.png"), full_page=True)
        finally:
            shutil.rmtree(Path(preview["clone_dir"]).parent)

        with closing(sqlite3.connect(stack.config.db_path)) as db, db:
            db.execute(
                "INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw) "
                "VALUES (?, ?, '1.0.0', ?, ?, 'stopped', ?)",
                (name, name, str(repo), 29800 + index, raw),
            )

    page.goto(f"{stack.router_url}/dashboard")
    for suffix, license_value, packaging_license in cases:
        row = page.locator(f'[data-app-name="license-{suffix}"]')
        expect(row).to_contain_text(f"Application license: {license_value or 'Not specified'}")
        expect(row).to_contain_text(f"Packaging license: {packaging_license or 'Not specified'}")
    expect(page.locator("#app-list script, #app-list img")).to_have_count(0)
    assert page.evaluate("window.licenseInjected === undefined")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    violations = Axe().run(page, options={"runOnly": {"type": "tag", "values": WCAG_AA_TAGS}}).response["violations"]
    assert not violations
    page.screenshot(path=str(Path(output_path) / "dashboard.png"), full_page=True)

    # License metadata must not interfere with filtering or the Details link.
    page.locator("#app-filter").fill("split")
    expect(page.locator(".app-row:visible")).to_have_count(1)
    row = page.locator('[data-app-name="license-split"]')
    row.hover()
    row.get_by_role("link", name="Details").click()
    page.wait_for_url(f"{stack.router_url}/app_detail/license-split")
    for suffix, license_value, packaging_license in cases:
        page.goto(f"{stack.router_url}/app_detail/license-{suffix}")
        for label, value in [("Application license", license_value), ("Packaging license", packaging_license)]:
            row = page.get_by_role("row").filter(has=page.get_by_role("rowheader", name=label, exact=True))
            expect(row.get_by_role("cell")).to_have_text(value or "Not specified")
        assert page.evaluate("window.licenseInjected === undefined")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(Path(output_path) / f"detail-{suffix}.png"), full_page=True)
