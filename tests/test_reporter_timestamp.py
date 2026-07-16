import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.reporter import generate_hub_index


def test_generate_hub_index_includes_utc_timestamp_in_footer(tmp_path: Path) -> None:
    report_dir = tmp_path
    (report_dir / "QUAL").mkdir()
    (report_dir / "QUAL" / "2026-11.json").write_text('[]', encoding="utf-8")

    html = generate_hub_index(report_dir)

    assert "UTC" in html
    assert "Pure maths" in html
