from pathlib import Path

from diskrecover.core.deduplication import find_duplicates
from diskrecover.core.export import export_files
from diskrecover.core.validation import IntegrityStatus, validate_tree


def test_validation_detects_pdf_and_duplicates(tmp_path: Path):
    first = tmp_path / "a.pdf"
    second = tmp_path / "copy.pdf"
    payload = b"%PDF-1.7\n1 0 obj\nendobj\n%%EOF\n"
    first.write_bytes(payload)
    second.write_bytes(payload)

    results = validate_tree(tmp_path)

    assert {result.integrity for result in results} == {IntegrityStatus.VALID_STRUCTURE}
    assert sum(result.status == IntegrityStatus.DUPLICATE for result in results) == 1
    assert len(find_duplicates(tmp_path)) == 1


def test_export_is_nondestructive_and_conflict_safe(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    original = source / "document.pdf"
    original.write_bytes(b"%PDF-1.7\n%%EOF\n")
    results = validate_tree(source)

    destination = tmp_path / "destination"
    records = export_files(results, destination, flatten=True, source_root=source)

    assert records[0].status == "EXPORTED"
    assert (destination / "document.pdf").read_bytes() == original.read_bytes()
    assert original.exists()
    assert (destination / "export-map.csv").is_file()
