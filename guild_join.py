# -*- coding: utf-8 -*-
"""
GUILD AUTO-JOIN BOT
===================
Guest UID + Password diye login kore, guild e join request pathay.
Kono match khela nei, kono TCP session nei — shudhu login + RequestJoinClan.

Usage:
    python guild_joiner.py

accounts.json format:
[
    {"uid": "123456789", "password": "abc123"},
    {"uid": "987654321", "password": "xyz789"}
]
"""

import asyncio
import json
import os
import random
import sys
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

try:
    from google_play_scraper import app as play_scraper
except ImportError:
    play_scraper = None

try:
    import thunderFF_pb2
except ImportError:
    thunderFF_pb2 = None


# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════
ACCOUNTS_FILE = "accounts.json"
FORCED_REGION = "IND"

# Guild retry
GUILD_JOIN_RETRIES = 5
GUILD_JOIN_RETRY_DELAY = 6.0

# Login retry
LOGIN_MAX_RETRIES = 3
LOGIN_RETRY_DELAY = 10.0

# AES key/iv (game client HTTP payload)
AES_KEY = b'Yg&tc%DEuh6%Zc^8'
AES_IV = b'6oyZDr22E3ychjM%'

# Fallback guild endpoints
FALLBACK_GUILD_URLS = [
    "https://clientbp.common.ggbluefox.com/RequestJoinClan",
    "https://client.ind.freefiremobile.com/RequestJoinClan",
    "https://clientbp.ggblueshark.com/RequestJoinClan",
]

# HTTP headers
HEADERS = {
    'User-Agent': 'UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)',
    'Connection': 'Keep-Alive',
    'Accept-Encoding': 'gzip',
    'Content-Type': 'application/x-www-form-urlencoded',
    'Expect': '100-continue',
    'X-Unity-Version': '2018.4.12f1',
    'X-GA-SV': '1789535859',
    'X-GA': 'v1 1',
    'ReleaseVersion': 'OB55',
}

client = httpx.AsyncClient(
    verify=False,
    timeout=15.0,
    limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
)


# ═══════════════════════════════════════════════════════════════
#  COLORS / LOG
# ═══════════════════════════════════════════════════════════════
class Colors:
    GREEN = '\033[92m'
    FAIL = '\033[91m'
    WARNING = '\033[93m'
    CYAN = '\033[96m'
    MAGENTA = '\033[95m'
    WHITE = '\033[97m'
    ENDC = '\033[0m'


def log(msg, color=Colors.WHITE):
    try:
        print(f"{color}{msg}{Colors.ENDC}")
    except Exception:
        pass


def log_ok(msg):
    log(f"[+] {msg}", Colors.GREEN)


def log_err(msg):
    log(f"[-] {msg}", Colors.FAIL)


def log_warn(msg):
    log(f"[!] {msg}", Colors.WARNING)


def log_info(msg):
    log(f"[i] {msg}", Colors.CYAN)


# ═══════════════════════════════════════════════════════════════
#  PROTOBUF HELPERS
# ═══════════════════════════════════════════════════════════════
def encode_varint(n: int) -> bytes:
    if n < 0:
        return b''
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            break
    return bytes(out)


def build_proto(fields: dict) -> bytes:
    parts = []
    for k, v in fields.items():
        if isinstance(v, dict):
            nested = build_proto(v)
            parts.append(encode_varint((k << 3) | 2) + encode_varint(len(nested)) + nested)
        elif isinstance(v, int):
            parts.append(encode_varint((k << 3) | 0) + encode_varint(v))
        elif isinstance(v, (str, bytes)):
            ev = v.encode() if isinstance(v, str) else v
            parts.append(encode_varint((k << 3) | 2) + encode_varint(len(ev)) + ev)
    return b''.join(parts)


def encrypt_api_bytes(plain_bytes: bytes) -> bytes:
    return AES.new(AES_KEY, AES.MODE_CBC, AES_IV).encrypt(pad(plain_bytes, AES.block_size))


def encrypt_api_hex(plain_hex: str) -> str:
    return encrypt_api_bytes(bytes.fromhex(plain_hex)).hex()


# ═══════════════════════════════════════════════════════════════
#  GUILD JOIN
# ═══════════════════════════════════════════════════════════════
def _guild_payload_formats(guild_id) -> List[str]:
    """Different protobuf formats for RequestJoinClan."""
    gid = int(str(guild_id).strip())
    raw = str(gid).encode("utf-8")
    return [
        f"08{encode_varint(gid).hex()}",                          # field 1, varint
        f"0a{encode_varint(len(raw)).hex()}{raw.hex()}",           # field 1, string
        f"12{encode_varint(len(raw)).hex()}{raw.hex()}",           # field 2, string
        f"10{encode_varint(gid).hex()}",                          # field 2, varint
    ]


def _guild_headers(token: str, release_version: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 11; ASUS_Z01QD Build/PI)",
        "X-Unity-Version": "2018.4.11f1",
        "X-GA": "v1 1",
        "ReleaseVersion": release_version or "OB55",
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
    }


def _guild_urls(server_url: Optional[str]) -> List[str]:
    urls = []
    if server_url:
        base = str(server_url).rstrip("/")
        if base:
            urls.append(f"{base}/RequestJoinClan")
    urls += [u for u in FALLBACK_GUILD_URLS if u not in urls]
    return urls


async def join_guild(token: str, guild_id, server_url: Optional[str],
                     release_version: Optional[str], label: str = "") -> Tuple[bool, str]:
    """Try every payload format against every endpoint."""
    if not token:
        return False, "no token"
    if not guild_id:
        return False, "no guild id"

    urls = _guild_urls(server_url)
    formats = _guild_payload_formats(guild_id)
    last = "no attempt"

    for idx, plain in enumerate(formats, 1):
        body = encrypt_api_bytes(bytes.fromhex(plain))
        hdrs = _guild_headers(token, release_version)
        for url in urls:
            try:
                r = await client.post(url, content=body, headers=hdrs)
                host = url.split("//")[1][:38]
                detail = (f"[GUILD] {label} fmt#{idx} -> {host} | "
                          f"guild {guild_id} | HTTP {r.status_code} | {len(r.content)}B")
                if r.status_code in (200, 201):
                    log_ok(detail + "  ✅ JOIN ACCEPTED")
                    return True, detail
                last = detail
                log_info(detail)
                if r.status_code in (401, 403):
                    last = f"{detail} | token rejected"
                    break
            except Exception as e:
                last = f"[GUILD] {label} fmt#{idx} -> {url[:48]} error: {e}"
                log_err(last)
    return False, last


# ═══════════════════════════════════════════════════════════════
#  LOGIN FLOW
# ═══════════════════════════════════════════════════════════════
async def get_playstore_version() -> Optional[str]:
    if not play_scraper:
        return "1.132.9"
    try:
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(
            None, lambda: play_scraper('com.dts.freefireth', lang='hi', country='id')
        )
        return res.get("version")
    except Exception:
        return "1.132.9"


async def version_config() -> Optional[Tuple[str, str, str]]:
    """Returns (release_version, client_version, server_url)."""
    app_version = await get_playstore_version()
    api_url = (
        "https://version.ggwhitehawk.com/live/ver.php"
        f"?version={app_version}&lang=hi&device=android&channel=android"
        "&appstore=googleplay&region=IND"
        "&whitelist_version=1.3.0&whitelist_sp_version=1.0.0"
    )
    try:
        r = await client.get(api_url)
        r.raise_for_status()
        d = r.json()
        su = d.get("server_url")
        rv = d.get("remote_version")
        lr = d.get("latest_release_version")
        if not su or not rv or not lr:
            return None
        return lr, rv, su
    except Exception as e:
        log_err(f"version_config: {e}")
        return None


async def get_access_token(uid: str, password: str) -> Optional[Tuple[str, str, int]]:
    """Returns (open_id, access_token, platform)."""
    url = "https://100067.connect.garena.com/oauth/guest/token/grant"
    hdrs = {
        "Host": "100067.connect.garena.com",
        "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; SM-G998B Build/SP1A.210812.016)",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "close",
    }
    data = {
        "uid": uid,
        "password": password,
        "response_type": "token",
        "client_type": "2",
        "client_secret": "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3",
        "client_id": "100067",
    }
    for attempt in range(5):
        try:
            r = await client.post(url, headers=hdrs, data=data)
            if r.status_code == 200:
                d = r.json()
                oid = d.get("open_id")
                at = d.get("access_token")
                pf = d.get("platform", 4)
                if oid and at:
                    return oid, at, pf
            if r.status_code == 429:
                await asyncio.sleep(1)
                continue
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return None


def _get_device_for_account(account_identifier) -> dict:
    """Minimal device info for MajorLogin."""
    brand, model, gpu, os_ver = random.choice([
        ("Samsung", "SM-G998B", "Adreno (TM) 660", "Android OS 12 / API-31"),
        ("Xiaomi", "2201122G", "Adreno (TM) 730", "Android OS 13 / API-33"),
        ("Realme", "RMX3700", "Mali-G710", "Android OS 14 / API-34"),
        ("OnePlus", "CPH2451", "Adreno (TM) 740", "Android OS 13 / API-33"),
        ("OPPO", "CPH2611", "Adreno (TM) 720", "Android OS 14 / API-34"),
    ])
    return {
        "unique_device_id": f"Google|{uuid.uuid4()}",
        "brand": brand,
        "model": model,
        "gpu_renderer": gpu,
        "system_software": os_ver,
        "screen_width": random.choice([1080, 1440, 720, 1280]),
        "screen_height": random.choice([2400, 3200, 1600, 2400]),
        "screen_dpi": str(random.randint(300, 420)),
        "memory": random.randint(2800, 6500),
        "processor_details": f"ARM64 FP ASIMD AES VMH | {random.randint(2200, 3200)} | {random.randint(6, 12)}",
        "client_ip": f"{random.randint(103, 223)}.{random.randint(10, 250)}.{random.randint(10, 250)}.{random.randint(10, 250)}",
        "android_id": uuid.uuid4().hex[:16],
    }


async def build_majorlogin_payload(open_id, access_token, platform,
                                   client_version, device_info) -> Optional[bytes]:
    try:
        p = {
            3: str(datetime.now())[:-7], 4: "free fire", 5: "1",
            7: str(client_version) if client_version else "1.132.8",
            8: str(device_info.get("system_software", "Android OS 15 / API-35")),
            9: "Handheld", 10: "Airtel", 11: "CarrierDataNetwork",
            12: str(device_info.get("screen_width", 1639)),
            13: str(device_info.get("screen_height", 720)),
            14: str(device_info.get("screen_dpi", 320)),
            15: str(device_info.get("processor_details", "ARM64 FP ASIMD AES | 2000 | 8")),
            16: str(device_info.get("memory", 5653)),
            17: str(device_info.get("gpu_renderer", "Mali-G52 MC2")),
            18: "OpenGL ES 3.2 v1.r49p1-03bet0.19498e0ae1d5dac223383c39a2e58f04",
            19: str(device_info.get("unique_device_id", f"Google|{uuid.uuid4()}")),
            20: str(device_info.get("client_ip", "182.5.135.225")),
            21: "en", 22: str(open_id), 23: str(platform), 24: "Handheld",
            25: str(device_info.get("model", "Xiaomi 2409BRN2CY")), 26: "IND",
            29: str(access_token), 30: "1", 41: "Airtel", 42: "4G",
            57: "7428b253defc164018c604a1ebbfebdf", 60: "109278", 61: "55402",
            62: "868", 64: "55927", 65: "109278", 66: "55927", 67: "109278",
            73: 2,
            74: "/data/app/~~6Cw028g_ld-8Z0z2qhrOLA==/com.dts.freefireth-i3wiaEmpx8CbiRVhUMx_ag==/lib/arm64",
            76: 1,
            77: "b8e0cd5e295eee42f5860d3c86e483dd|/data/app/~~6Cw028g_ld-8Z0z2qhrOLA==/com.dts.freefireth-i3wiaEmpx8CbiRVhUMx_ag==/base.apk",
            78: 6, 79: 1, 81: "32", 83: "2019121229", 85: 3,
            86: "OpenGLES2", 87: 3071, 88: 4, 89: 14466, 93: "3rd_party",
            94: "KqsHTyb7yHjsAiyNWUUZa0DSOD6U0AhDQ76smAo1WeHHHpSmJ8KMRyRrxb2ttgJqO9YOqO/K6AORAvDMAX/qBS8+JahIkrM7kHfzlfULbgRAoZ9r",
            95: "111207", 96: "{\"cur_rate\":[60,90,120],\"support_etc2\":true}",
            97: "1", 98: "1", 99: str(platform), 100: str(platform),
            102: "D]C\x15\x05T\x0eU3", 103: "1", 104: "52837", 105: "1",
            106: "https://dl.gmc.freefiremobile.com/live/ABHotUpdates/|https://core-gmc.freefiremobile.com/live/ABHotUpdates/|211c933168f55902c7dfbfd8c4e2957d",
            107: "1.3fb8adbf45f44ce9",
        }
        return encrypt_api_bytes(build_proto(p))
    except Exception as e:
        log_err(f"build_majorlogin: {e}")
        return None


async def send_majorlogin(data, release_version, server_url):
    """Returns MajorLoginRes protobuf or None."""
    if thunderFF_pb2 is None:
        log_err("thunderFF_pb2 missing — cannot do MajorLogin")
        return None
    try:
        url = f"{server_url}MajorLogin" if server_url.endswith('/') else f"{server_url}/MajorLogin"
        hdrs = HEADERS.copy()
        hdrs["ReleaseVersion"] = str(release_version)
        r = await client.post(url, headers=hdrs, data=data)
        if r.status_code != 200:
            log_err(f"MajorLogin HTTP {r.status_code}")
            return None
        c = r.content
        if len(c) < 40:
            log_err(f"MajorLogin response too short ({len(c)}B)")
            return None
        # Multi-offset parse
        for off in [0, 64] + list(range(128)):
            try:
                m = thunderFF_pb2.MajorLoginRes()
                m.ParseFromString(c[off:])
                if m.region and m.token:
                    return m
            except Exception:
                continue
        return None
    except Exception as e:
        log_err(f"send_majorlogin: {e}")
        return None


async def send_getlogin(data, base_url, token, release_version):
    """Returns (GetLoginDataRes, dict) or None."""
    if thunderFF_pb2 is None:
        return None
    try:
        url = f"{base_url.rstrip('/')}/GetLoginData"
        hdrs = HEADERS.copy()
        hdrs["ReleaseVersion"] = release_version
        hdrs['Authorization'] = f"Bearer {token}"
        hdrs['Host'] = "clientbp.ppmainecoonghj.com"
        r = await client.post(url, headers=hdrs, data=data)
        if r.status_code != 200:
            log_err(f"GetLoginData HTTP {r.status_code}")
            return None
        c = r.content
        rp = thunderFF_pb2.GetLoginDataRes()
        ok = False
        try:
            rp.ParseFromString(c)
            if rp.functional_addrs or rp.informational_addrs:
                ok = True
        except Exception:
            pass
        if not ok:
            for off in range(min(128, len(c))):
                try:
                    cand = thunderFF_pb2.GetLoginDataRes()
                    cand.ParseFromString(c[off:])
                    if cand.functional_addrs or cand.informational_addrs:
                        rp = cand
                        break
                except Exception:
                    continue
        return rp, {}
    except Exception as e:
        log_err(f"send_getlogin: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
#  FULL LOGIN PIPELINE (returns account_data)
# ═══════════════════════════════════════════════════════════════
async def login_account(uid: str, password: str) -> Optional[dict]:
    """Full guest login. Returns account_data dict (token, server_url, etc.) or None."""
    log_info(f"[LOGIN] UID {uid}...")

    vc = await version_config()
    if not vc:
        log_err("version_config failed")
        return None
    release_version, client_version, server_url = vc

    tg = await get_access_token(uid, password)
    if not tg:
        log_err(f"[LOGIN] UID {uid} — token grant failed (wrong password?)")
        return None
    open_id, access_token, platform = tg
    log_ok(f"[LOGIN] UID {uid} — access_token OK")

    device_info = _get_device_for_account(uid)
    lpd = await build_majorlogin_payload(open_id, access_token, platform,
                                         client_version, device_info)
    if not lpd:
        return None

    mr = await send_majorlogin(lpd, release_version, server_url)
    if mr is None:
        log_err(f"[LOGIN] UID {uid} — MajorLogin failed")
        return None
    log_ok(f"[LOGIN] UID {uid} — MajorLogin OK (acc {mr.account_id})")

    gl = await send_getlogin(lpd, mr.url, mr.token, release_version)
    if gl is None:
        log_err(f"[LOGIN] UID {uid} — GetLoginData failed")
        return None
    res_proto, _ = gl

    acc_id = str(mr.account_id)
    nickname = getattr(res_proto, "nickname", "") or f"Player_{acc_id}"
    log_ok(f"[LOGIN] UID {uid} — account {acc_id} | {nickname}")

    return {
        'account_id': mr.account_id,
        'nickname': nickname,
        'region': mr.region or FORCED_REGION,
        'token': mr.token,
        'server_url': mr.url,
        'release_version': release_version,
        'client_version': client_version,
        'aes_ak': mr.aes_ak,
        'iv_i': mr.iv_i,
        'access_token': access_token,
        'open_id': open_id,
        'platform': str(platform),
        'auth_uid': str(uid),
    }


# ═══════════════════════════════════════════════════════════════
#  WORKER (login + guild join, with retries)
# ═══════════════════════════════════════════════════════════════
async def process_account(uid: str, password: str, guild_id: int):
    label = f"UID {uid}"

    # ── login with retries ──
    account = None
    for attempt in range(1, LOGIN_MAX_RETRIES + 1):
        account = await login_account(uid, password)
        if account:
            break
        log_warn(f"{label} — login attempt {attempt}/{LOGIN_MAX_RETRIES} failed")
        if attempt < LOGIN_MAX_RETRIES:
            await asyncio.sleep(LOGIN_RETRY_DELAY)

    if not account:
        log_err(f"{label} — ❌ LOGIN FAILED (skipping guild join)")
        return False

    # ── guild join with retries ──
    for attempt in range(1, GUILD_JOIN_RETRIES + 1):
        ok, detail = await join_guild(
            account['token'],
            guild_id,
            account.get('server_url'),
            account.get('release_version'),
            label=f"acc {account['account_id']}",
        )
        if ok:
            log_ok(f"{label} — ✅ GUILD JOIN SUCCESS ({guild_id})")
            return True
        log_warn(f"{label} — guild attempt {attempt}/{GUILD_JOIN_RETRIES} failed")
        if attempt < GUILD_JOIN_RETRIES:
            await asyncio.sleep(GUILD_JOIN_RETRY_DELAY)

    log_err(f"{label} — ❌ GUILD JOIN FAILED")
    return False


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
def load_accounts() -> List[dict]:
    if not os.path.exists(ACCOUNTS_FILE):
        log_err(f"{ACCOUNTS_FILE} not found!")
        return []
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        log_err(f"{ACCOUNTS_FILE} root must be a list")
        return []
    except Exception as e:
        log_err(f"load_accounts: {e}")
        return []


async def ask_guild_id() -> Optional[int]:
    log("=" * 60, Colors.CYAN)
    while True:
        try:
            raw = await asyncio.to_thread(input, "[?] Enter GUILD ID (Enter = exit): ")
        except (EOFError, KeyboardInterrupt):
            return None
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            gid = int(raw)
            if gid > 0:
                log_ok(f"[GUILD] Guild ID: {gid}")
                log("=" * 60, Colors.CYAN)
                return gid
        except ValueError:
            pass
        log_err("[GUILD] Invalid guild ID (must be positive integer)")


async def run_all(guild_id: int, accounts: List[dict]):
    """Login + guild join for every account in parallel, results summary."""
    tasks = []
    for acc in accounts:
        uid = str(acc.get("uid", "")).strip()
        password = str(acc.get("password", "")).strip()
        if not uid or not password:
            log_warn(f"Skipping bad account entry: {acc}")
            continue
        tasks.append(asyncio.create_task(process_account(uid, password, guild_id)))

    if not tasks:
        log_err("No valid accounts to process")
        return

    log_info(f"Processing {len(tasks)} account(s) in parallel...")
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # ── summary ──
    log("=" * 60, Colors.CYAN)
    log("RESULTS SUMMARY", Colors.MAGENTA)
    log("=" * 60, Colors.CYAN)
    ok = 0
    fail = 0
    for acc, res in zip(accounts, results):
        uid = acc.get("uid", "?")
        if isinstance(res, Exception):
            log_err(f"UID {uid} — EXCEPTION: {res}")
            fail += 1
        elif res is True:
            log_ok(f"UID {uid} — ✅ SUCCESS")
            ok += 1
        else:
            log_err(f"UID {uid} — ❌ FAILED")
            fail += 1
    log("=" * 60, Colors.CYAN)
    log_ok(f"SUCCESS: {ok}")
    log_err(f"FAILED : {fail}")
    log("=" * 60, Colors.CYAN)


async def main():
    log("=" * 60, Colors.CYAN)
    log("  GUILD AUTO-JOIN BOT", Colors.GREEN)
    log("  Guest UID + Password → Login → Join Guild", Colors.WHITE)
    log("=" * 60, Colors.CYAN)

    guild_id = await ask_guild_id()
    if not guild_id:
        log_warn("[GUILD] No guild ID — exiting")
        return

    accounts = load_accounts()
    if not accounts:
        log_err(f"No accounts in {ACCOUNTS_FILE}")
        log_info(f"Create {ACCOUNTS_FILE} with format:")
        log_info('  [{"uid": "123456789", "password": "abc123"}]', Colors.WHITE)
        return

    log_info(f"Loaded {len(accounts)} account(s)")
    await run_all(guild_id, accounts)

    try:
        await client.aclose()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log_warn("\n[STOP] Interrupted by user")