"""Shared fixtures: a synthetic consolidation workbook (same layout as the real one), a face
folder, a config pointing at tmp paths, and an in-memory fake Zoho."""
from __future__ import annotations

import copy
import itertools
from pathlib import Path

import openpyxl
import pytest
from PIL import Image

from src import db
from src.config import load_config

ROOT = Path(__file__).resolve().parent.parent

SHEETS = {
    "READ ME — schema": [["Synthetic test workbook"]],
    "dim_accounts": [
        ["account_id", "company", "accno", "allocation_proposed", "allocation_candidates", "allocation_conflict",
         "allocation_status", "category", "cluster", "branch", "division", "axis_partner", "fy25_sellout",
         "fy26_sellout", "tasha_visit", "tasha_concerns"],
        ["A0001", "Acme Security (Pty) Ltd", "ACM01", "SN", "SN / TJ", "Y", "Allocated", "A", "HO", "Pentagon",
         "Elvey Projects", "Silver", "1,000,000", 2500000.5, "High", " ; Pricing concerns"],
        ["A0002", "Beta Integrators", None, "HW", None, None, "Allocated", "B", None, None, "Elvey Inland",
         None, 0, 900000, None, None],
        ["A0003", "Gamma Systems", None, "EC", None, None, "Allocated", "C", None, None, "Elvey Coastal",
         None, 50000, 0, None, None],
        ["A0010", "Elvey", None, "1house", None, None, "Allocated", "D", None, None, None, None, 0, 0, None, None],
        ["A0011", "Duxbury", None, "1house", None, None, "Allocated", "D", None, None, None, None, 0, 0, None, None],
    ],
    "dim_contacts": [
        ["contact_id", "account_id", "full_name", "first_name", "last_name", "role", "role_status", "email", "tel",
         "cell", "linkedin_url", "segment", "category", "image_filename", "image_status", "src"],
        ["C1", "A0001", "Anna Smith", "Anna", "Smith", "CEO", None, "Anna@acme.co.za", None, "082 000 0001", "in ›",
         "Customer", "A", "CUST__Anna_Smith.jpg", "downloaded", "Template"],
        ["C2", "A0001", "Bob Jones", "Bob", "Jones", None, "lookup pending (KYC app)", "bob@acme.co.za", None, None,
         "https://www.linkedin.com/in/bobjones", "Customer", "A", None, "none", "Zoho"],
        ["C3", "A0001", "Bob Jones", "Bob", "Jones", "Buyer", None, "bob@acme.co.za", None, "083 111 2222", None,
         "Customer", "A", None, "none", "Marketing"],  # duplicate of C2 (same email)
        ["C4", "A0002", "Carla Müller", "Carla", "Müller", "PM", None, None, None, None, None, "Customer", "B", None,
         "none", "Template"],
        ["C5", "A0003", "Dan Out", "Dan", "Out", None, None, "dan@gamma.co.za", None, None, None, "Customer", "C",
         None, "none", "Template"],  # outside top-N
        ["C6", "A0010", "Jaco Moolman", "Jaco", "Moolman", "Director", None, None, None, None, None, "Customer",
         "D", "INT__Jaco_Moolman.jpg", "downloaded", "Template"],  # internal person listed as customer
        ["C7", None, "Jaco Moolman", "Jaco", "Moolman", None, None, None, None, None, None, "Internal", None,
         "INT__Jaco_Moolman.jpg", "downloaded", "KYC seed"],
        ["C8", None, "Leandro da Cunha", "Leandro", "da Cunha", None, None, None, None, None, None, "Competitor",
         None, "Duxbury__Leandro_da_Cunha.jpg", "downloaded", "KYC seed"],
        ["C9", None, "Gordon Moore", "Gordon", "Moore", None, None, None, None, None, None, "Competitor", None,
         "Reditron__Gordon_Moore.jpg", "downloaded", "KYC seed"],
    ],
    "dim_reps": [
        ["rep_id", "am_code", "name", "email", "role", "cluster", "branch", "status"],
        ["R02", "HW", "Haiko Wostmann", "h@x", "PM / Solutions Architect", "HO", "Pentagon", "active"],
        ["R03", "SN", "Siresen Naidoo", "s@x", "Account Manager", "HO", "Pentagon", "active"],
        ["R10", "TJ", "Timothy Steyn", "t@x", "Account Manager", "Central", "Greenstone", "active"],
    ],
    "dim_brands": [["brand_id", "brand", "qlik_supplier", "portfolio_flag"], ["B1", "Bosch", "BOSCH", "Y"],
                   ["B2", "Milestone", "MILESTONE", "Y"]],
    "fact_sellout": [["account_id", "brand_id", "fy25", "fy26"], ["A0001", "B1", 0, 100], ["A0001", "B2", 0, 900]],
    "view_top50_projects": [["rank", "account_id", "company"], [1, "A0001", "Acme Security (Pty) Ltd"],
                            [2, "A0002", "Beta Integrators"], [3, "A0003", "Gamma Systems"]],
    "view_top500_contacts": [["rank", "contact_id", "company", "full_name"], [1, "C1", "", ""], [2, "C2", "", ""],
                             [3, "C3", "", ""], [4, "C4", "", ""], [9, "C5", "", ""], [10, "C6", "", ""]],
}

FACES = ["CUST__Anna_Smith.jpg", "INT__Jaco_Moolman.jpg", "Duxbury__Leandro_da_Cunha.jpg",
         "Reditron__Gordon_Moore.jpg", "INT__Tasha_Smith.jpg", "CUST__Carla_Muller.jpg"]


def write_workbook(path: Path) -> Path:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in SHEETS.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    wb.save(path)
    return path


@pytest.fixture
def cfg(tmp_path):
    kyc = tmp_path / "kyc"
    kyc.mkdir()
    for i, f in enumerate(FACES):
        Image.new("RGB", (240, 240), (i * 30, 100, 150)).save(kyc / f, "JPEG")
    wbk = write_workbook(tmp_path / "consolidated.xlsx")
    c = load_config(ROOT / "config.yaml", overrides={
        "paths.kyc_folder": str(kyc), "paths.db": str(tmp_path / "t.db"), "paths.data_dir": str(tmp_path / "data"),
        "paths.to_enrich_csv": str(tmp_path / "data" / "to_enrich.csv"),
        "paths.browser_profile": str(tmp_path / "profile"),
        "inputs.workbook": str(wbk), "scope.top_accounts": 2, "scope.top_contacts": 6,
    })
    c["segments"]["competitor_accounts"] = {"Duxbury": "Duxbury", "Reditron": "Reditron"}
    return c


@pytest.fixture
def conn(cfg):
    c = db.connect(cfg.db_path)
    yield c
    c.close()


@pytest.fixture
def loaded(cfg, conn):
    from src import images, ingest

    ingest.run(cfg)
    images.match(conn, cfg)
    return conn


class FakeZoho:
    """In-memory stand-in for ZohoClient (same public methods)."""

    FIELDS = {
        "Accounts": ["id", "Account_Name", "Pentagon_Account_Number", "Owner", "Modified_Time", "Division", "Elvey_Rep"],
        "Contacts": ["id", "First_Name", "Last_Name", "Full_Name", "Email", "Phone", "Mobile", "Title", "Account_Name",
                     "Record_Image", "Modified_Time"],
    }

    def __init__(self, accounts=None, contacts=None):
        self.store = {"Accounts": {r["id"]: r for r in (accounts or [])}, "Contacts": {r["id"]: r for r in (contacts or [])}}
        self.calls: list[tuple] = []
        self.photos: dict[str, bytes] = {}
        self._ids = itertools.count(9000)

    def field_names(self, module):
        return list(self.FIELDS[module])

    def iter_records(self, module, fields, per_page=200):
        self.calls.append(("GET", module))
        for r in self.store[module].values():
            yield {k: copy.deepcopy(v) for k, v in r.items() if k in fields or k == "id"}

    def write(self, method, module, records):
        self.calls.append((method, module, copy.deepcopy(records)))
        out = []
        for r in records:
            unknown = [k for k in r if k not in self.FIELDS[module]]
            if unknown:
                out.append({"code": "INVALID_DATA", "message": f"unknown {unknown}"})
                continue
            if method == "POST":
                rid = str(next(self._ids))
                self.store[module][rid] = {**r, "id": rid}
            else:
                rid = r["id"]
                self.store[module][rid].update(r)
            if module == "Contacts":
                rec = self.store[module][rid]
                rec["Full_Name"] = " ".join(filter(None, [rec.get("First_Name"), rec.get("Last_Name")]))
            out.append({"code": "SUCCESS", "details": {"id": rid}, "message": "ok"})
        return out

    def upload_photo(self, module, record_id, content, filename):
        self.calls.append(("PHOTO", module, record_id))
        self.photos[record_id] = content
        self.store[module][record_id]["Record_Image"] = "img"
        return True, "success"


@pytest.fixture
def fake_zoho():
    return FakeZoho(
        accounts=[{"id": "100", "Account_Name": "ACME SECURITY", "Division": "Old division", "Owner": {"id": "1"}},
                  {"id": "101", "Account_Name": "Unrelated Ltd"}],
        contacts=[{"id": "200", "First_Name": "Anna", "Last_Name": "Smith", "Full_Name": "Anna Smith",
                   "Email": "anna@acme.co.za", "Mobile": "+27 82 000 0001", "Title": "CEO",
                   "Account_Name": {"id": "100", "name": "ACME SECURITY"}}],
    )
