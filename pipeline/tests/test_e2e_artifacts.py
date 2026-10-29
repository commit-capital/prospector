from __future__ import annotations

import io
import struct
import tarfile
import zlib
from pathlib import Path

import pytest

from pipeline.e2e_artifacts import (
    MAX_ENTRIES,
    MAX_PNG_BYTES,
    MAX_PNGS,
    MAX_REPORT_BYTES,
    clean_png,
    read_artifacts,
)

SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(ctype: bytes, data: bytes = b"", crc: int | None = None) -> bytes:
    if crc is None:
        crc = zlib.crc32(ctype + data)
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)


def _png_with_chunks(types: list[bytes]) -> bytes:
    out = SIG
    for t in types:
        data = b"\x00" * 13 if t == b"IHDR" else b"" if t == b"IEND" else b"x"
        out += _chunk(t, data)
    return out


def _chunk_types(png: bytes | None) -> list[bytes]:
    assert png is not None
    pos, types = 8, []
    while pos < len(png):
        (n,) = struct.unpack(">I", png[pos : pos + 4])
        types.append(png[pos + 4 : pos + 8])
        pos += 12 + n
    return types


GOOD = _png_with_chunks([b"IHDR", b"IDAT", b"IEND"])


def _tar(entries: list[tuple[str, bytes | tarfile.TarInfo]]) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, body in entries:
            if isinstance(body, tarfile.TarInfo):
                body.name = name
                tf.addfile(body)
            else:
                ti = tarfile.TarInfo(name)
                ti.size = len(body)
                tf.addfile(ti, io.BytesIO(body))
    buf.seek(0)
    return buf


def _special(kind: bytes, linkname: str = "") -> tarfile.TarInfo:
    ti = tarfile.TarInfo("x")
    ti.type = kind
    ti.linkname = linkname
    return ti


def _hostile_tar() -> io.BytesIO:
    return _tar(
        [
            ("e2e-out/report.json", b"{}"),
            ("e2e-out/../x.png", GOOD),
            ("/etc/x.png", GOOD),
            ("e2e-out/link.png", _special(tarfile.SYMTYPE, "/etc/passwd")),
        ]
    )


def test_reads_report_and_pngs() -> None:
    a = read_artifacts(_tar([("e2e-out/report.json", b'{"a":1}'), ("e2e-out/a/b.png", GOOD)]))
    assert a.report == b'{"a":1}'
    assert a.pngs == {"a/b.png": GOOD}
    assert a.skipped == []


def test_skips_symlinks_hardlinks_devices_and_dirs() -> None:
    a = read_artifacts(
        _tar(
            [
                ("e2e-out/", _special(tarfile.DIRTYPE)),
                ("e2e-out/sym.png", _special(tarfile.SYMTYPE, "/etc/passwd")),
                ("e2e-out/hard.png", _special(tarfile.LNKTYPE, "e2e-out/ok.png")),
                ("e2e-out/dev.png", _special(tarfile.CHRTYPE)),
                ("e2e-out/ok.png", GOOD),
            ]
        )
    )
    assert list(a.pngs) == ["ok.png"]
    assert len(a.skipped) == 3
    assert all(any(n in s for n in ("sym.png", "hard.png", "dev.png")) for s in a.skipped)


def test_skips_traversal_and_absolute_names() -> None:
    a = read_artifacts(_hostile_tar())
    assert a.pngs == {}
    assert a.report == b"{}"
    assert len(a.skipped) == 3
    assert any("../x.png" in s for s in a.skipped)
    assert any("/etc/x.png" in s for s in a.skipped)


def test_skips_entries_outside_prefix() -> None:
    a = read_artifacts(_tar([("other/a.png", GOOD), ("report.json", b"{}")]))
    assert a.pngs == {} and a.report is None
    assert len(a.skipped) == 2


def test_symlinked_root_yields_empty() -> None:
    a = read_artifacts(_tar([("e2e-out", _special(tarfile.SYMTYPE, "/etc"))]))
    assert a.report is None and a.pngs == {}
    assert len(a.skipped) == 1 and "e2e-out" in a.skipped[0]


def test_nested_report_json_is_not_the_report() -> None:
    a = read_artifacts(_tar([("e2e-out/sub/report.json", b"{}")]))
    assert a.report is None and a.pngs == {}
    assert len(a.skipped) == 1


def test_caps_png_size_and_count() -> None:
    big = GOOD + b"\x00" * (MAX_PNG_BYTES)
    a = read_artifacts(_tar([("e2e-out/big.png", big)]))
    assert a.pngs == {} and len(a.skipped) == 1 and "big.png" in a.skipped[0]

    entries: list[tuple[str, bytes | tarfile.TarInfo]] = [
        (f"e2e-out/{i}.png", GOOD) for i in range(MAX_PNGS + 1)
    ]
    b = read_artifacts(_tar(entries))
    assert len(b.pngs) == MAX_PNGS
    assert f"{MAX_PNGS}.png" not in b.pngs
    assert len(b.skipped) == 1 and f"{MAX_PNGS}.png" in b.skipped[0]


def test_rejected_pngs_do_not_use_count_cap() -> None:
    entries: list[tuple[str, bytes | tarfile.TarInfo]] = [
        (f"e2e-out/bad{i}.png", b"nope") for i in range(MAX_PNGS)
    ]
    entries.append(("e2e-out/good.png", GOOD))
    a = read_artifacts(_tar(entries))
    assert list(a.pngs) == ["good.png"]
    assert len(a.skipped) == MAX_PNGS


def test_caps_report_size() -> None:
    a = read_artifacts(_tar([("e2e-out/report.json", b"x" * (MAX_REPORT_BYTES + 1))]))
    assert a.report is None
    assert len(a.skipped) == 1 and "report.json" in a.skipped[0]


def test_non_png_bytes_with_png_name_skipped() -> None:
    a = read_artifacts(_tar([("e2e-out/fake.png", b"<html>not a png</html>")]))
    assert a.pngs == {}
    assert len(a.skipped) == 1 and "fake.png" in a.skipped[0]


def test_non_png_names_are_skipped() -> None:
    a = read_artifacts(_tar([("e2e-out/notes.txt", b"hi")]))
    assert a.pngs == {} and a.report is None
    assert len(a.skipped) == 1


def test_truncated_stream_keeps_earlier_entries() -> None:
    raw = _tar([("e2e-out/a.png", GOOD), ("e2e-out/b.png", GOOD * 50)]).getvalue()
    a = read_artifacts(io.BytesIO(raw[: 512 * 2 + 512 + 100]))
    assert a.pngs == {"a.png": GOOD}
    assert len(a.skipped) == 1 and "truncated" in a.skipped[0]


def test_corrupt_stream_does_not_raise() -> None:
    a = read_artifacts(io.BytesIO(b"this is not a tar archive" * 100))
    assert a.report is None and a.pngs == {}
    assert len(a.skipped) == 1


def test_clean_png_keeps_only_critical_chunks() -> None:
    png = _png_with_chunks([b"IHDR", b"tEXt", b"iTXt", b"eXIf", b"PLTE", b"tRNS", b"IDAT", b"IEND"])
    assert _chunk_types(clean_png(png)) == [b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"]


def test_clean_png_rejects_bad_crc_and_truncation() -> None:
    bad = SIG + _chunk(b"IHDR", b"\x00" * 13) + _chunk(b"IDAT", b"x", crc=0) + _chunk(b"IEND")
    assert clean_png(bad) is None
    assert clean_png(GOOD[:-3]) is None
    assert clean_png(GOOD[:-12]) is None
    assert clean_png(b"") is None


def test_clean_png_rejects_structure_errors() -> None:
    assert clean_png(GOOD + b"\x00") is None
    assert clean_png(b"\x89PNG\r\n\x1a\x0b" + GOOD[8:]) is None
    assert clean_png(SIG + _chunk(b"IDAT", b"x") + _chunk(b"IEND")) is None
    assert clean_png(SIG + _chunk(b"IHDR", b"\x00" * 13) + _chunk(b"IDAT", b"x")) is None
    huge = SIG + struct.pack(">I", 2**31) + b"IHDR"
    assert clean_png(huge) is None


def test_clean_png_is_idempotent() -> None:
    assert clean_png(GOOD) == GOOD


def test_never_writes_to_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    before = sorted(tmp_path.rglob("*"))
    read_artifacts(_hostile_tar())
    assert sorted(tmp_path.rglob("*")) == before


def test_entry_count_is_capped_and_skipped_is_bounded() -> None:
    n = MAX_ENTRIES + 500
    a = read_artifacts(_tar([(f"e2e-out/{'z' * 2000}{i}.txt", b"x") for i in range(n)]))
    assert len(a.skipped) == MAX_ENTRIES + 1
    assert a.skipped[-1] == f"more than {MAX_ENTRIES} entries; the rest were not read"
    assert all(len(line) < 300 for line in a.skipped)


def test_skipped_lines_cannot_forge_log_lines() -> None:
    a = read_artifacts(_tar([("e2e-out/a\nFORGED\x1b[0m\u202e.txt", b"x")]))
    assert len(a.skipped) == 1
    assert "\n" not in a.skipped[0] and "\x1b" not in a.skipped[0] and "\u202e" not in a.skipped[0]
    assert "a?FORGED?[0m?.txt" in a.skipped[0]


@pytest.mark.parametrize(
    "name",
    [
        "e2e-out/a\nX.png",
        "e2e-out/a\\..\\b.png",
        "e2e-out/\u202egnp.txt.png",
        " ../x.png",
        "e2e-out/ a.png",
        "e2e-out/a//b.png",
        "e2e-out/./a.png",
        "e2e-out/a/../b.png",
    ],
)
def test_unsafe_png_keys_are_skipped(name: str) -> None:
    a = read_artifacts(_tar([(name, GOOD)]))
    assert a.pngs == {}
    assert len(a.skipped) == 1


def test_clean_png_requires_13_byte_ihdr() -> None:
    for n in (12, 14):
        png = SIG + _chunk(b"IHDR", b"\x00" * n) + _chunk(b"IDAT", b"x") + _chunk(b"IEND")
        assert clean_png(png) is None


def test_clean_png_requires_an_idat() -> None:
    assert clean_png(SIG + _chunk(b"IHDR", b"\x00" * 13) + _chunk(b"IEND")) is None


def test_clean_png_requires_empty_iend() -> None:
    png = SIG + _chunk(b"IHDR", b"\x00" * 13) + _chunk(b"IDAT", b"x") + _chunk(b"IEND", b"x")
    assert clean_png(png) is None
