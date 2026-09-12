"""Responsive PLC snapshot catalog UI contracts."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_snapshot_webviews_share_container_responsive_layout_contract():
    html = (ROOT / "tc_agent" / "static" / "index.html").read_text(encoding="utf-8")
    duplicate = (ROOT / "tc_agent_vsix" / "webview" / "index.html").read_text(encoding="utf-8")

    assert html == duplicate
    assert ".snapshot-page { container-type:inline-size; container-name:snapshot-page;" in html
    assert "@container snapshot-page (max-width: 720px)" in html
    assert ".snapshot-page:not(.detail-only) .snapshot-detail-panel { display:none; }" in html
    assert ".snapshot-page.detail-only .snapshot-list-panel { display:none; }" in html
    assert ".snapshot-card { width:100%; height:auto; min-height:0;" in html
    assert ".snapshot-card-stats { display:flex; flex-wrap:wrap;" in html
    assert "function formatSnapshotTime(value)" in html
    assert "const snapshotViewport = {listScrollTop:0, detailScrollTop:0};" in html
    assert ".snapshot-content-view .st-code { min-width:0; overflow:auto; }" in html


def test_snapshot_cards_do_not_reintroduce_global_button_fixed_height():
    html = (ROOT / "tc_agent" / "static" / "index.html").read_text(encoding="utf-8")
    card_css = html[html.index(".snapshot-card {"):html.index(".snapshot-card:hover")]

    assert "height:auto" in card_css
    assert "min-height:0" in card_css
    assert "white-space:normal" in card_css
    assert "overflow:visible" in card_css
