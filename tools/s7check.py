"""Read-only S7 check, independent of ProtoProbe. Usage:  python s7check.py <PLC-IP> [rack] [slot] [db_number]
   python s7check.py <PLC-IP> [rack] [slot] probe      -> tries DB1..DB60 and groups them by the PLC's answer
Needs: pip install python-snap7==3.3.1.  Prints what the PLC answers for connect, CPU state, inputs (I), outputs (Q), markers (M) and one DB.
Writes nothing to the PLC."""
import sys
import snap7
from snap7.type import Area


def attempt(label, fn):
    try:
        data = fn()
        text = data if isinstance(data, str) else bytes(data).hex(" ")
        print(f"  OK     {label}: {text}")
        return True
    except Exception as exc:
        print(f"  FAILED {label}: {exc}")
        return False


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    ip = sys.argv[1]
    rack = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    slot = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    probe = len(sys.argv) > 4 and sys.argv[4].lower() == "probe"
    db = 1 if probe or len(sys.argv) <= 4 else int(sys.argv[4])
    print(f"snap7 {getattr(snap7, '__version__', '?')}  PLC {ip}  rack {rack} slot {slot}")
    client = snap7.client.Client()
    try:
        client.connect(ip, rack, slot)
    except Exception as exc:
        print(f"CONNECT FAILED: {exc}")
        print("  -> network/IP wrong, firewall on port 102, or rack/slot wrong (S7-1200/1500: rack 0, slot 1; S7-300: slot 2).")
        return 1
    print("CONNECTED")
    attempt("CPU state", lambda: str(client.get_cpu_state()))
    attempt("CPU info", lambda: str(client.get_cpu_info()))
    if probe:
        groups = {}
        for n in range(1, 61):
            try:
                client.read_area(Area.DB, n, 0, 2)
                groups.setdefault("readable", []).append(n)
            except Exception as exc:
                groups.setdefault(str(exc), []).append(n)
        client.disconnect()
        print("DB1..DB60 grouped by the PLC's answer:")
        for answer, dbs in groups.items():
            print(f"  {answer}: DB {dbs if len(dbs) < 12 else str(dbs[:3]) + ' ... ' + str(dbs[-2:]) + ' (' + str(len(dbs)) + ' DBs)'}")
        print("'Object does not exist' = no such DB. A different error for a DB you know exists = the DB is there but the read is refused "
              "(the DB is empty = size 0, or optimized block access, or protection).")
        return 0
    print("Reads (2 bytes each):")
    ok = [
        attempt("Inputs  I0  (PE)", lambda: client.read_area(Area.PE, 0, 0, 2)),
        attempt("Outputs Q0  (PA)", lambda: client.read_area(Area.PA, 0, 0, 2)),
        attempt("Markers M0  (MK)", lambda: client.read_area(Area.MK, 0, 0, 2)),
        attempt(f"DB{db} byte 0..1", lambda: client.read_area(Area.DB, db, 0, 2)),
    ]
    client.disconnect()
    print()
    if not any(ok):
        print("Connected but EVERY read failed: usually 'Permit access with PUT/GET' is off (CPU properties > Protection & Security > Connection mechanisms), "
              "or the CPU access level needs 'Full access (no protection)'. Download the change to the CPU.")
    elif not ok[3]:
        print("I/Q/M work but the DB does not: wrong DB number, an EMPTY DB (no variables = size 0, so bytes 0-1 do not exist), "
              "or the DB has 'Optimized block access' on.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
