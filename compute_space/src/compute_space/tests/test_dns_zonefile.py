"""The zone file as the source of truth: parse, edit, serialize, round-trip."""

from __future__ import annotations

import threading
from pathlib import Path

import dns.rdatatype
import dns.zone
import pytest

import compute_space.core.dns.zonefile as zonefile
from compute_space.core.dns.records import DnsRecord
from compute_space.core.dns.records import InvalidRecord

ZONE = "app.example.com"


def _seed(path: Path, serial: int = 100) -> Path:
    """A zone file in the shape the CoreDNS template writes, including the parenthesized SOA and
    the blank-owner continuation that regex-based editing trips over."""
    path.write_text(
        f"$ORIGIN {ZONE}.\n"
        "$TTL 300\n"
        f"@   IN SOA  ns.{ZONE}. admin.{ZONE}. (\n"
        f"    {serial}   ; serial\n"
        "    3600  ; refresh\n"
        "    600   ; retry\n"
        "    86400 ; expire\n"
        "    60    ; minimum\n"
        ")\n"
        f"@   IN NS   ns.{ZONE}.\n"
        "ns  IN A    203.0.113.10\n"
        "@   IN A    203.0.113.10\n"
        "*   IN A    203.0.113.10\n"
    )
    return path


def _serial(path: Path) -> int:
    zone_obj = dns.zone.from_file(str(path), origin=ZONE + ".", relativize=False)
    rdataset = zone_obj.get_rdataset("@", dns.rdatatype.SOA)
    assert rdataset is not None
    return int(rdataset[0].serial)


def _by_key(path: Path) -> dict[tuple[str, str], list[str]]:
    out: dict[tuple[str, str], list[str]] = {}
    for record in zonefile.read_records(path, ZONE):
        assert record.data is not None
        out.setdefault((record.name, record.type), []).append(record.data)
    return out


def test_reads_the_template_shape_including_apex_and_wildcard(tmp_path: Path) -> None:
    path = _seed(tmp_path / "zonefile")
    records = _by_key(path)

    assert records[("@", "A")] == ["203.0.113.10"]
    assert records[("*", "A")] == ["203.0.113.10"]
    assert records[("ns", "A")] == ["203.0.113.10"]
    # The apex is reported as "@" rather than as the bare zone name.
    assert not any(name == ZONE for name, _ in records)


def test_append_adds_without_disturbing_the_existing_rrset(tmp_path: Path) -> None:
    path = _seed(tmp_path / "zonefile")
    zonefile.append_records(path, ZONE, [DnsRecord("_acme-challenge", "TXT", 60, "first")])
    zonefile.append_records(path, ZONE, [DnsRecord("_acme-challenge", "TXT", 60, "second")])

    assert sorted(_by_key(path)[("_acme-challenge", "TXT")]) == ['"first"', '"second"']


def test_set_replaces_the_whole_rrset(tmp_path: Path) -> None:
    path = _seed(tmp_path / "zonefile")
    zonefile.append_records(path, ZONE, [DnsRecord("_acme-challenge", "TXT", 60, "stale")])

    zonefile.set_records(
        path,
        ZONE,
        [DnsRecord("_acme-challenge", "TXT", 60, "base"), DnsRecord("_acme-challenge", "TXT", 60, "wildcard")],
    )

    assert sorted(_by_key(path)[("_acme-challenge", "TXT")]) == ['"base"', '"wildcard"']


def test_set_leaves_other_names_and_types_alone(tmp_path: Path) -> None:
    path = _seed(tmp_path / "zonefile")
    zonefile.append_records(path, ZONE, [DnsRecord("www", "A", 300, "198.51.100.7")])

    zonefile.set_records(path, ZONE, [DnsRecord("_acme-challenge", "TXT", 60, "tok")])

    records = _by_key(path)
    assert records[("www", "A")] == ["198.51.100.7"]
    assert records[("*", "A")] == ["203.0.113.10"]


def test_delete_without_data_clears_the_whole_rrset(tmp_path: Path) -> None:
    # This is what a cleanup path needs: a run that crashed before recording the token it wrote
    # cannot delete by exact value.
    path = _seed(tmp_path / "zonefile")
    zonefile.append_records(
        path, ZONE, [DnsRecord("_acme-challenge", "TXT", 60, "a"), DnsRecord("_acme-challenge", "TXT", 60, "b")]
    )

    zonefile.delete_records(path, ZONE, [DnsRecord("_acme-challenge", "TXT", data=None)])

    assert ("_acme-challenge", "TXT") not in _by_key(path)


def test_delete_with_data_removes_only_that_record(tmp_path: Path) -> None:
    path = _seed(tmp_path / "zonefile")
    zonefile.append_records(
        path, ZONE, [DnsRecord("_acme-challenge", "TXT", 60, "a"), DnsRecord("_acme-challenge", "TXT", 60, "b")]
    )

    zonefile.delete_records(path, ZONE, [DnsRecord("_acme-challenge", "TXT", 60, '"a"')])

    assert _by_key(path)[("_acme-challenge", "TXT")] == ['"b"']


def test_deleting_something_absent_is_not_an_error(tmp_path: Path) -> None:
    # Cleanup paths run twice; the second one must not raise.
    path = _seed(tmp_path / "zonefile")
    zonefile.delete_records(path, ZONE, [DnsRecord("_acme-challenge", "TXT", data=None)])


def test_every_write_bumps_the_serial_so_coredns_reloads(tmp_path: Path) -> None:
    path = _seed(tmp_path / "zonefile", serial=100)
    zonefile.append_records(path, ZONE, [DnsRecord("www", "A", 300, "198.51.100.7")])
    assert _serial(path) == 101
    zonefile.delete_records(path, ZONE, [DnsRecord("www", "A", data=None)])
    assert _serial(path) == 102


def test_serial_wraps_rather_than_overflowing(tmp_path: Path) -> None:
    # Serials are unsigned 32-bit; RFC 1982 arithmetic makes the wrapped value compare as newer.
    path = _seed(tmp_path / "zonefile", serial=2**32 - 1)
    zonefile.append_records(path, ZONE, [DnsRecord("www", "A", 300, "198.51.100.7")])
    assert _serial(path) == 0


def test_multi_string_and_long_txt_values_round_trip(tmp_path: Path) -> None:
    # A >255-byte TXT has to be split into chunks on the wire; the parser must give it back whole.
    path = _seed(tmp_path / "zonefile")
    long_value = "x" * 400
    zonefile.append_records(path, ZONE, [DnsRecord("long", "TXT", 60, f'"{long_value[:255]}" "{long_value[255:]}"')])

    data = _by_key(path)[("long", "TXT")][0]
    assert long_value[:255] in data and long_value[255:] in data


def test_mx_and_srv_rdata_round_trip(tmp_path: Path) -> None:
    path = _seed(tmp_path / "zonefile")
    zonefile.append_records(
        path,
        ZONE,
        [
            DnsRecord("@", "MX", 300, "10 mail.example.com."),
            DnsRecord("_sip._tcp", "SRV", 300, "10 5 443 host.example.com."),
        ],
    )

    records = _by_key(path)
    assert records[("@", "MX")] == ["10 mail.example.com."]
    assert records[("_sip._tcp", "SRV")] == ["10 5 443 host.example.com."]


def test_written_file_is_still_parseable_by_coredns_conventions(tmp_path: Path) -> None:
    # Canonical output loses comments, but everything that matters must survive a re-read.
    path = _seed(tmp_path / "zonefile")
    zonefile.append_records(path, ZONE, [DnsRecord("www", "A", 300, "198.51.100.7")])

    reread = _by_key(path)
    assert reread[("@", "SOA")]
    assert reread[("@", "NS")]
    assert reread[("www", "A")] == ["198.51.100.7"]


def test_invalid_rdata_is_rejected_before_the_file_changes(tmp_path: Path) -> None:
    path = _seed(tmp_path / "zonefile")
    before = path.read_text()

    with pytest.raises(InvalidRecord):
        zonefile.append_records(path, ZONE, [DnsRecord("www", "A", 300, "not-an-ip")])

    assert path.read_text() == before


def test_a_missing_zone_file_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(zonefile.ZoneFileError, match="does not exist"):
        zonefile.read_records(tmp_path / "nope", ZONE)


def test_unparseable_zone_file_is_a_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "zonefile"
    path.write_text("this is not a zone file {{{\n")
    with pytest.raises(zonefile.ZoneFileError, match="could not parse"):
        zonefile.read_records(path, ZONE)


def test_update_router_records_repoints_only_the_router_owned_names(tmp_path: Path) -> None:
    path = _seed(tmp_path / "zonefile")
    zonefile.append_records(path, ZONE, [DnsRecord("www", "A", 300, "198.51.100.7")])

    zonefile.update_router_records(path, ZONE, "203.0.113.99")

    records = _by_key(path)
    assert records[("@", "A")] == ["203.0.113.99"]
    assert records[("*", "A")] == ["203.0.113.99"]
    assert records[("ns", "A")] == ["203.0.113.99"]
    assert records[("www", "A")] == ["198.51.100.7"]


def test_concurrent_appends_do_not_lose_records(tmp_path: Path) -> None:
    # Read-modify-write on a shared file: without the per-zone lock the last writer wins and
    # everyone else's record vanishes. The cert path and any number of apps now race here.
    path = _seed(tmp_path / "zonefile")
    errors: list[BaseException] = []

    def write(i: int) -> None:
        try:
            zonefile.append_records(path, ZONE, [DnsRecord(f"host{i}", "A", 300, f"198.51.100.{i}")])
        except BaseException as e:  # noqa: BLE001 - surfaced via the assert below
            errors.append(e)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(1, 21)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    records = _by_key(path)
    for i in range(1, 21):
        assert records[(f"host{i}", "A")] == [f"198.51.100.{i}"]


def test_a_failed_write_leaves_the_previous_file_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # CoreDNS re-reads on mtime change, so a half-written file would make the zone unresolvable.
    path = _seed(tmp_path / "zonefile")
    before = path.read_text()

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(zonefile.os, "replace", boom)
    with pytest.raises(zonefile.ZoneFileError):
        zonefile.append_records(path, ZONE, [DnsRecord("www", "A", 300, "198.51.100.7")])

    assert path.read_text() == before
    assert not (tmp_path / "zonefile.tmp").exists()
