"""Live BLE probe for Beurer BF600-family scales (developed against a BF450).

Waits for the scale to advertise (someone has to step on it), optionally watches the
adverts for a while, then connects and runs the same sequence as the Beurer app:
set time -> subscribe -> read state -> user list -> [register] -> consent -> listen.
Every advert, GATT read, write and notification is logged with a timestamp, so the log
doubles as a source of test fixtures. Protocol details: docs/beurer-ble-protocol.md.

Requirements (Linux, BlueZ over the system D-Bus):
    python3 -m venv venv && venv/bin/pip install bleak      # bleak pulls in dbus-fast
    bluetoothctl pairable on    # the scale drops unbonded links ~0.5 s after connecting
The script registers its own NoInputNoOutput agent, so Just Works bonding is accepted
automatically. After a factory reset of the scale, remove the stale bond first:
    bluetoothctl remove <MAC>

Examples:
    probe.py runs/a --observe 240 --no-connect           # passive: advert timeline only
    probe.py runs/b --pair --create --profile 1990-01-01,180,m,3
    probe.py runs/c --pair --idx 1 --code 1234 --take-if-empty
    probe.py runs/d --pair --idx 1 --code 1234 --bad-consent --temp-delete --post-scan 90

Writes <outdir>/log.txt and <outdir>/adv.jsonl. Users created by --create/--temp-delete
are appended to --users (default: users.json next to <outdir>). Keep that file: without
the consent code the scale will not hand out a user's measurements.
"""
import argparse, asyncio, datetime, json, os, random, time
from bleak import BleakScanner, BleakClient
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method
from dbus_fast import BusType, Message as DMsg

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("outdir")
p.add_argument("--name", default="BF450", help="advertised local-name prefix to wait for")
p.add_argument("--wait", type=float, default=300, help="seconds to wait for the scale")
p.add_argument("--observe", type=float, default=0, help="log adverts this long before connecting")
p.add_argument("--no-connect", action="store_true", help="only scan, never connect")
p.add_argument("--pair", action="store_true", help="bond while connecting (required by the BF450)")
p.add_argument("--create", action="store_true", help="register a new user with a random consent code")
p.add_argument("--profile", default=None,
               help="profile for --create: YYYY-MM-DD,height_cm,m|f,activity(1-5); "
                    "default: copy the first user-list entry, else 1990-01-01,180,m,3")
p.add_argument("--idx", type=int, help="user index (slot) to consent as")
p.add_argument("--code", type=int, help="consent code for --idx")
p.add_argument("--take", action="store_true", help="send take-measurement (FFF4 00) after consent")
p.add_argument("--take-if-empty", action="store_true",
               help="send take-measurement only if no stored reading arrives within 6 s")
p.add_argument("--bad-consent", action="store_true",
               help="before consenting, try a wrong code and an absent slot (7) and log the answers")
p.add_argument("--temp-delete", action="store_true",
               help="after listening, register a temporary user, consent, delete it (UCP 03)")
p.add_argument("--listen", type=float, default=25, help="seconds to listen after consent")
p.add_argument("--post-scan", type=float, default=0, help="scan this long after disconnecting")
p.add_argument("--users", default=None, help="file for created users (default: ../users.json)")
A = p.parse_args()

OUT = A.outdir; os.makedirs(OUT, exist_ok=True)
LOG = os.path.join(OUT, "log.txt")
ADV = os.path.join(OUT, "adv.jsonl")
USERS = A.users or os.path.join(os.path.dirname(os.path.abspath(OUT)), "users.json")

def log(*a):
    s = f"{datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]} " + " ".join(str(x) for x in a)
    print(s, flush=True)
    with open(LOG, "a") as f: f.write(s + "\n")

def save_user(**kw):
    with open(USERS, "a") as f: f.write(json.dumps(dict(t=time.time(), **kw)) + "\n")

class Agent(ServiceInterface):
    """NoInputNoOutput agent that accepts everything and logs what BlueZ asks."""
    def __init__(self): super().__init__("org.bluez.Agent1")
    @method()
    def Release(self): log("agent: Release")
    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"): log("agent: RequestConfirmation", device, passkey)
    @method()
    def RequestAuthorization(self, device: "o"): log("agent: RequestAuthorization", device)
    @method()
    def AuthorizeService(self, device: "o", uuid: "s"): log("agent: AuthorizeService", device, uuid)
    @method()
    def RequestPasskey(self, device: "o") -> "u": log("agent: RequestPasskey", device); return 0
    @method()
    def RequestPinCode(self, device: "o") -> "s": log("agent: RequestPinCode", device); return "0000"
    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"): log("agent: DisplayPasskey", passkey)
    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"): log("agent: DisplayPinCode", pincode)
    @method()
    def Cancel(self): log("agent: Cancel")

async def setup_bus(watch):
    """Register the pairing agent and log Device1 connection changes and disconnect reasons.

    watch: dict that gets the target's D-Bus path under "path" once the scale is seen."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    bus.export("/probe/agent", Agent())
    for m, sig, body in (("RegisterAgent", "os", ["/probe/agent", "NoInputNoOutput"]),
                         ("RequestDefaultAgent", "o", ["/probe/agent"])):
        r = await bus.call(DMsg(destination="org.bluez", path="/org/bluez", interface="org.bluez.AgentManager1",
                                member=m, signature=sig, body=body))
        log("agent", m, r.message_type.name, r.body)
    for rule in ("type='signal',sender='org.bluez',interface='org.bluez.Device1',member='Disconnected'",
                 "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.Properties',"
                 "member='PropertiesChanged',path_namespace='/org/bluez'"):
        await bus.call(DMsg(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                            interface="org.freedesktop.DBus", member="AddMatch", signature="s", body=[rule]))
    def on_msg(m):
        if m.path != watch.get("path"):
            return
        if m.member == "Disconnected":
            log("DISCONNECTED signal", m.body)
        elif m.member == "PropertiesChanged" and m.body and m.body[0] == "org.bluez.Device1":
            ch = {k: v.value for k, v in m.body[1].items() if k not in ("RSSI", "ManufacturerData", "ServiceData", "TxPower")}
            if ch: log("dev props", ch)
    bus.add_message_handler(on_msg)
    return bus

def u(x): return f"0000{x:04x}-0000-1000-8000-00805f9b34fb"

# BF450 characteristic map (custom service FFFF); see docs §1 and §9.1.
NAMES = {0x2A9D: "WEIGHT", 0x2A9C: "BODY", 0xFFF5: "CUST1", 0xFFF6: "CUST2", 0x2A9F: "UCP",
         0xFFF1: "USERLIST", 0xFFF4: "TAKE", 0xFFF8: "FFF8", 0x2A99: "DBINC", 0x2A2B: "TIME"}
SUBS = [0x2A9D, 0x2A9C, 0xFFF5, 0xFFF6, 0xFFF1, 0xFFF4, 0xFFF8, 0x2A99, 0x2A2B, 0x2A9F]
READS = [0x2A19, 0x2A2B, 0x2A9E, 0x2A9B, 0xFFF0, 0xFFF2, 0xFFF3, 0x2A9A, 0x2A99, 0x2A85, 0x2A8C, 0x2A8E]

queues = {k: asyncio.Queue() for k in NAMES}
def mk_cb(k):
    def cb(_ch, data: bytearray):
        log(f"<< {NAMES[k]:8s} {k:04X} [{len(data)}] {bytes(data).hex(' ')}")
        queues[k].put_nowait(bytes(data))
    return cb

async def safe(coro, what):
    try:
        return await coro
    except Exception as e:
        log(f"!! {what}: {e!r}"); return None

async def write(c, k, data, what=""):
    log(f">> {NAMES.get(k, hex(k))} {k:04X} {bytes(data).hex(' ')} {what}")
    return await safe(c.write_gatt_char(u(k), bytes(data), response=True), f"write {k:04X}")

async def read_log(c, k):
    v = await safe(c.read_gatt_char(u(k)), f"read {k:04X}")
    if v is not None: log(f"RD {k:04X} [{len(v)}] {bytes(v).hex(' ')}")

async def ucp(c, payload, timeout=8):
    while not queues[0x2A9F].empty(): queues[0x2A9F].get_nowait()
    await write(c, 0x2A9F, payload, "UCP")
    try: return await asyncio.wait_for(queues[0x2A9F].get(), timeout)
    except asyncio.TimeoutError: log("!! UCP timeout"); return None

def le16(v): return [v & 0xFF, v >> 8]

async def user_list(c, what="user list"):
    while not queues[0xFFF1].empty(): queues[0xFFF1].get_nowait()
    await write(c, 0xFFF1, [0x00], what)
    users = []
    try:
        while True:
            d = await asyncio.wait_for(queues[0xFFF1].get(), 4)
            if len(d) == 1: log("user list end", d[0], "(1 = complete, 2 = empty)"); break
            users.append(d)
    except asyncio.TimeoutError: log("user list: timeout")
    for d in users:
        if len(d) >= 12:
            log(f"USER idx={d[1]} init={d[2:5]!r} born={int.from_bytes(d[5:7],'little')}-{d[7]}-{d[8]} "
                f"h={d[9]} g={d[10]} act={d[11]}")
    return users

def profile(users):
    if A.profile:
        dob, h, g, act = A.profile.split(",")
        y, mo, dy = (int(x) for x in dob.split("-"))
        return y, mo, dy, int(h), 1 if g.lower().startswith("f") else 0, int(act)
    src = next((d for d in users if len(d) >= 12), None)
    if src:
        return int.from_bytes(src[5:7], "little"), src[7], src[8], src[9], src[10], src[11]
    return 1990, 1, 1, 180, 0, 3

async def session(dev):
    t0 = time.time()
    async with BleakClient(dev, timeout=30, pair=A.pair) as c:
        log(f"connected {time.time()-t0:.2f}s")
        n = datetime.datetime.now()
        await write(c, 0x2A2B, [*le16(n.year), n.month, n.day, n.hour, n.minute, n.second, 0, 0, 0], "set time")
        for k in SUBS:
            await safe(c.start_notify(u(k), mk_cb(k)), f"subscribe {k:04X}")
        log("subscribed")
        for k in READS:
            await read_log(c, k)
        users = await user_list(c)
        idx, code = A.idx, A.code
        if A.create:
            code = random.randint(1111, 9998)
            r = await ucp(c, [0x01, *le16(code)])
            log("register resp", r and r.hex(" "))
            if r and len(r) >= 4 and r[0] == 0x20 and r[2] == 1:
                idx = r[3]
                save_user(idx=idx, code=code, addr=dev.address)
                log(f"CREATED idx={idx} code={code}")
        if A.bad_consent and idx is not None:
            bad = code + 1 if code < 9998 else code - 1
            r = await ucp(c, [0x02, idx, *le16(bad)]); log("PROBE wrong code resp", r and r.hex(" "))
            r = await ucp(c, [0x02, 7, *le16(code)]); log("PROBE slot 7 (absent) resp", r and r.hex(" "))
        if idx is not None:
            r = await ucp(c, [0x02, idx, *le16(code)])
            log("consent resp", r and r.hex(" "))
            if A.create and r and r[-1] == 1:
                yr, mo, dy, h, g, act = profile(users)
                await write(c, 0x2A85, [*le16(yr), mo, dy], "dob")
                await write(c, 0x2A99, [1, 0, 0, 0], "db inc")
                await write(c, 0x2A8C, [g], "gender")
                await write(c, 0x2A8E, [h, 0], "height")
                await write(c, 0xFFF2, [act], "activity")
                for k in (0x2A85, 0x2A8C, 0x2A8E, 0xFFF2, 0x2A9A):
                    await read_log(c, k)
            if A.take_if_empty:
                try:
                    await asyncio.wait_for(queues[0x2A9D].get(), 6)
                    log("stored measurement delivered; no take needed")
                except asyncio.TimeoutError:
                    log("TAKE-FALLBACK: nothing stored -> take measurement, step on again")
                    await write(c, 0xFFF4, [0x00], "take measurement")
            elif A.take:
                await write(c, 0xFFF4, [0x00], "take measurement")
        log(f"listening {A.listen}s")
        end = time.time() + A.listen
        while time.time() < end and c.is_connected:
            await asyncio.sleep(0.5)
        log("connected" if c.is_connected else "scale disconnected")
        if A.temp_delete and c.is_connected:
            tcode = random.randint(1111, 9998)
            r = await ucp(c, [0x01, *le16(tcode)]); log("TEMP register resp", r and r.hex(" "), "code", tcode)
            if r and len(r) >= 4 and r[2] == 1:
                tidx = r[3]
                save_user(idx=tidx, code=tcode, temp=True)
                r = await ucp(c, [0x02, tidx, *le16(tcode)]); log("TEMP consent resp", r and r.hex(" "))
                r = await ucp(c, [0x03]); log("TEMP delete resp", r and r.hex(" "))
                save_user(deleted_idx=tidx, resp=r and r.hex())
                r = await ucp(c, [0x02, tidx, *le16(tcode)]); log("TEMP consent-after-delete resp", r and r.hex(" "))
            await user_list(c, "user list after delete")
    log("session end")

def adverts():
    try:
        with open(ADV) as f: return [json.loads(l) for l in f]
    except FileNotFoundError:
        return []

async def main():
    watch = {}
    await setup_bus(watch)
    found = asyncio.Event(); tgt = {}
    def cb(dev, ad):
        if not (ad.local_name or dev.name or "").startswith(A.name):
            return
        sd = {k[4:8]: v.hex() for k, v in ad.service_data.items()}
        md = {f"{k:04x}": v.hex() for k, v in ad.manufacturer_data.items()}
        # RSSI -127 = BlueZ replaying its cache when scanning starts, not a live advert
        if ad.rssi > -127 and not found.is_set():
            log("seen", dev.address, sd, md, ad.rssi)
            tgt["d"] = dev; found.set()
            if isinstance(dev.details, dict) and "path" in dev.details:
                watch["path"] = dev.details["path"]
        with open(ADV, "a") as f:
            f.write(json.dumps(dict(t=round(time.time(), 3), sd=sd, md=md, rssi=ad.rssi)) + "\n")
    sc = BleakScanner(cb, scanning_mode="active"); await sc.start(); log("waiting for scale")
    try: await asyncio.wait_for(found.wait(), A.wait)
    except asyncio.TimeoutError: log("not seen"); await sc.stop(); return
    if A.observe:
        log(f"observing adverts {A.observe}s before connecting"); await asyncio.sleep(A.observe)
        log("adverts seen:", len(adverts()))
    await sc.stop()
    if A.no_connect:
        return
    for attempt in range(3):
        try:
            await session(tgt["d"]); break
        except Exception as e:
            log(f"!! session attempt {attempt}: {e!r}")
            await asyncio.sleep(1)
    if A.post_scan:
        n0 = len(adverts())
        await sc.start(); log(f"post-scan {A.post_scan}s"); await asyncio.sleep(A.post_scan); await sc.stop()
        rows = adverts()[n0:]
        log(f"post-scan adverts: {len(rows)}", "flags:", sorted({json.dumps(r['sd']) for r in rows}),
            "last:", rows[-1]['t'] if rows else None)

asyncio.run(main())
