"""Tests for the v4 Analyze/BI panels (V4-SPEC.md §4)."""
from __future__ import annotations

from src import analyze, db


def _panel(panels, key):
    return next(p for p in panels if p["key"] == key)


def test_panels_shape_and_totals(cfg, loaded):
    panels = analyze.panels(loaded)
    keys = [p["key"] for p in panels]
    assert len(keys) == len(set(keys))  # every panel key is unique
    for p in panels:
        assert p["total"] == sum(s["value"] for s in p["slices"])
        for s in p["slices"]:
            assert "label" in s and "value" in s


def test_by_branch_matches_contact_counts(cfg, loaded):
    panels = analyze.panels(loaded)
    branch = _panel(panels, "branch")
    total_contacts = db.one(loaded, "SELECT count(*) n FROM contacts")["n"]
    assert branch["total"] == total_contacts
    customers = next(s for s in branch["slices"] if s["label"] == "Customers")
    assert customers["filter"] == {"branch": "customer"}


def test_by_account_role_reflects_dual_role_supplier(cfg, loaded):
    panels = analyze.panels(loaded)
    roles = _panel(panels, "account_role")
    labels = {s["label"]: s["value"] for s in roles["slices"]}
    assert labels["Customer accounts"] == 2  # Acme + Beta
    assert labels["Supplier accounts"] == 1  # Acme's installer_category 'SubD'


def test_allocation_panel_filters_map_to_rep_select_values(cfg, loaded):
    panels = analyze.panels(loaded)
    alloc = _panel(panels, "allocation")
    by_label = {s["label"]: s for s in alloc["slices"]}
    assert by_label["Allocated"]["filter"] == {"branch": "customer", "frep": "__alloc"}
    assert by_label["Unallocated"]["filter"] == {"branch": "customer", "frep": "__unalloc"}
    assert by_label["Allocated"]["value"] + by_label["Unallocated"]["value"] == \
        db.one(loaded, "SELECT count(*) n FROM contacts WHERE segment='customer'")["n"]


def test_by_category_sorts_by_cadence_not_alphabetically(cfg, loaded):
    loaded.execute("UPDATE contacts SET category='D' WHERE full_name='Anna Smith'")
    loaded.execute("UPDATE contacts SET category='A' WHERE full_name='Bob Jones'")
    loaded.commit()
    panels = analyze.panels(loaded)
    cat = _panel(panels, "category")
    labels = [s["label"] for s in cat["slices"]]
    assert labels.index("A") < labels.index("D")


def test_by_contact_class_unclassified_maps_to_fclass_none(cfg, loaded):
    panels = analyze.panels(loaded)
    cls = _panel(panels, "contact_class")
    unclassified = next(s for s in cls["slices"] if s["label"] == "Unclassified")
    assert unclassified["filter"] == {"branch": "customer", "fclass": "__none"}
    assert unclassified["value"] == db.one(
        loaded, "SELECT count(*) n FROM contacts WHERE segment='customer' AND contact_class IS NULL")["n"]


def test_sellout_by_division_is_money_metric_and_sums_correctly(cfg, loaded):
    panels = analyze.panels(loaded)
    p = _panel(panels, "sellout_division")
    assert p["metric"] == "money"
    total = db.one(loaded, "SELECT sum(coalesce(latest_sellout,0)) v FROM accounts WHERE segment='customer'")["v"]
    assert p["total"] == total


def test_in_zoho_and_role_coverage_panels_have_no_filter(cfg, loaded):
    panels = analyze.panels(loaded)
    for key in ("in_zoho", "in_makdb", "role_source", "coverage"):
        p = _panel(panels, key)
        assert all("filter" not in s for s in p["slices"])
