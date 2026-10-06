"""Deal Intelligence Graph: derivations on the demo seed, the live-DB derivation, the import CLI
and the read API (build brief §4-§5, §9 acceptance 1 and 6)."""
import json

import pytest
from fastapi.testclient import TestClient

from src import dashboard, db, graph


@pytest.fixture()
def seed():
    return graph.load_seed()


def test_seed_links_all_resolve(seed):
    raw = json.loads(graph.SEED_FILE.read_text(encoding="utf-8"))
    assert len(seed.links) == len(raw["links"])  # no dangling link silently dropped
    assert {l["rel"] for l in seed.links} <= set(graph.RELS)


def test_investigate_veracitech_acceptance(seed):
    inv = graph.investigate(seed, "veracitech")
    assert {c["label"] for c in inv["contacts"]} == {"JP Nel", "Thabo Mokoena", "Riaan Botha"}
    assert "Consult-Eng Partners" in {c["label"] for c in inv["consultants"]}
    assert {"Barloworld", "Transnet", "Waterfall Estate"} <= {e["label"] for e in inv["endusers"]}
    assert len(inv["opportunities"]) >= 3 and inv["owner"] == "Quin"
    assert {p["label"] for p in inv["projects"]} == {"Barloworld Tender", "Transnet ANPR", "Waterfall Perimeter"}
    consult = next(c for c in inv["consultants"] if c["label"] == "Consult-Eng Partners")
    assert consult["via"] == "Barloworld Tender, Transnet ANPR"  # 'via' = the shared project names
    assert any("Duxbury" in p for p in inv["plays"])  # competitor on a shared project is surfaced


def test_ego_groups_and_second_ring(seed):
    e1 = graph.ego(seed, "veracitech", 1)
    rels = {g["rel"] for g in e1["groups"]}
    assert {"works_at", "opportunity", "involved_in"} <= rels
    assert all(n["ring"] == 1 for n in e1["nodes"])
    e2 = graph.ego(seed, "veracitech", 2)
    ring2 = [n for n in e2["nodes"] if n["ring"] == 2]
    assert ring2 and all(n["parent"] in {x["id"] for x in e1["nodes"]} for n in ring2)
    assert "consult-eng" in {n["id"] for n in ring2}  # one step away via a shared project


def test_dossier_kyc_pending_and_relationships(seed):
    d = graph.dossier(seed, "riaan")
    assert d["kyc_pending"] is True and d["employer"] == "Veracitech"
    d = graph.dossier(seed, "jp-nel")
    assert d["kyc_pending"] is False and d["enrichment"]["experience"]
    d = graph.dossier(seed, "veracitech")
    assert d["investigable"] and {g["rel"] for g in d["relationships"]} >= {"works_at", "involved_in"}


def test_search_ranks_prefix_first(seed):
    assert graph.search(seed, "veraci")[0]["id"] == "veracitech"
    assert graph.search(seed, "mokoena")[0]["id"] == "thabo"   # word-prefix inside a name
    assert graph.search(seed, "") == []


def test_filter_and_monogram(seed):
    f = graph.filter_graph(seed, types=["project"])
    assert {n["type"] for n in f["nodes"]} == {"project"}
    f = graph.filter_graph(seed, opps_only=True)
    assert all(n["oppCount"] for n in f["nodes"]) and "veracitech" in {n["id"] for n in f["nodes"]}
    svg = graph.monogram_svg(seed.by_id["jp-nel"])
    assert svg.startswith("<svg") and ">JN<" in svg


def test_live_graph_from_kyc_db(cfg, loaded):
    g = graph.load_live(loaded, cfg)
    kinds = {graph.lens_key(n) for n in g.nodes}
    assert {"person", "customer", "competitor"} <= kinds
    acme = next(n for n in g.nodes if n["label"] == "Acme Security (Pty) Ltd")
    people = {g.by_id[o]["label"] for l, o, d in g.neighbors(acme["id"], "works_at") if d == "in"}
    assert {"Anna Smith", "Bob Jones"} <= people
    anna = next(n for n in g.nodes if n["label"] == "Anna Smith")
    assert anna["photoUrl"] == f"/api/photo/{anna['id']}" and anna["kyc"] == "pending"
    inv = graph.investigate(g, acme["id"])  # works on partial data: no projects yet
    assert inv["stats"]["projects"] == 0 and inv["stats"]["contacts"] >= 2


def test_import_projects_into_live(cfg, loaded, tmp_path):
    f = tmp_path / "deals.json"
    f.write_text(json.dumps({
        "nodes": [
            {"id": "acme", "label": "Acme Security (Pty) Ltd", "type": "company", "group": "customer"},
            {"id": "beta", "label": "Beta Integrators", "type": "company", "group": "consultant"},
            {"id": "ghost", "label": "Nobody Ltd", "type": "company", "group": "enduser"},
            {"id": "tower", "label": "Tower CCTV", "type": "project", "brands": ["Milestone"],
             "opps": [{"t": "Tower phase 1", "p": "Quoted", "val": "R 300k"}]},
        ],
        "links": [{"source": "acme", "target": "tower", "rel": "involved_in"},
                  {"source": "beta", "target": "tower", "rel": "specifies"},
                  {"source": "ghost", "target": "tower", "rel": "involved_in"}],
    }), encoding="utf-8")
    s = graph.import_file(loaded, f)
    assert s["projects"] == 1 and s["opportunities"] == 1 and s["links"] == 2 and s["unmatched"] == ["Nobody Ltd"]
    assert graph.import_file(loaded, f)["projects"] == 1  # idempotent re-import
    assert db.one(loaded, "SELECT count(*) n FROM projects")["n"] == 1
    g = graph.load_live(loaded, cfg)
    acme = next(n for n in g.nodes if n["label"] == "Acme Security (Pty) Ltd")
    inv = graph.investigate(g, acme["id"])
    assert [c["label"] for c in inv["consultants"]] == ["Beta Integrators"]
    assert inv["opportunities"][0]["t"] == "Tower phase 1"


def test_graph_api(cfg, loaded, monkeypatch):
    monkeypatch.setenv("ELVEY_GRAPH_SOURCE", "seed")
    c = TestClient(dashboard.create_app(cfg))
    full = c.get("/api/graph").json()
    assert full["source"] == "seed" and len(full["nodes"]) > 20
    assert {n["type"] for n in c.get("/api/graph?types=project").json()["nodes"]} == {"project"}
    assert c.get("/api/entity/veracitech").json()["node"]["label"] == "Veracitech"
    assert c.get("/api/entity/nope").status_code == 404
    assert c.get("/api/entity/veracitech/ego?depth=2").json()["center"]["id"] == "veracitech"
    assert c.get("/api/account/veracitech/investigate").json()["stats"]["contacts"] == 3
    assert c.get("/api/account/thabo/investigate").status_code == 400
    assert c.get("/api/search?q=trans").json()[0]["label"].startswith("Transnet")
    r = c.get("/api/photo/riaan")
    assert r.headers["content-type"].startswith("image/svg+xml") and ">RB<" in r.text

    monkeypatch.setenv("ELVEY_GRAPH_SOURCE", "live")
    live = c.get("/api/graph").json()
    assert live["source"] == "live"
    anna = next(n for n in live["nodes"] if n["label"] == "Anna Smith")
    assert c.get(f"/api/photo/{anna['id']}").headers["content-type"] == "image/jpeg"  # real face from the KYC folder
