"""Frontend cleanliness, dead code, outdated copy, and responsive layout hygiene."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / "app" / "static" / "index.html"
LANDING_HTML = ROOT / "app" / "static" / "landing.html"


def test_landing_dead_code_and_icons_removed():
    content = LANDING_HTML.read_text(encoding="utf-8")
    
    # 1. Unused icons in var P
    p_match = re.search(r"var P = \{(.*?)\n\};", content, re.DOTALL)
    assert p_match, "var P not found in landing.html"
    p_icons = p_match.group(1)
    for unused_icon in ["alert:", "pulse:", "refresh:"]:
        assert unused_icon not in p_icons, f"Unused icon {unused_icon} should be removed from landing.html"

    # 2. Dead CSS from legacy key query UI
    dead_selectors = ["#result{", ".alert{", ".chips{", ".avatar{width:44px"]
    for sel in dead_selectors:
        assert sel not in content, f"Dead CSS selector {sel} should be removed from landing.html"


def test_index_dead_code_and_icons_removed():
    content = INDEX_HTML.read_text(encoding="utf-8")

    # 1. Unused icons in const ICONS
    icons_match = re.search(r"const ICONS = \{(.*?)\n\};", content, re.DOTALL)
    assert icons_match, "const ICONS not found in index.html"
    icons_block = icons_match.group(1)
    assert "more:" not in icons_block, "Unused icon 'more' should be removed from const ICONS in index.html"

    # 2. Dead CSS classes from old member table columns and legacy member details grid
    dead_classes = [".mbar{", ".mflags{", ".mq{", ".mtable td.mweek", ".mx-wide{", ".danger-row{",
                    ".mx-grid{", ".mx-g h4{", ".mx-acts{"]
    for dc in dead_classes:
        assert dc not in content, f"Dead CSS {dc} should be removed from index.html"

    # 3. Dead JS functions and unused switch cases
    assert "hasTags =" not in content, "Unused helper hasTags should be removed from index.html"
    assert "case 'copy-code':" not in content, "Unused switch case copy-code should be removed from index.html"


def test_index_outdated_version_copy_removed():
    content = INDEX_HTML.read_text(encoding="utf-8")
    assert "2.15.32" not in content, "Outdated version reference 2.15.32 should be removed from index.html"
    assert "节约模式同样翻倍" not in content, (
        "Outdated tooltip claiming V5 doubles in economy mode should be removed (economy_multiplier is fixed to 1.0)"
    )


def test_index_member_table_mobile_responsive_layout():
    content = INDEX_HTML.read_text(encoding="utf-8")
    
    # tr.mr should layout mcell (m), mact (act), mtoday (q), mstat (s), mtime (t)
    assert 'grid-template-areas:"m act" "q q" "s t"' in content, (
        "Mobile layout should map mstat to 's' and mtime to 't' instead of legacy 'w t'"
    )
    assert "table.mtable td.mstat{grid-area:s" in content, (
        "table.mtable td.mstat must have grid-area:s in mobile responsive stylesheet"
    )
    assert "table.mtable td.mweek" not in content, (
        "Orphaned table.mtable td.mweek style should be removed"
    )


def test_index_time_helpers_declared_in_helpers_section():
    content = INDEX_HTML.read_text(encoding="utf-8")
    
    # fullTime and shortTime should be declared before loadBugs / renderBugs
    pos_full_time = content.find("const fullTime =")
    pos_short_time = content.find("function shortTime(")
    pos_load_bugs = content.find("async function loadBugs()")
    
    assert pos_full_time != -1 and pos_short_time != -1, "fullTime and shortTime must be defined"
    assert pos_full_time < pos_load_bugs, "fullTime must be defined before loadBugs"
    assert pos_short_time < pos_load_bugs, "shortTime must be defined before loadBugs"
