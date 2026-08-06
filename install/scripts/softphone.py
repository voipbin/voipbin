#!/usr/bin/env python3
"""
Simple SIP Softphone that registers and auto-answers calls.
Runs in the background to keep registration alive.
"""

import socket
import hashlib
import os
import random
import time
import sys
import re
import threading
import argparse


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


# Defaults derive from .env (dual-mode DNS, design §2.10) so the softphone
# points at the configured base domain in both modes; explicit CLI arguments
# still win.
def default_server():
    return "sip." + (read_env_var("BASE_DOMAIN") or "voipbin.test")


def default_domain_ext():
    return read_env_var("DOMAIN_NAME_EXTENSION") or "registrar.voipbin.test"


class SIPSoftphone:
    def __init__(self, server, port, customer_id, extension, password, local_port=None,
                 domain_ext=None):
        self.server = server
        self.port = port
        self.customer_id = customer_id
        self.extension = extension
        self.password = password
        self.domain = f"{customer_id}.{domain_ext or default_domain_ext()}"

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(5)
        if local_port:
            self.sock.bind(('0.0.0.0', local_port))
        else:
            self.sock.bind(('0.0.0.0', 0))

        # Derive our own IP from the route to the SIP server instead of
        # reusing the server value: with a hostname server (the default),
        # reusing it would embed a non-numeric address into Via/Contact and
        # the auto-answer SDP c=/o= lines, which require numeric IPs.
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect((server, port))
            self.local_ip = probe.getsockname()[0]
            probe.close()
        except OSError as e:
            self.sock.close()
            raise SystemExit(f"Could not resolve/reach SIP server {server}:{port}: {e}")
        self.local_port = self.sock.getsockname()[1]
        self.tag = self._generate_tag()
        self.call_id_counter = 0
        self.running = True
        self.registered = False

    def _generate_branch(self):
        return f"z9hG4bK{random.randint(100000000, 999999999)}"

    def _generate_tag(self):
        return f"{random.randint(10000000, 99999999)}"

    def _generate_call_id(self):
        self.call_id_counter += 1
        return f"{self.call_id_counter}_{random.randint(1000000, 9999999)}@{self.local_ip}"

    def _calculate_digest(self, username, realm, password, method, uri, nonce, nc, cnonce, qop):
        ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        if qop:
            response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
        else:
            response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        return response

    def _parse_auth(self, response):
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

    def _get_response_code(self, response):
        match = re.match(r'SIP/2\.0 (\d+)', response)
        return int(match.group(1)) if match else None

    def register(self):
        """Send REGISTER request"""
        branch = self._generate_branch()
        call_id = self._generate_call_id()
        cseq = 1

        register = f"""REGISTER sip:{self.domain} SIP/2.0\r
Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch};rport\r
Max-Forwards: 70\r
From: <sip:{self.extension}@{self.domain}>;tag={self.tag}\r
To: <sip:{self.extension}@{self.domain}>\r
Call-ID: {call_id}\r
CSeq: {cseq} REGISTER\r
Contact: <sip:{self.extension}@{self.local_ip}:{self.local_port}>\r
Expires: 300\r
Content-Length: 0\r
\r
"""

        self.sock.sendto(register.encode(), (self.server, self.port))

        try:
            data, addr = self.sock.recvfrom(65535)
            response = data.decode('utf-8', errors='ignore')
            code = self._get_response_code(response)

            if code == 401:
                auth_info = self._parse_auth(response)
                if 'nonce' in auth_info:
                    cnonce = self._generate_tag()
                    nc = "00000001"
                    uri = f"sip:{self.domain}"
                    digest = self._calculate_digest(
                        self.extension, auth_info['realm'], self.password,
                        "REGISTER", uri, auth_info['nonce'], nc, cnonce, auth_info.get('qop')
                    )

                    auth_header = f'Authorization: Digest username="{self.extension}"'
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
                    branch = self._generate_branch()

                    register = f"""REGISTER sip:{self.domain} SIP/2.0\r
Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch};rport\r
Max-Forwards: 70\r
From: <sip:{self.extension}@{self.domain}>;tag={self.tag}\r
To: <sip:{self.extension}@{self.domain}>\r
Call-ID: {call_id}\r
CSeq: {cseq} REGISTER\r
Contact: <sip:{self.extension}@{self.local_ip}:{self.local_port}>\r
{auth_header}\r
Expires: 300\r
Content-Length: 0\r
\r
"""
                    self.sock.sendto(register.encode(), (self.server, self.port))
                    data, addr = self.sock.recvfrom(65535)
                    response = data.decode('utf-8', errors='ignore')
                    code = self._get_response_code(response)

            if code == 200:
                self.registered = True
                print(f"[{self.extension}] Registered successfully")
                return True
            else:
                print(f"[{self.extension}] Registration failed: {code}")
                return False

        except socket.timeout:
            print(f"[{self.extension}] Registration timeout")
            return False

    def run(self, auto_answer=True):
        """Run the softphone, keeping registration alive and optionally auto-answering"""
        print(f"[{self.extension}] Starting softphone on {self.local_ip}:{self.local_port}")

        # Initial registration
        self.register()

        last_register = time.time()
        register_interval = 240  # Re-register every 4 minutes

        while self.running:
            # Re-register periodically
            if time.time() - last_register > register_interval:
                self.register()
                last_register = time.time()

            # Listen for incoming messages
            try:
                self.sock.settimeout(1)
                data, addr = self.sock.recvfrom(65535)
                message = data.decode('utf-8', errors='ignore')

                if message.startswith('INVITE'):
                    print(f"[{self.extension}] Incoming call!")
                    if auto_answer:
                        self._handle_invite(message, addr)
                elif message.startswith('BYE'):
                    self._handle_bye(message, addr)
                elif message.startswith('OPTIONS'):
                    self._handle_options(message, addr)

            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"[{self.extension}] Error: {e}")

    def _handle_invite(self, message, addr):
        """Handle incoming INVITE - auto-answer"""
        # Parse headers
        lines = message.split('\r\n')
        via_headers = []  # Collect ALL Via headers
        from_header = None
        to_header = None
        call_id = None
        cseq = None
        contact = None

        for line in lines:
            if line.startswith('Via:'):
                via_headers.append(line)  # Collect all Via headers
            elif line.startswith('From:'):
                from_header = line
            elif line.startswith('To:'):
                to_header = line
            elif line.startswith('Call-ID:'):
                call_id = line.split(':', 1)[1].strip()
            elif line.startswith('CSeq:'):
                cseq = line.split(':', 1)[1].strip()
            elif line.startswith('Contact:'):
                contact = line

        # Join all Via headers for the response
        via = '\r\n'.join(via_headers)

        # Add tag to To header
        to_tag = self._generate_tag()
        if ';tag=' not in to_header:
            to_header = to_header.rstrip() + f';tag={to_tag}'

        # Send 180 Ringing
        ringing = f"""SIP/2.0 180 Ringing\r
{via}\r
{from_header}\r
{to_header}\r
Call-ID: {call_id}\r
CSeq: {cseq}\r
Contact: <sip:{self.extension}@{self.local_ip}:{self.local_port}>\r
Content-Length: 0\r
\r
"""
        self.sock.sendto(ringing.encode(), addr)
        print(f"[{self.extension}] Sent 180 Ringing")

        time.sleep(0.5)  # Brief delay before answering

        # Create SDP answer
        sdp = f"""v=0\r
o=- {random.randint(1000000, 9999999)} {random.randint(1000000, 9999999)} IN IP4 {self.local_ip}\r
s=SIP Call\r
c=IN IP4 {self.local_ip}\r
t=0 0\r
m=audio {self.local_port + 1000} RTP/AVP 0 8\r
a=rtpmap:0 PCMU/8000\r
a=rtpmap:8 PCMA/8000\r
a=sendrecv\r
"""

        # Send 200 OK
        ok = f"""SIP/2.0 200 OK\r
{via}\r
{from_header}\r
{to_header}\r
Call-ID: {call_id}\r
CSeq: {cseq}\r
Contact: <sip:{self.extension}@{self.local_ip}:{self.local_port}>\r
Content-Type: application/sdp\r
Content-Length: {len(sdp)}\r
\r
{sdp}"""

        self.sock.sendto(ok.encode(), addr)
        print(f"[{self.extension}] Sent 200 OK - Call answered!")

    def _handle_bye(self, message, addr):
        """Handle BYE request"""
        lines = message.split('\r\n')
        via_headers = []
        from_header = None
        to_header = None
        call_id = None
        cseq = None

        for line in lines:
            if line.startswith('Via:'):
                via_headers.append(line)
            elif line.startswith('From:'):
                from_header = line
            elif line.startswith('To:'):
                to_header = line
            elif line.startswith('Call-ID:'):
                call_id = line.split(':', 1)[1].strip()
            elif line.startswith('CSeq:'):
                cseq = line.split(':', 1)[1].strip()

        via = '\r\n'.join(via_headers)
        ok = f"""SIP/2.0 200 OK\r
{via}\r
{from_header}\r
{to_header}\r
Call-ID: {call_id}\r
CSeq: {cseq}\r
Content-Length: 0\r
\r
"""
        self.sock.sendto(ok.encode(), addr)
        print(f"[{self.extension}] Call ended (BYE)")

    def _handle_options(self, message, addr):
        """Handle OPTIONS request"""
        lines = message.split('\r\n')
        via_headers = []
        from_header = None
        to_header = None
        call_id = None
        cseq = None

        for line in lines:
            if line.startswith('Via:'):
                via_headers.append(line)
            elif line.startswith('From:'):
                from_header = line
            elif line.startswith('To:'):
                to_header = line
            elif line.startswith('Call-ID:'):
                call_id = line.split(':', 1)[1].strip()
            elif line.startswith('CSeq:'):
                cseq = line.split(':', 1)[1].strip()

        via = '\r\n'.join(via_headers)
        ok = f"""SIP/2.0 200 OK\r
{via}\r
{from_header}\r
{to_header}\r
Call-ID: {call_id}\r
CSeq: {cseq}\r
Allow: INVITE,ACK,CANCEL,BYE,OPTIONS\r
Content-Length: 0\r
\r
"""
        self.sock.sendto(ok.encode(), addr)

    def stop(self):
        self.running = False
        self.sock.close()


def main():
    parser = argparse.ArgumentParser(description='SIP Softphone')
    parser.add_argument('extension', help='Extension number (e.g., 2000)')
    parser.add_argument('password', help='SIP password')
    parser.add_argument('--server', default=None,
                        help='SIP server address (default: sip.<BASE_DOMAIN> from .env)')
    parser.add_argument('--port', type=int, default=5060, help='SIP server port')
    parser.add_argument('--customer-id', default='904a4f3b-d72e-48d4-9d9f-1e06968917c5', help='Customer ID')
    parser.add_argument('--domain-ext', default=None,
                        help='SIP realm domain suffix (default: DOMAIN_NAME_EXTENSION from .env)')
    parser.add_argument('--local-port', type=int, help='Local port to bind')
    parser.add_argument('--no-auto-answer', action='store_true', help='Disable auto-answer')

    args = parser.parse_args()

    phone = SIPSoftphone(
        args.server or default_server(),
        args.port,
        args.customer_id,
        args.extension,
        args.password,
        args.local_port,
        domain_ext=args.domain_ext
    )

    try:
        phone.run(auto_answer=not args.no_auto_answer)
    except KeyboardInterrupt:
        print(f"\n[{args.extension}] Shutting down...")
        phone.stop()


if __name__ == "__main__":
    main()
