"""Tests for nina-generator pipeline stages."""

from pathlib import Path

from nina.generator.stages.action_infer import infer_actions, url_pattern_for_type
from nina.generator.stages.assemble import assemble_contract
from nina.generator.stages.dom_extract import extract_dom_signals
from nina.generator.stages.sitemap import infer_page_type, parse_sitemap
from nina.generator.diff import contract_diff_text
from nina.generator.stages.routes import build_routes_manifest, merge_routes_into_contract
from nina.generator.stages.validate import validate_contract

EXAMPLES = Path(__file__).resolve().parents[1] / "contracts" / "examples"


def test_parse_sitemap():
    entries = parse_sitemap(EXAMPLES / "sitemap.xml", "https://example.com")
    assert len(entries) >= 4
    assert entries[0]["url"].startswith("https://")


def test_infer_page_type():
    assert infer_page_type("https://example.com/cart") == "cart"
    assert infer_page_type("https://example.com/shop") == "product_list"


def test_dom_extract_finds_search():
    html = '<html><body><input type="search" id="q" name="query"></body></html>'
    signals = extract_dom_signals(html)
    assert len(signals["searchInputs"]) == 1
    assert signals["searchInputs"][0]["selector"] == "#q"


def test_dom_extract_finds_size_buttons():
    html = """
    <html><body>
      <button>S</button><button>M</button><button>L</button>
      <button>Add to cart</button>
    </body></html>
    """
    signals = extract_dom_signals(html)
    assert signals["sizeOptions"] == ["S", "M", "L"]


def test_infer_actions_builds_search():
    dom = {"home": {"searchInputs": [{"selector": "#q", "name": "q"}]}}
    actions, selectors = infer_actions({"home"}, dom)
    ids = {a["id"] for a in actions}
    assert "search" in ids
    assert "navigate" in ids
    assert selectors.get("search_input") == "#q"


def test_assemble_and_validate_minimal():
    site = {"id": "test", "name": "Test", "baseUrl": "https://example.com"}
    crawled = [{"url": "https://example.com/", "pageType": "home", "html": ""}]
    actions, selectors = infer_actions({"home"}, {})
    contract = assemble_contract(site, crawled, actions, selectors)
    ok, errors = validate_contract(contract)
    assert ok, errors


def test_assemble_auto_enforces_inferred_high_risk_actions_without_policy_file():
    """The per-action risk:"high" field (set by infer_actions for checkout)
    must translate into actual enforcement (contract.risk.confirmActions),
    not stay purely descriptive. Before this fix, a generated contract with
    no risk.policy.yaml (or one that didn't happen to list this action's
    exact id) would ship with checkout fully unenforced at runtime, even
    though the JSON itself says "risk": "high"."""
    site = {"id": "test", "name": "Test", "baseUrl": "https://example.com"}
    crawled = [
        {"url": "https://example.com/cart", "pageType": "cart", "html": ""},
    ]
    actions, selectors = infer_actions({"cart"}, {})
    assert any(a["id"] == "checkout" and a.get("risk") == "high" for a in actions)

    contract = assemble_contract(site, crawled, actions, selectors)  # no risk_policy passed
    assert "checkout" in (contract.get("risk") or {}).get("confirmActions", [])


def test_assemble_merges_inferred_high_risk_with_human_authored_policy():
    site = {"id": "test", "name": "Test", "baseUrl": "https://example.com"}
    crawled = [{"url": "https://example.com/cart", "pageType": "cart", "html": ""}]
    actions, selectors = infer_actions({"cart"}, {})
    contract = assemble_contract(
        site, crawled, actions, selectors,
        risk_policy={"confirmActions": ["place_order"], "blockActions": ["export_all_data"]},
    )
    risk = contract["risk"]
    assert set(risk["confirmActions"]) == {"checkout", "place_order"}
    assert risk["blockActions"] == ["export_all_data"]


def test_build_routes_manifest():
    crawled = [
        {"url": "https://example.com/", "pageType": "home"},
        {"url": "https://example.com/cart", "pageType": "cart"},
    ]
    manifest = build_routes_manifest(crawled)
    assert len(manifest["routes"]) == 2
    merged = merge_routes_into_contract({"site": {}, "version": "1.0.0"}, manifest)
    assert merged["routes"][1]["pageId"] == "cart"


def test_contract_diff_text():
    diff = contract_diff_text({"version": "1.0.0"}, {"version": "1.0.1"})
    assert "1.0.0" in diff
    assert "1.0.1" in diff


def test_examples_pipeline_dry_run():
    from nina.generator.pipeline import run_pipeline

    result = run_pipeline(EXAMPLES, dry_run=True)
    assert result.ok, result.errors
    assert result.contract is not None
    assert result.stats.get("actions", 0) >= 1
