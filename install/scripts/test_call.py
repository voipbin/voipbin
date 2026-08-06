#!/usr/bin/env python3
"""
Test SIP call between two extensions via Kamailio SIP proxy.
This script simulates extension 2000 calling extension 3000.
"""

import socket
import hashlib
import random
import os
import time
import sys
import re

def read_env_var(name, default=""):
    """Read VAR= from the sandbox .env (last occurrence wins, never sourced).

    The project directory is the script's parent, overridable via
    VOIPBIN_PROJECT_DIR (same override the CLI honors).
    """
    project_dir = os.environ.get("VOIPBIN_PROJECT_DIR") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    value = default
    try:
        with open(os.path.join(project_dir, ".env")) as f:
            for line in f:
                if line.startswith(f"{name}="):
                    # .strip() drops \r\n (CRLF-saved .env) and surrounding
                    # whitespace, mirroring common.sh:get_env_var.
                    value = line.split("=", 1)[1].strip()
    except OSError:
        pass
    return value


# Configuration. Defaults derive from .env (dual-mode DNS, design §2.10) so
# the script points at the configured base domain in both modes; explicit
# VOIPBIN_* environment variables still win.
SIP_SERVER = os.environ.get("VOIPBIN_SIP_SERVER") or (
    "sip." + (read_env_var("BASE_DOMAIN") or "voipbin.test")
)
SIP_PORT = 5060
# Set VOIPBIN_CUSTOMER_ID to the customer created by setup_test_customer.sh
CUSTOMER_ID = os.environ.get("VOIPBIN_CUSTOMER_ID", "904a4f3b-d72e-48d4-9d9f-1e06968917c5")
DOMAIN_NAME_EXTENSION = os.environ.get("VOIPBIN_DOMAIN_NAME_EXTENSION") or (
    read_env_var("DOMAIN_NAME_EXTENSION") or "registrar.voipbin.test"
)
DOMAIN = f"{CUSTOMER_ID}.{DOMAIN_NAME_EXTENSION}"

# Caller (User A)
CALLER_EXT = "2000"
CALLER_PASS = "pass2000"

# Callee (User B)
CALLEE_EXT = "3000"

def generate_branch():
    return f"z9hG4bK{random.randint(100000000, 999999999)}"

def generate_tag():
    return f"{random.randint(10000000, 99999999)}"

def generate_call_id():
    return f"{random.randint(1000000000, 9999999999)}@{SIP_SERVER}"

def calculate_digest_response(username, realm, password, method, uri, nonce, nc, cnonce, qop):
    """Calculate MD5 digest response for SIP authentication"""
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    if qop:
        response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
    else:
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    return response

def parse_auth_challenge(response):
    """Parse WWW-Authenticate or Proxy-Authenticate header"""
    auth_info = {}
    for line in response.split('\r\n'):
        if line.startswith('WWW-Authenticate:') or line.startswith('Proxy-Authenticate:'):
            match = re.search(r'realm="([^"]+)"', line)
            if match:
                auth_info['realm'] = match.group(1)
            match = re.search(r'nonce="([^"]+)"', line)
            if match:
                auth_info['nonce'] = match.group(1)
            match = re.search(r'qop="?([^",]+)"?', line)
            if match:
                auth_info['qop'] = match.group(1).split(',')[0]
    return auth_info

def get_response_code(response):
    """Extract SIP response code"""
    match = re.match(r'SIP/2\.0 (\d+)', response)
    if match:
        return int(match.group(1))
    return None

def get_to_tag(response):
    """Extract To tag from response"""
    match = re.search(r'To:.*?;tag=([^\s;>]+)', response)
    if match:
        return match.group(1)
    return None

def main():
    print("=" * 60)
    print("VoIPBin SIP Call Test")
    print("=" * 60)
    print(f"Caller: {CALLER_EXT}@{DOMAIN}")
    print(f"Callee: {CALLEE_EXT}@{DOMAIN}")
    print(f"Server: {SIP_SERVER}:{SIP_PORT}")
    print("=" * 60)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(15)
    sock.bind(('0.0.0.0', 0))
    # Derive our own IP from the route to the SIP server. The previous code
    # reused SIP_SERVER as local_ip, which only worked by coincidence while
    # both were hardcoded host-local IPs - and breaks outright (400 Bad
    # Request) once SIP_SERVER is a hostname, since SDP c=/o= lines require a
    # numeric IP.
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect((SIP_SERVER, SIP_PORT))
        local_ip = probe.getsockname()[0]
        probe.close()
    except OSError as e:
        print(f"Could not resolve/reach SIP server {SIP_SERVER}:{SIP_PORT}: {e}")
        sock.close()
        return False
    local_port = sock.getsockname()[1]
    print(f"Local endpoint: {local_ip}:{local_port}")

    branch = generate_branch()
    tag = generate_tag()
    call_id = generate_call_id()
    cseq = 1

    # SDP for audio
    sdp = f"""v=0
o=- {random.randint(1000000, 9999999)} {random.randint(1000000, 9999999)} IN IP4 {local_ip}
s=Test Call
c=IN IP4 {local_ip}
t=0 0
m=audio 10000 RTP/AVP 0 8
a=rtpmap:0 PCMU/8000
a=rtpmap:8 PCMA/8000
a=sendrecv
"""

    try:
        print("\n[Step 1] Sending INVITE...")

        invite = f"""INVITE sip:{CALLEE_EXT}@{DOMAIN} SIP/2.0\r
Via: SIP/2.0/UDP {local_ip}:{local_port};branch={branch};rport\r
Max-Forwards: 70\r
From: <sip:{CALLER_EXT}@{DOMAIN}>;tag={tag}\r
To: <sip:{CALLEE_EXT}@{DOMAIN}>\r
Call-ID: {call_id}\r
CSeq: {cseq} INVITE\r
Contact: <sip:{CALLER_EXT}@{local_ip}:{local_port}>\r
Content-Type: application/sdp\r
Allow: INVITE,ACK,CANCEL,BYE,OPTIONS\r
User-Agent: VoIPBin-TestClient/1.0\r
Content-Length: {len(sdp)}\r
\r
{sdp}"""

        sock.sendto(invite.encode(), (SIP_SERVER, SIP_PORT))

        to_tag = None
        max_attempts = 20

        for attempt in range(max_attempts):
            try:
                data, addr = sock.recvfrom(65535)
                response = data.decode('utf-8', errors='ignore')
                code = get_response_code(response)

                if code:
                    reason_line = response.split('\r\n')[0]
                    print(f"[Response] {reason_line}")

                    if code == 100:
                        continue
                    elif code == 180:
                        print("  -> Ringing!")
                        continue
                    elif code == 183:
                        continue
                    elif code == 401 or code == 407:
                        auth_info = parse_auth_challenge(response)
                        print(f"  -> Auth required. Realm: {auth_info.get('realm', 'N/A')}")

                        if 'nonce' in auth_info:
                            cnonce = generate_tag()
                            nc = "00000001"
                            uri = f"sip:{CALLEE_EXT}@{DOMAIN}"
                            digest = calculate_digest_response(
                                CALLER_EXT,
                                auth_info['realm'],
                                CALLER_PASS,
                                "INVITE",
                                uri,
                                auth_info['nonce'],
                                nc,
                                cnonce,
                                auth_info.get('qop')
                            )

                            if code == 407:
                                auth_header = f'Proxy-Authorization: Digest username="{CALLER_EXT}"'
                            else:
                                auth_header = f'Authorization: Digest username="{CALLER_EXT}"'
                            auth_header += f', realm="{auth_info["realm"]}"'
                            auth_header += f', nonce="{auth_info["nonce"]}"'
                            auth_header += f', uri="{uri}"'
                            auth_header += f', response="{digest}"'
                            auth_header += ', algorithm=MD5'
                            if auth_info.get('qop'):
                                auth_header += f', qop={auth_info["qop"]}'
                                auth_header += f', nc={nc}'
                                auth_header += f', cnonce="{cnonce}"'

                            cseq += 1
                            branch = generate_branch()
                            print("\n[Step 2] Sending authenticated INVITE...")

                            invite = f"""INVITE sip:{CALLEE_EXT}@{DOMAIN} SIP/2.0\r
Via: SIP/2.0/UDP {local_ip}:{local_port};branch={branch};rport\r
Max-Forwards: 70\r
From: <sip:{CALLER_EXT}@{DOMAIN}>;tag={tag}\r
To: <sip:{CALLEE_EXT}@{DOMAIN}>\r
Call-ID: {call_id}\r
CSeq: {cseq} INVITE\r
Contact: <sip:{CALLER_EXT}@{local_ip}:{local_port}>\r
{auth_header}\r
Content-Type: application/sdp\r
Allow: INVITE,ACK,CANCEL,BYE,OPTIONS\r
User-Agent: VoIPBin-TestClient/1.0\r
Content-Length: {len(sdp)}\r
\r
{sdp}"""
                            sock.sendto(invite.encode(), (SIP_SERVER, SIP_PORT))
                        continue

                    elif code == 200:
                        to_tag = get_to_tag(response)
                        print("  -> 200 OK - Call answered!")

                        # Send ACK
                        ack_branch = generate_branch()
                        ack = f"""ACK sip:{CALLEE_EXT}@{DOMAIN} SIP/2.0\r
Via: SIP/2.0/UDP {local_ip}:{local_port};branch={ack_branch};rport\r
Max-Forwards: 70\r
From: <sip:{CALLER_EXT}@{DOMAIN}>;tag={tag}\r
To: <sip:{CALLEE_EXT}@{DOMAIN}>;tag={to_tag}\r
Call-ID: {call_id}\r
CSeq: {cseq} ACK\r
Contact: <sip:{CALLER_EXT}@{local_ip}:{local_port}>\r
Content-Length: 0\r
\r
"""
                        print("\n[Step 3] Sending ACK...")
                        sock.sendto(ack.encode(), (SIP_SERVER, SIP_PORT))

                        print("\n" + "=" * 60)
                        print("CALL ESTABLISHED - 2-WAY AUDIO PATH READY!")
                        print("=" * 60)

                        print("\nWaiting 3 seconds then hanging up...")
                        time.sleep(3)

                        # Send BYE
                        bye_branch = generate_branch()
                        bye = f"""BYE sip:{CALLEE_EXT}@{DOMAIN} SIP/2.0\r
Via: SIP/2.0/UDP {local_ip}:{local_port};branch={bye_branch};rport\r
Max-Forwards: 70\r
From: <sip:{CALLER_EXT}@{DOMAIN}>;tag={tag}\r
To: <sip:{CALLEE_EXT}@{DOMAIN}>;tag={to_tag}\r
Call-ID: {call_id}\r
CSeq: {cseq + 1} BYE\r
Contact: <sip:{CALLER_EXT}@{local_ip}:{local_port}>\r
Content-Length: 0\r
\r
"""
                        print("\n[Step 4] Sending BYE...")
                        sock.sendto(bye.encode(), (SIP_SERVER, SIP_PORT))

                        try:
                            data, addr = sock.recvfrom(65535)
                            bye_response = data.decode('utf-8', errors='ignore')
                            bye_code = get_response_code(bye_response)
                            print(f"[BYE Response] {bye_code}")
                        except socket.timeout:
                            print("  -> BYE timeout")

                        print("\n" + "=" * 60)
                        print("TEST PASSED: 2-way audio call completed!")
                        print("=" * 60)
                        return True

                    elif code >= 400:
                        print(f"  -> Error: {code}")
                        return False

            except socket.timeout:
                print(f"[Timeout] Attempt {attempt + 1}/{max_attempts}")
                continue

        print("\nTest incomplete")
        return False

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        sock.close()

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
