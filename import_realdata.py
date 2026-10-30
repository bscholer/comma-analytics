#!/usr/bin/env python3
"""Import drives from a copy of the device's realdata folder (e.g. an old backup) into the archive and InfluxDB.

  ./import_realdata.py /realdata                          # everything
  ./import_realdata.py /realdata --since 2023-04 --until 2023-05
  ./import_realdata.py /realdata --dry-run                # count what would be staged

Each <route>--<segment>/qlog (or qlog.bz2 / qlog.zst) is compressed into archive/device/ exactly as a device pull
would leave it, then ingested the same way. Already-staged segments are skipped, so it resumes after an interrupt.

Only routes named by date (older openpilot: 2022-05-13--18-46-45) can be filtered with --since/--until; routes named
by counter (00000036--03f0cd3d00) only get a date once their logs are read, so they're staged unless --dated-only.
Stop the sync daemon first: both write the same state file.
"""
import argparse
import bz2
import datetime as dt
import re
import sys
from pathlib import Path

import zstandard
from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS

from ct import device
from ct.config import ENV, load_state, secret

DATED = re.compile(r"(\d{4}-\d{2}-\d{2})--\d{2}-\d{2}-\d{2}--\d+$")
SEGMENT = re.compile(r".+--\d+$")


def qlog_bytes(seg: Path) -> bytes | None:
  for name, read in (("qlog.zst", lambda p: zstandard.ZstdDecompressor().stream_reader(open(p, "rb")).read()),
                     ("qlog", Path.read_bytes), ("qlog.bz2", lambda p: bz2.decompress(p.read_bytes()))):
    p = seg / name
    if p.exists() and p.stat().st_size:
      return read(p)
  return None


def wanted(name: str, args) -> bool:
  m = DATED.match(name)
  if not m:
    return not args.dated_only
  day = m[1]
  return (not args.since or day >= args.since) and (not args.until or day < args.until)


def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("realdata", type=Path)
  ap.add_argument("--since", help="YYYY-MM[-DD], inclusive")
  ap.add_argument("--until", help="YYYY-MM[-DD], exclusive")
  ap.add_argument("--dated-only", action="store_true", help="skip counter-named routes")
  ap.add_argument("--dry-run", action="store_true")
  args = ap.parse_args()

  segs = sorted(p for p in args.realdata.iterdir() if SEGMENT.match(p.name) and wanted(p.name, args))
  staged = skipped = 0
  zc = zstandard.ZstdCompressor(level=10)
  for seg in segs:
    out = device.DEVICE / seg.name / "qlog.zst"
    if out.exists():
      continue
    if args.dry_run:
      staged += 1
      continue
    data = qlog_bytes(seg)
    if data is None:
      skipped += 1
      continue
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    tmp.write_bytes(zc.compress(data))
    tmp.replace(out)
    staged += 1
    if staged % 500 == 0:
      print(f"{dt.datetime.now():%H:%M:%S} staged {staged}", flush=True)
  print(f"{len(segs)} segments in range, {staged} {'to stage' if args.dry_run else 'staged'}, {skipped} without a qlog",
        flush=True)
  if args.dry_run:
    return 0

  import sync
  with InfluxDBClient(url=ENV["INFLUX_URL"], token=secret("influx_token"), org=ENV["INFLUX_ORG"], timeout=60_000) as client:
    write = client.write_api(write_options=SYNCHRONOUS)
    sync.DELETE = client.delete_api()
    sync.ingest_device(write, load_state(), sync.drivers(write))
  return 0


if __name__ == "__main__":
  sys.exit(main())
