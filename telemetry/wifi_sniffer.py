"""
wifi_sniffer.py  (version 2)
Direct Remote ID (ASTM F3411 / OpenDroneID) receiver for Wi-Fi frames.

Data flow:
  MikroTik sniffer --UDP (TZSP, port 37008)--> this module --> process_telemetry() in app.py

New in version 2 (for the tests with real DJI drones):
  * the TZSP header is parsed (frame start and RSSI tag)
  * the beacon information elements are listed (SSID, channel, all vendor IEs),
    so unknown formats can be studied from a recording
  * ASTM Remote ID is decoded from the vendor IE (OUI FA-0B-BC, type 0D)
  * fallback: a message pack is searched in the whole frame (for example in a NAN action frame)
  * a callback hook (CAPTURE_HOOK) gives every UDP packet to a recorder (see rid_probe.py)

The interface to app.py is the same as before:
  last_wifi_recv_time, start_wifi_sniffer(callback_fn), parse_opendroneid_payload(data)
"""
import socket
import struct
import time

UDP_IP = "0.0.0.0"
UDP_PORT = 37008

# Used only when the drone sends no valid "height" field
GROUND_BASELINE_WGS84 = 15.0

ASTM_OUI = b"\xfa\x0b\xbc"
ODID_OUI_TYPE = 0x0D
MSG_BASIC_ID, MSG_LOCATION, MSG_AUTH, MSG_SELF_ID, MSG_SYSTEM, MSG_OPERATOR_ID, MSG_PACK = 0, 1, 2, 3, 4, 5, 0xF
MSG_SIZE = 25

MANUFACTURER_PREFIX = {"1581": "DJI"}      # first characters of a CTA-2063-A serial number
KNOWN_OUIS = {                                # for readable reports
    "fa0bbc": "ASTM Remote ID",
    "263712": "DJI (proprietary)",
    "506f9a": "Wi-Fi Alliance (NAN)",
}

last_wifi_recv_time = 0
CAPTURE_HOOK = None          # function(data: bytes, addr) called for every UDP packet
_state = {}                  # last known identity data per transmitter MAC


# ---------------------------------------------------------------- packet framing
def split_packet(data):
    """Split a UDP payload from the MikroTik sniffer.
    Returns (frame, tags): frame = the 802.11 frame (bytes) or None, tags = {tag_id: bytes}."""
    tags = {}
    off = 0
    if len(data) >= 5 and data[0] == 1 and data[1] <= 5:          # TZSP header
        off = 4
        while off < len(data):
            tag = data[off]
            if tag == 1:                  # END
                off += 1
                break
            if tag == 0:                  # padding
                off += 1
                continue
            if off + 1 >= len(data):
                return None, tags
            ln = data[off + 1]
            tags[tag] = data[off + 2:off + 2 + ln]
            off += 2 + ln
    if len(data) > off + 4 and data[off] == 0 and data[off + 1] == 0:   # radiotap header
        off += struct.unpack_from("<H", data, off + 2)[0]
    if len(data) > off + 24 and (data[off] & 0x03) == 0:                # 802.11 frame, protocol version 0
        return data[off:], tags
    return None, tags


def rssi_from_tags(tags):
    """TZSP tag 10 is the signal strength (signed byte). Check it against a second receiver before using it."""
    t = tags.get(10)
    if t:
        v = t[0]
        return v - 256 if v > 127 else v
    return None


def frame_info(frame):
    """Basic facts about a management frame: type, transmitter MAC, SSID, channel and vendor IEs."""
    info = {"type": (frame[0] >> 2) & 3, "subtype": frame[0] >> 4,
            "mac": ":".join(f"{b:02x}" for b in frame[10:16]),
            "ssid": None, "channel": None, "vendor_ies": []}
    if info["type"] != 0 or info["subtype"] not in (8, 5) or len(frame) < 36:      # beacon or probe response
        return info
    pos = 36
    while pos + 2 <= len(frame):
        tag, ln = frame[pos], frame[pos + 1]
        body = frame[pos + 2:pos + 2 + ln]
        if len(body) < ln:
            break
        if tag == 0:
            info["ssid"] = body.decode("ascii", errors="replace")
        elif tag == 3 and ln >= 1:
            info["channel"] = body[0]
        elif tag == 221 and ln >= 3:
            info["vendor_ies"].append({
                "oui": body[:3].hex(),
                "type": body[3] if ln >= 4 else None,
                "len": ln,
                "body": body[4:] if ln >= 4 else b"",
            })
        pos += 2 + ln
    return info


# ---------------------------------------------------------------- message packs
def _extract_messages(body):
    """Messages of an ASTM vendor IE body: [counter] + pack header (F?, 25, n) + n messages."""
    for k in (1, 0):
        if len(body) >= k + 3 and (body[k] >> 4) == MSG_PACK and body[k + 1] == MSG_SIZE:
            n = body[k + 2]
            start = k + 3
            if n > 0 and start + n * MSG_SIZE <= len(body):
                return [body[start + i * MSG_SIZE:start + (i + 1) * MSG_SIZE] for i in range(n)]
    return []


def scan_message_packs(buf):
    """Fallback: look for a message pack header anywhere in the bytes (NAN frames, unknown wrappers)."""
    for i in range(len(buf) - 3):
        if (buf[i] >> 4) == MSG_PACK and (buf[i] & 0x0F) <= 3 and buf[i + 1] == MSG_SIZE:
            n = buf[i + 2]
            if 1 <= n <= 9 and i + 3 + n * MSG_SIZE <= len(buf):
                msgs = [buf[i + 3 + j * MSG_SIZE:i + 3 + (j + 1) * MSG_SIZE] for j in range(n)]
                if all((m[0] >> 4) in (0, 1, 2, 3, 4, 5) and (m[0] & 0x0F) <= 3 for m in msgs):
                    return msgs
    return []


def _find_odid_ies(data):
    """Last resort when the frame start is unknown: search the ASTM signature in the raw bytes."""
    sig = ASTM_OUI + bytes([ODID_OUI_TYPE])
    pos = data.find(sig)
    while pos != -1:
        if pos >= 2 and data[pos - 2] == 0xDD:
            end = pos + data[pos - 1]
            if end <= len(data):
                yield data[pos + 4:end]
        pos = data.find(sig, pos + 1)


# ---------------------------------------------------------------- message decoders
def _text(raw):
    return raw.split(b"\x00")[0].decode("ascii", errors="ignore").strip()


def decode_messages(msgs):
    """Decode Basic ID (0), Location (1), System (4) and Operator ID (5) messages."""
    d = {}
    for m in msgs:
        t = m[0] >> 4
        try:
            if t == MSG_BASIC_ID:
                d["uas_id"] = _text(m[2:22])
                d["id_type"] = m[1] >> 4
                d["ua_type"] = m[1] & 0x0F
            elif t == MSG_LOCATION:
                flags = m[1]
                mult, ew, status = flags & 1, (flags >> 1) & 1, flags >> 4
                direction = m[2] + 180 if ew else m[2]
                d["heading"] = direction if direction <= 359 else None
                if mult == 1 and m[3] == 255:
                    d["speed"] = None
                else:
                    d["speed"] = m[3] * 0.75 + 255 * 0.25 if mult else m[3] * 0.25
                d["status"] = status
                d["latitude"] = struct.unpack_from("<i", m, 5)[0] / 1e7
                d["longitude"] = struct.unpack_from("<i", m, 9)[0] / 1e7
                for name, off in (("pressure_altitude", 13), ("geodetic_altitude", 15), ("height", 17)):
                    raw = struct.unpack_from("<H", m, off)[0]
                    d[name] = None if raw == 0 else raw * 0.5 - 1000.0
            elif t == MSG_SYSTEM:
                d["operator_latitude"] = struct.unpack_from("<i", m, 2)[0] / 1e7
                d["operator_longitude"] = struct.unpack_from("<i", m, 6)[0] / 1e7
            elif t == MSG_OPERATOR_ID:
                d["operator_id"] = _text(m[2:22])
        except struct.error:
            continue
    return d


def _valid_position(lat, lon):
    return (lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180
            and not (lat == 0 and lon == 0))


def analyze_packet(data):
    """Study one UDP packet. Returns (info, found) where
       info  = {"mac", "ssid", "channel", "rssi", "vendor_ies", ...}
       found = list of (method, decoded_dict) for every Remote ID message pack that was found."""
    frame, tags = split_packet(data)
    info = {"mac": "unknown", "ssid": None, "channel": None, "subtype": None, "vendor_ies": [],
            "rssi": rssi_from_tags(tags), "has_frame": frame is not None}
    found = []
    if frame is not None:
        info.update(frame_info(frame))
        for ie in info["vendor_ies"]:
            if ie["oui"] == ASTM_OUI.hex() and ie["type"] == ODID_OUI_TYPE:
                msgs = _extract_messages(ie["body"])
                if msgs:
                    found.append(("ie", decode_messages(msgs)))
    else:
        for body in _find_odid_ies(data):
            msgs = _extract_messages(body)
            if msgs:
                found.append(("ie", decode_messages(msgs)))
    if not found:                                         # fallback: NAN or unknown wrapper
        msgs = scan_message_packs(frame if frame is not None else data)
        if msgs:
            found.append(("scan", decode_messages(msgs)))
    return info, found


def info_summary(info):
    """One readable text line about the vendor IEs of a frame."""
    parts = []
    for ie in info.get("vendor_ies", []):
        name = KNOWN_OUIS.get(ie["oui"], "unknown")
        t = "-" if ie["type"] is None else f"{ie['type']:02x}"
        parts.append(f"{ie['oui']}/{t} ({name}, {ie['len']} B)")
    return "; ".join(parts) if parts else "-"


# ---------------------------------------------------------------- telemetry for app.py
def parse_opendroneid_payload(raw_data):
    """Parse one UDP packet. Returns a telemetry dict or None (interface used by app.py)."""
    global last_wifi_recv_time
    info, found = analyze_packet(raw_data)
    mac = info["mac"]
    state = _state.setdefault(mac, {})
    result = None
    for method, d in found:
        for key in ("uas_id", "operator_id", "operator_latitude", "operator_longitude"):
            if d.get(key):
                state[key] = d[key]
        if not _valid_position(d.get("latitude"), d.get("longitude")):
            continue
        if d.get("height") is not None:
            altitude = max(0.0, d["height"])
        elif d.get("geodetic_altitude") is not None:
            altitude = max(0.0, d["geodetic_altitude"] - GROUND_BASELINE_WGS84)
        else:
            altitude = 0.0
        uas_id = state.get("uas_id") or (info["ssid"][4:] if (info["ssid"] or "").startswith("RID-") else "UNKNOWN")
        model = next((n for p, n in MANUFACTURER_PREFIX.items() if uas_id.startswith(p)), "Unknown")
        last_wifi_recv_time = time.time()
        result = {
            "source": "wifi_remote_id",
            "device_id": uas_id,
            "model": model,
            "latitude": round(d["latitude"], 6),
            "longitude": round(d["longitude"], 6),
            "altitude": round(altitude, 1),
            "speed": round(d["speed"], 1) if d.get("speed") is not None else 0.0,
            "heading": int(d["heading"]) if d.get("heading") is not None else 0,
            "timestamp": last_wifi_recv_time,
            "operator_id": state.get("operator_id", ""),
            "operator_latitude": state.get("operator_latitude"),
            "operator_longitude": state.get("operator_longitude"),
            "tx_mac": mac,
            "rssi": info["rssi"],
            "decode_method": method,
        }
    return result


def start_wifi_sniffer(callback_fn=None):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((UDP_IP, UDP_PORT))
    print(f"[*] Wi-Fi Sniffer Active on UDP Port {UDP_PORT}...")
    while True:
        try:
            data, addr = sock.recvfrom(4096)
            if CAPTURE_HOOK:
                CAPTURE_HOOK(data, addr)
            telemetry = parse_opendroneid_payload(data)
            if telemetry and callback_fn:
                print(f"[+] Remote ID: {telemetry['device_id']}  Lat={telemetry['latitude']}, "
                      f"Lon={telemetry['longitude']}, Alt={telemetry['altitude']} m, RSSI={telemetry['rssi']}")
                callback_fn(telemetry)
        except Exception as e:
            print(f"[!] Wi-Fi Sniffer Error: {e}")
