"""The MQTT / TLS settings of the Output screen: TLS version pinning, certificate check, errors in words. Synthetic certificate only."""
import datetime
import os
import socket
import ssl
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(__file__)
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "edge-gateway")]
import protoprobe as pp  # noqa: E402


def self_signed():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False).sign(key, hashes.SHA256()))
    d = tempfile.mkdtemp()
    cp, kp = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    with open(cp, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kp, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cp, kp


class Context(unittest.TestCase):
    def test_versions_are_pinned_and_auto_is_open(self):
        self.assertEqual(pp.build_tls_context("1.2").minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(pp.build_tls_context("1.2").maximum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(pp.build_tls_context("1.3").maximum_version, ssl.TLSVersion.TLSv1_3)
        auto = pp.build_tls_context("auto")
        self.assertNotEqual(auto.minimum_version, auto.maximum_version)
        with self.assertRaises(ValueError):
            pp.build_tls_context("9.9")

    def test_certificate_check_can_be_switched_off(self):
        on, off = pp.build_tls_context("auto", verify=True), pp.build_tls_context("auto", verify=False)
        self.assertEqual((on.verify_mode, on.check_hostname), (ssl.CERT_REQUIRED, True))
        self.assertEqual((off.verify_mode, off.check_hostname), (ssl.CERT_NONE, False))


class Handshake(unittest.TestCase):
    """A real local TLS server pinned to 1.2: a client pinned to 1.3 must fail, one pinned to 1.2 must work, an untrusted certificate must fail."""
    def setUp(self):
        self.cert, self.key = self_signed()
        sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sctx.load_cert_chain(self.cert, self.key)
        sctx.minimum_version = sctx.maximum_version = ssl.TLSVersion.TLSv1_2
        self.srv = socket.socket(); self.srv.bind(("127.0.0.1", 0)); self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.stop = False

        def serve():
            self.srv.settimeout(0.2)
            while not self.stop:
                try:
                    c, _ = self.srv.accept()
                except OSError:
                    continue
                try:
                    sctx.wrap_socket(c, server_side=True).close()
                except (ssl.SSLError, OSError):
                    c.close()
        self.t = threading.Thread(target=serve, daemon=True); self.t.start()

    def tearDown(self):
        self.stop = True; self.t.join(2); self.srv.close()

    def _try(self, **kw):
        ctx = pp.build_tls_context(**kw)
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=3) as s, ctx.wrap_socket(s, server_hostname="localhost") as t:
                return t.version()
        except (ssl.SSLError, OSError) as exc:
            return exc

    def test_version_and_trust(self):
        self.assertEqual(self._try(tls_version="1.2", ca_cert=self.cert), "TLSv1.2")
        self.assertIsInstance(self._try(tls_version="1.3", ca_cert=self.cert), Exception)      # server only speaks 1.2
        self.assertIsInstance(self._try(tls_version="auto"), Exception)                        # certificate not trusted
        self.assertEqual(self._try(tls_version="auto", verify=False), "TLSv1.2")               # check switched off


class Publisher(unittest.TestCase):
    def test_a_closed_port_gives_a_reason_not_a_crash(self):
        try:
            import paho.mqtt.client  # noqa: F401
        except ImportError:
            self.skipTest("paho-mqtt not installed")
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        pub = pp.MQTTPublisher()
        ok = pub.connect("127.0.0.1", port, "", "", "", False, None, None, None)
        self.assertFalse(ok)
        self.assertTrue(pub.last_error)

    def test_bad_tls_files_are_reported(self):
        try:
            import paho.mqtt.client  # noqa: F401
        except ImportError:
            self.skipTest("paho-mqtt not installed")
        pub = pp.MQTTPublisher()
        self.assertFalse(pub.connect("127.0.0.1", 1, "", "", "", True, "/nonexistent/ca.pem", None, None))
        self.assertIn("TLS settings", pub.last_error)


if __name__ == "__main__":
    unittest.main()
