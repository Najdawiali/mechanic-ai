"""How scripts/web_verify.py reads NHTSA's bulletin files: a CSV with a header, or the flat file without one."""

import importlib.util
import io
import sys
import zipfile
from pathlib import Path

_spec = importlib.util.spec_from_file_location("web_verify", Path(__file__).resolve().parents[1] / "scripts" / "web_verify.py")
verify = importlib.util.module_from_spec(_spec)
sys.modules["web_verify"] = verify
_spec.loader.exec_module(verify)


def _member(name: str, text: str):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(name, text)
    zf = zipfile.ZipFile(buffer)
    return zf, zf.infolist()[0]


def test_the_flat_file_without_a_header_uses_the_documented_field_order():
    line = "10170893\t\t20200120\t20-01-003H\t20200116\tT4M\tSCA\tHYUNDAI\tSONATA HYBRID\t2016\tELECTRICAL\t\t\tDTC P1990"
    zf, member = _member("flat.txt", line + "\n" + line + "\n")
    info = verify.inspect_member(zf, member)
    assert info["delimiter"] == "TAB" and not info["looks_like_header"]
    cols = verify.columns(info)
    row = next(verify.rows(zf, member, "\t", "utf-8"))
    assert (row[cols["make"]], row[cols["model"]], row[cols["year"]], row[cols["doc_id"]]) == ("HYUNDAI", "SONATA HYBRID", "2016", "20-01-003H")


def test_a_csv_with_a_header_is_read_by_column_name():
    text = '"NHTSA ID Number","TSB/Document ID","Make","Model","Model Year","Summary"\n"10160095","19-HC-001H","HYUNDAI","SONATA HYBRID","2018","P1990"\n'
    zf, member = _member("comms.csv", text)
    info = verify.inspect_member(zf, member)
    assert info["delimiter"] == "COMMA" and info["looks_like_header"]
    cols = verify.columns(info)
    assert cols["make"] == 2 and cols["model"] == 3 and cols["year"] == 4 and cols["summary"] == 5 and cols["doc_id"] == 1


def test_an_unknown_layout_is_reported_not_guessed():
    zf, member = _member("odd.txt", "1\t2\t3\n4\t5\t6\n")
    assert verify.columns(verify.inspect_member(zf, member)) is None
