"""A throwaway Prosody server for tests and manual testing (tools/run-prosody).

Everything lives in one temporary directory: config, data, logs and a self-signed certificate for localhost,
conference.localhost and upload.localhost.  It listens on 127.0.0.1 only, on free ports, with STARTTLS and
direct TLS, group chats with archives, HTTP file upload over HTTPS, invites, blocking, admin commands and
in-band registration - and with test users alice, bob, carol and admin (password: "password")."""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time

DOMAIN = "localhost"
MUC = "conference.localhost"
UPLOAD = "upload.localhost"
USERS = ("alice", "bob", "carol", "dave", "admin")
PASSWORD = "password"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_cert(directory: str) -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, DOMAIN)])
    now = dt.datetime.now(dt.UTC)
    san = x509.SubjectAlternativeName([x509.DNSName(DOMAIN), x509.DNSName("*." + DOMAIN), x509.DNSName(MUC),
                                       x509.DNSName(UPLOAD), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=30)).add_extension(san, critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    crt = os.path.join(directory, "localhost.crt")
    kp = os.path.join(directory, "localhost.key")
    with open(crt, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kp, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                   serialization.NoEncryption()))
    os.chmod(kp, 0o600)
    return crt, kp


CONFIG = """
daemonize = false
run_as_root = true
pidfile = "{dir}/prosody.pid"
data_path = "{dir}/data"
log = {{ {loglevel} = "{dir}/prosody.log" }}
interfaces = {{ "127.0.0.1" }}
c2s_ports = {{ {c2s} }}
c2s_direct_tls_ports = {{ {c2s_tls} }}
s2s_ports = {{ }}
http_ports = {{ }}
http_interfaces = {{ "127.0.0.1" }}
https_ports = {{ {https} }}
https_interfaces = {{ "127.0.0.1" }}
https_ssl = {{ certificate = "{crt}"; key = "{key}"; }}
ssl = {{ certificate = "{crt}"; key = "{key}"; }}
c2s_require_encryption = true
authentication = "internal_hashed"
storage = "internal"
allow_registration = true
registration_invite_only = false
min_seconds_between_registrations = 0
admins = {{ "admin@{domain}" }}
archive_expires_after = "never"
default_archive_policy = true
max_archive_query_results = 100
smacks_hibernation_time = 30
limits = {{ c2s = {{ rate = "10mb/s"; burst = "10mb"; }}; }}
contact_info = {{ abuse = {{ "mailto:abuse@{domain}" }}; admin = {{ "xmpp:admin@{domain}" }}; }}
modules_enabled = {{
    "roster"; "saslauth"; "tls"; "disco"; "carbons"; "pep"; "private"; "blocklist"; "vcard4"; "vcard_legacy";
    "version"; "uptime"; "time"; "ping"; "register"; "admin_adhoc"; "mam"; "csi_simple"; "smacks";
    "bookmarks"; "server_contact_info"; "invites"; "invites_adhoc"; "invites_register"; "announce"; "motd";
    "lastactivity"; "http"; "posix";
}}
modules_disabled = {{ "s2s"; "dialback"; }}

VirtualHost "{domain}"

Component "{muc}" "muc"
    modules_enabled = {{ "muc_mam"; }}
    muc_room_default_persistent = false
    restrict_room_creation = false

Component "{upload}" "http_file_share"
    http_file_share_size_limit = 16*1024*1024
    http_host = "{domain}"
    http_external_url = "https://{domain}:{https}/"
"""


class ProsodyServer:
    def __init__(self, directory: str | None = None, loglevel: str = "info"):
        self.dir = directory or tempfile.mkdtemp(prefix="ward-prosody-")
        self.own_dir = directory is None
        self.loglevel = loglevel
        self.c2s = free_port()
        self.c2s_tls = free_port()
        self.https = free_port()
        self.proc: subprocess.Popen | None = None
        self.cert = ""
        self.key = ""

    @staticmethod
    def available() -> bool:
        return shutil.which("prosody") is not None and shutil.which("prosodyctl") is not None

    @property
    def config_path(self) -> str:
        return os.path.join(self.dir, "prosody.cfg.lua")

    def write_config(self) -> None:
        os.makedirs(os.path.join(self.dir, "data"), exist_ok=True)
        self.cert, self.key = make_cert(self.dir)
        with open(self.config_path, "w") as fh:
            fh.write(CONFIG.format(dir=self.dir, c2s=self.c2s, c2s_tls=self.c2s_tls, https=self.https,
                                   crt=self.cert, key=self.key, domain=DOMAIN, muc=MUC, upload=UPLOAD,
                                   loglevel=self.loglevel))

    def ctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["prosodyctl", "--config", self.config_path, *args], capture_output=True, text=True,
                              timeout=60, check=check)

    def register(self, user: str, password: str = PASSWORD) -> None:
        self.ctl("register", user, DOMAIN, password)

    def start(self, timeout: float = 20) -> ProsodyServer:
        self.write_config()
        for u in USERS:
            self.register(u)
        self.proc = subprocess.Popen(["prosody", "--config", self.config_path, "-F"], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("prosody exited:\n" + self.log_tail())
            try:
                with socket.create_connection(("127.0.0.1", self.c2s), 0.5), \
                        socket.create_connection(("127.0.0.1", self.https), 0.5):
                    return self
            except OSError:
                time.sleep(0.1)
        self.stop()
        raise RuntimeError("prosody did not start:\n" + self.log_tail())

    def log_tail(self, n: int = 40) -> str:
        try:
            with open(os.path.join(self.dir, "prosody.log")) as fh:
                return "".join(fh.readlines()[-n:])
        except OSError:
            return "(no log)"

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.proc = None
        if self.own_dir:
            shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self) -> ProsodyServer:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
