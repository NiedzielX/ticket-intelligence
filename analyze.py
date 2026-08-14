#!/usr/bin/env python3
"""
Quick analysis of captured Roboticket snapshots.
"""

import argparse
import sqlite3
from collections import defaultdict

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="legia_inventory.sqlite")
    p.add_argument("--event-id", type=int, required=True)
    args = p.parse_args()

    con = sqlite3.connect(args.db)

    print("\nSNAPSHOTS")
    rows = con.execute(
        """SELECT s.snapshot_id, s.captured_at_utc,
                  COUNT(DISTINCT o.seat_id) occ_seats,
                  COUNT(DISTINCT m.seat_id) my_seats
           FROM snapshots s
           LEFT JOIN seat_occupancy o ON o.snapshot_id=s.snapshot_id
           LEFT JOIN my_seats m ON m.snapshot_id=s.snapshot_id
           WHERE s.event_id=?
           GROUP BY s.snapshot_id, s.captured_at_utc
           ORDER BY s.captured_at_utc""",
        (args.event_id,),
    ).fetchall()

    for snapshot_id, ts, occ_n, my_n in rows:
        print(f"{ts}  {snapshot_id}  occupancy={occ_n:,}  my={my_n:,}")

    print("\nOCC DISTRIBUTION BY SNAPSHOT")
    for snapshot_id, ts, _, _ in rows:
        dist = con.execute(
            """SELECT occ, COUNT(DISTINCT seat_id)
               FROM seat_occupancy
               WHERE event_id=? AND snapshot_id=?
               GROUP BY occ ORDER BY occ""",
            (args.event_id, snapshot_id),
        ).fetchall()
        print(ts, dict(dist))

    if len(rows) >= 2:
        a = rows[-2][0]
        b = rows[-1][0]
        print(f"\nCHANGES: {a} -> {b}")

        q = """
        WITH A AS (
          SELECT seat_id, occ FROM seat_occupancy
          WHERE event_id=? AND snapshot_id=?
        ),
        B AS (
          SELECT seat_id, occ FROM seat_occupancy
          WHERE event_id=? AND snapshot_id=?
        )
        SELECT A.occ, B.occ, COUNT(*)
        FROM A JOIN B USING(seat_id)
        WHERE COALESCE(A.occ,-999) <> COALESCE(B.occ,-999)
        GROUP BY A.occ, B.occ
        ORDER BY 1,2
        """
        changes = con.execute(q, (args.event_id, a, args.event_id, b)).fetchall()
        if not changes:
            print("No occ changes among seats present in both snapshots.")
        else:
            for old, new, count in changes:
                print(f"occ {old} -> {new}: {count:,}")

        q2 = """
        WITH A AS (
          SELECT seat_id, occ FROM seat_occupancy
          WHERE event_id=? AND snapshot_id=?
        ),
        B AS (
          SELECT seat_id, occ FROM seat_occupancy
          WHERE event_id=? AND snapshot_id=?
        )
        SELECT s.sector_id, s.row_label, s.seat_label, A.seat_id, A.occ, B.occ
        FROM A JOIN B USING(seat_id)
        LEFT JOIN seats s ON s.event_id=? AND s.seat_id=A.seat_id
        WHERE COALESCE(A.occ,-999) <> COALESCE(B.occ,-999)
        ORDER BY s.sector_id, s.row_label, s.seat_label
        LIMIT 100
        """
        details = con.execute(q2, (args.event_id, a, args.event_id, b, args.event_id)).fetchall()
        if details:
            print("\nFIRST CHANGED SEATS")
            for r in details:
                print(r)

if __name__ == "__main__":
    main()
