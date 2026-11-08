import io
import os
import socket
import ssl
import time
import unittest
from concurrent.futures import TimeoutError as FutureTimeout
from unittest.mock import Mock, patch

import media_download as media


PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]


class FakeSocket:
    """Use the real HTTP parser without accessing the network."""

    def __init__(self, response):
        self.stream = io.BytesIO(response)
        self.sent = bytearray()
        self.closed = False

    def makefile(self, mode, buffering=0):
        return self.stream

    def setsockopt(self, *args):
        pass

    def settimeout(self, timeout):
        pass

    def sendall(self, data):
        self.sent.extend(data)

    def close(self):
        self.closed = True


class DownloadTests(unittest.TestCase):
    def download(self, response, url="http://images.example/image?signature=secret", **kwargs):
        sock = FakeSocket(response)
        self.addCleanup(sock.stream.close)
        with patch.object(media, "_resolve", return_value=PUBLIC), patch.object(
            media, "_connect", return_value=sock
        ):
            result = media.download_image(url, **kwargs)
        self.assertTrue(sock.closed)
        self.assertTrue(sock.stream.closed)
        return result, sock

    def test_signed_url_and_original_host_are_preserved(self):
        body, sock = self.download(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc")
        self.assertEqual(body, b"abc")
        self.assertIn(b"GET /image?signature=secret HTTP/1.1\r\n", sock.sent)
        self.assertIn(b"Host: images.example\r\n", sock.sent)
        self.assertIn(b"Accept-Encoding: identity\r\n", sock.sent)
        self.assertNotIn(b"Authorization:", sock.sent)

    def test_http10_and_connection_close_bodies_are_read(self):
        for headers in (b"HTTP/1.0 200 OK\r\n", b"HTTP/1.1 200 OK\r\nConnection: close\r\n"):
            with self.subTest(headers=headers):
                body, _ = self.download(headers + b"\r\nabc")
                self.assertEqual(body, b"abc")

    def test_connection_close_keeps_real_socket_file_alive_until_body_is_read(self):
        class SocketAdapter:
            def __init__(self, sock):
                self.sock = sock

            def __getattr__(self, name):
                return getattr(self.sock, name)

            def setsockopt(self, *args):
                pass  # The socketpair uses local sockets rather than TCP.

        client, server = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        server.sendall(b"HTTP/1.0 200 OK\r\n\r\nabc")
        server.shutdown(socket.SHUT_WR)
        with patch.object(media, "_resolve", return_value=PUBLIC), patch.object(
            media, "_connect", return_value=SocketAdapter(client)
        ):
            self.assertEqual(media.download_image("http://images.example/a"), b"abc")
        self.assertEqual(client.fileno(), -1)

    def test_tls_verifies_the_hostname_instead_of_the_pinned_ip(self):
        with patch.object(ssl.SSLContext, "wrap_socket", side_effect=lambda sock, **kw: sock) as wrap:
            body, sock = self.download(
                b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc",
                url="https://images.example/image",
            )
        self.assertEqual(body, b"abc")
        wrap.assert_called_once_with(sock, server_hostname="images.example")

    def test_chunked_download(self):
        body, _ = self.download(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n"
        )
        self.assertEqual(body, b"abc")

    def test_redirects_are_rejected_without_fetching_the_location(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status), self.assertRaisesRegex(media.MediaDownloadError, "redirects"):
                self.download(
                    f"HTTP/1.1 {status} Redirect\r\nLocation: http://user:secret@127.0.0.1/\r\n\r\n".encode()
                )

    def test_declared_oversize_is_rejected_before_body_read(self):
        with patch.object(media, "MAX_IMAGE_BYTES", 3), self.assertRaises(media.MediaDownloadError) as error:
            self.download(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n")
        self.assertEqual(error.exception.status_code, 413)

    def test_streamed_size_limit_with_and_without_chunked_encoding(self):
        responses = (
            b"HTTP/1.0 200 OK\r\n\r\nabcd",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nabcd\r\n0\r\n\r\n",
        )
        for response in responses:
            with self.subTest(response=response), patch.object(media, "MAX_IMAGE_BYTES", 3):
                with self.assertRaises(media.MediaDownloadError) as error:
                    self.download(response)
                self.assertEqual(error.exception.status_code, 413)

    def test_exact_size_limit_is_accepted(self):
        with patch.object(media, "MAX_IMAGE_BYTES", 3):
            body, _ = self.download(b"HTTP/1.0 200 OK\r\n\r\nabc")
        self.assertEqual(body, b"abc")

    def test_progressing_download_can_take_more_than_fifteen_seconds(self):
        clock = [0.0]

        class SlowStream(io.BytesIO):
            def readinto(self, buffer):
                clock[0] += 0.5
                return super().readinto(memoryview(buffer)[:1])

        sock = FakeSocket(b"")
        sock.stream = SlowStream(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc")
        with patch.object(media.time, "monotonic", side_effect=lambda: clock[0]):
            with patch.object(media, "_resolve", return_value=PUBLIC), patch.object(media, "_connect", return_value=sock):
                self.assertEqual(media.download_image("http://images.example/a"), b"abc")
        self.assertGreater(clock[0], 15)
        self.assertLess(clock[0], 30)
        self.assertTrue(sock.closed)
        self.assertTrue(sock.stream.closed)

    def test_bad_framing_truncation_and_encoded_responses_are_rejected(self):
        responses = (
            b"HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\nx",
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nabc",
            b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n",
            b"HTTP/1.1 404 secret\r\n\r\n",
        )
        for response in responses:
            with self.subTest(response=response), self.assertRaises(media.MediaDownloadError) as error:
                self.download(response)
            self.assertNotIn("secret", str(error.exception))

    def test_url_credentials_malformed_urls_and_other_ports_are_rejected_before_dns(self):
        urls = (
            "file:///etc/passwd", "ftp://images.example/a", "http://user:secret@images.example/a",
            "http://@images.example/a", "http://images.example:8080/a", "https://images.example:80/a",
            "http://images.example:0/a", "http://images.example:bad/a", "http:///a",
            "http://[::1", "http://[fe80::1%25eth0]/a", "http://images.example/\r\nsecret",
            "http://images.example/ space", "http://images.example\\@127.0.0.1/a",
            "http://images.example/" + "x" * media.MAX_URL_LENGTH,
        )
        with patch.object(media, "_resolve") as resolve:
            for url in urls:
                with self.subTest(url=url[:100]), self.assertRaises(media.MediaDownloadError) as error:
                    media.download_image(url)
                self.assertNotIn("secret", str(error.exception))
            resolve.assert_not_called()

    def test_optional_allowlist_uses_exact_hostnames(self):
        with patch.object(media, "_resolve") as resolve:
            for host in ("evil.example", "images.example.evil.example", "sub.images.example"):
                with self.subTest(host=host), self.assertRaises(media.MediaDownloadError):
                    media.download_image(f"https://{host}/a", allowed_hosts={"images.example"})
            resolve.assert_not_called()
        self.assertEqual(
            media._destination("https://IMAGES.EXAMPLE./a", {"images.example"})[1], "images.example"
        )

    def test_network_errors_do_not_expose_signed_urls(self):
        secret = "https://user:password@images.example/image?token=secret"
        with patch.object(media, "_resolve", side_effect=OSError(secret)):
            with self.assertRaises(media.MediaDownloadError) as error:
                media.download_image("https://images.example/image?token=secret")
        self.assertEqual(str(error.exception), "Failed to download image.")


class ConfigurationTests(unittest.TestCase):
    def test_image_limit_defaults_to_twenty_mib(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(media._image_size_limit(), 20 * 1024 * 1024)

    def test_image_limit_can_be_configured_in_mib(self):
        for value in ("1", "5", "40"):
            with self.subTest(value=value), patch.dict(os.environ, {"MEDIA_MAX_IMAGE_MIB": value}):
                self.assertEqual(media._image_size_limit(), int(value) * 1024 * 1024)

    def test_invalid_image_limits_fail_without_echoing_the_value(self):
        for value in ("0", "-1", "1.5", "", "secret"):
            with self.subTest(value=value), patch.dict(os.environ, {"MEDIA_MAX_IMAGE_MIB": value}):
                with self.assertRaises(ValueError) as error:
                    media._image_size_limit()
                self.assertEqual(str(error.exception), "MEDIA_MAX_IMAGE_MIB must be a positive integer.")


class DestinationTests(unittest.TestCase):
    def test_private_special_and_transition_addresses_are_rejected(self):
        values = (
            "127.0.0.1", "0.0.0.0", "10.0.0.1", "172.16.0.1", "192.168.1.1",
            "169.254.169.254", "100.100.100.200", "192.0.0.8", "192.88.99.1",
            "198.18.0.1", "192.0.2.1", "224.0.0.1", "255.255.255.255",
            "::1", "::", "fc00::1", "fe80::1", "ff02::1", "2001:db8::1",
            "::ffff:127.0.0.1", "::ffff:93.184.216.34", "64:ff9b::a00:1",
            "64:ff9b:1::a00:1", "2002:7f00:1::", "2001::1", "3fff::1",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertFalse(media._public_address(value))
        self.assertTrue(media._public_address("93.184.216.34"))
        self.assertTrue(media._public_address("2606:4700:4700::1111"))

    def test_dns_answer_with_any_private_address_is_rejected(self):
        private = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 80, 0, 0))
        with patch.object(socket, "getaddrinfo", return_value=PUBLIC + [private]):
            with self.assertRaisesRegex(media.MediaDownloadError, "public address"):
                media._resolve("images.example", 80, time.monotonic() + 10)

    def test_private_literals_and_alternative_ip_spellings_are_resolved_and_rejected(self):
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        for host in ("127.0.0.1", "2130706433", "0177.0.0.1", "0x7f000001", "localhost"):
            with self.subTest(host=host), patch.object(socket, "getaddrinfo", return_value=private):
                with patch.object(media, "_connect") as connect, self.assertRaises(media.MediaDownloadError):
                    media.download_image(f"http://{host}/")
                connect.assert_not_called()

    def test_connection_does_not_repeat_dns_resolution(self):
        sock = Mock()
        with patch.object(socket, "socket", return_value=sock), patch.object(socket, "getaddrinfo") as resolve:
            self.assertIs(media._connect(PUBLIC, time.monotonic() + 10), sock)
        resolve.assert_not_called()
        sock.connect.assert_called_once_with(("93.184.216.34", 80))

    def test_connect_rearms_timeout_for_tls_with_remaining_budget(self):
        sock = Mock()
        with patch.object(socket, "socket", return_value=sock):
            with patch.object(media.time, "monotonic", side_effect=[0, 4]):
                media._connect(PUBLIC, deadline=6)
        self.assertEqual([call.args[0] for call in sock.settimeout.call_args_list], [5, 2])

    def test_failed_connection_is_closed(self):
        sock = Mock()
        sock.connect.side_effect = OSError("secret")
        with patch.object(socket, "socket", return_value=sock), self.assertRaises(OSError):
            media._connect(PUBLIC, time.monotonic() + 10)
        sock.close.assert_called_once()

    def test_dns_timeout_and_concurrency_are_bounded(self):
        future = Mock()
        future.result.side_effect = FutureTimeout()
        with patch.object(media._DNS_EXECUTOR, "submit", return_value=future):
            with patch.object(media, "_DNS_SLOTS") as slots:
                with self.assertRaisesRegex(media.MediaDownloadError, "resolution timed out"):
                    media._resolve("images.example", 80, time.monotonic() + 10)
                future.cancel.assert_called_once()
                future.add_done_callback.call_args.args[0](future)
                slots.release.assert_called_once()
        with patch.object(media, "_DNS_SLOTS") as slots, patch.object(media._DNS_EXECUTOR, "submit") as submit:
            slots.acquire.return_value = False
            with self.assertRaisesRegex(media.MediaDownloadError, "temporarily unavailable"):
                media._resolve("images.example", 80, time.monotonic() + 10)
            submit.assert_not_called()

    def test_deadline_is_enforced_for_slow_headers_and_chunk_framing(self):
        for response in (
            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n",
        ):
            # In the second case, deliver the headers immediately and trickle
            # only chunk framing/body bytes to exercise that separate path.
            fast_bytes = response.index(b"\r\n\r\n") + 4 if b"chunked" in response else 0

            class SlowStream(io.BytesIO):
                def readinto(self, buffer):
                    if self.tell() < fast_bytes:
                        return super().readinto(memoryview(buffer)[:fast_bytes - self.tell()])
                    clock[0] += 3
                    return super().readinto(memoryview(buffer)[:1])

            clock = [0]
            sock = FakeSocket(b"")
            sock.stream = SlowStream(response)
            with self.subTest(response=response), patch.object(media.time, "monotonic", side_effect=lambda: clock[0]):
                with patch.object(media, "_resolve", return_value=PUBLIC), patch.object(media, "_connect", return_value=sock):
                    with self.assertRaisesRegex(media.MediaDownloadError, "timed out"):
                        media.download_image("http://images.example/a")
            self.assertTrue(sock.closed)
            self.assertTrue(sock.stream.closed)


if __name__ == "__main__":
    unittest.main()
