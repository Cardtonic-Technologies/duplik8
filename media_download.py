"""Bounded HTTP image downloads that cannot connect to private destinations."""

import http.client
import io
import ipaddress
import os
import re
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from urllib.parse import urlsplit


def _image_size_limit():
    try:
        size_mib = int(os.getenv("MEDIA_MAX_IMAGE_MIB", "20"))
        if size_mib <= 0:
            raise ValueError
    except ValueError:
        raise ValueError("MEDIA_MAX_IMAGE_MIB must be a positive integer.") from None
    return size_mib * 1024 * 1024


MAX_IMAGE_BYTES = _image_size_limit()
MAX_URL_LENGTH = 8192
DOWNLOAD_TIMEOUT = 30.0
IO_TIMEOUT = 5.0
CHUNK_SIZE = 64 * 1024

# Bound both resolver concurrency and queued work if the system resolver stalls.
_DNS_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="media-dns")
_DNS_SLOTS = threading.BoundedSemaphore(4)
_SPECIAL_IPV4 = (
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.88.99.0/24"),
)
_SPECIAL_IPV6 = (
    ipaddress.ip_network("2001::/23"),
    ipaddress.ip_network("2002::/16"),
    ipaddress.ip_network("3fff::/20"),
)


class MediaDownloadError(Exception):
    """Only fixed, URL-free messages from this exception reach API clients."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MediaDownloadError("Image download timed out.")
    return min(IO_TIMEOUT, remaining)


def _destination(url, allowed_hosts):
    if (
        not url
        or len(url) > MAX_URL_LENGTH
        or "\\" in url
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)
    ):
        raise MediaDownloadError("Invalid image URL.")
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (ValueError, UnicodeError):
        raise MediaDownloadError("Invalid image URL.") from None

    default_port = {"http": 80, "https": 443}.get(parsed.scheme)
    if (
        default_port is None
        or not hostname
        or "%" in hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and port != default_port)
    ):
        raise MediaDownloadError("Image URL destination is not allowed.")
    if allowed_hosts and hostname not in allowed_hosts:
        raise MediaDownloadError("Image URL host is not allowed.")

    # Keep the query intact for signed media URLs, but never send a fragment.
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    return parsed.scheme, hostname, default_port, target


def _public_address(value):
    address = ipaddress.ip_address(value)
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
    ):
        return False
    if address.version == 4:
        return not any(address in network for network in _SPECIAL_IPV4)
    # Exclude transition/NAT64 addresses and special ranges consistently on
    # Python 3.10 as well as newer ipaddress versions.
    return address in ipaddress.ip_network("2000::/3") and not any(
        address in network for network in _SPECIAL_IPV6
    )


def _resolve(hostname, port, deadline):
    if not _DNS_SLOTS.acquire(blocking=False):
        raise MediaDownloadError("Image host resolution is temporarily unavailable.")
    try:
        future = _DNS_EXECUTOR.submit(socket.getaddrinfo, hostname, port, 0, socket.SOCK_STREAM)
    except Exception:
        _DNS_SLOTS.release()
        raise
    future.add_done_callback(lambda _: _DNS_SLOTS.release())
    try:
        addresses = future.result(timeout=_remaining(deadline))
    except FutureTimeout:
        future.cancel()
        raise MediaDownloadError("Image host resolution timed out.") from None
    if not addresses:
        raise MediaDownloadError("Image host could not be resolved.")
    # Reject the entire answer if ANY address is unsafe, including mixed A/AAAA.
    for family, kind, protocol, _, address in addresses:
        if family not in (socket.AF_INET, socket.AF_INET6) or not _public_address(address[0]):
            raise MediaDownloadError("Image URL must resolve to a public address.")
    return addresses


def _connect(addresses, deadline):
    last_error = None
    for family, kind, protocol, _, address in addresses:
        sock = socket.socket(family, kind, protocol)
        try:
            sock.settimeout(_remaining(deadline))
            # Connect to the validated sockaddr directly; never resolve again.
            sock.connect(address)
            # HTTPS uses this socket's timeout for its TLS handshake next.
            sock.settimeout(_remaining(deadline))
            return sock
        except (OSError, MediaDownloadError) as error:
            sock.close()
            last_error = error
    raise last_error


class _DeadlineReader(io.RawIOBase):
    def __init__(self, sock, deadline):
        super().__init__()
        self.sock = sock
        self.deadline = deadline
        # A real socket file keeps the descriptor alive when http.client closes
        # its connection after seeing HTTP/1.0 or a Connection: close header.
        self.stream = sock.makefile("rb", buffering=0)

    def readable(self):
        return True

    def readinto(self, buffer):
        # Re-arm on every socket read, including headers and chunk framing.
        # A peer sending a byte at a time cannot reset the overall deadline.
        self.sock.settimeout(_remaining(self.deadline))
        return self.stream.readinto(buffer)

    def close(self):
        try:
            self.stream.close()
        finally:
            super().close()


class _ResponseSocket:
    def __init__(self, sock, deadline):
        self.sock = sock
        self.deadline = deadline

    def makefile(self, mode):
        return io.BufferedReader(_DeadlineReader(self.sock, self.deadline))


def download_image(url, allowed_hosts=()):
    """Fetch within the configured size limit and 30 seconds, without redirects or ambient auth."""
    scheme, hostname, port, target = _destination(url, allowed_hosts)
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    connection = None
    response = None
    try:
        addresses = _resolve(hostname, port, deadline)
        if scheme == "https":
            connection = http.client.HTTPSConnection(
                hostname, port, timeout=_remaining(deadline), context=ssl.create_default_context()
            )
        else:
            connection = http.client.HTTPConnection(hostname, port, timeout=_remaining(deadline))

        # http.client retains the original host for Host, TLS SNI and certificate
        # verification. Only its TCP connection factory uses the pinned IPs.
        connection._create_connection = lambda *args, **kwargs: _connect(addresses, deadline)
        connection.response_class = lambda sock, **kwargs: http.client.HTTPResponse(
            _ResponseSocket(sock, deadline), **kwargs
        )
        connection.connect()
        connection.sock.settimeout(_remaining(deadline))
        connection.request("GET", target, headers={"Accept": "image/*", "Accept-Encoding": "identity"})
        connection.sock.settimeout(_remaining(deadline))
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise MediaDownloadError("Image URL redirects are not allowed.")
        if response.status != 200:
            raise MediaDownloadError("Image server did not return a successful response.")
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise MediaDownloadError("Encoded image downloads are not allowed.")

        length = response.getheader("Content-Length")
        if length is not None:
            if not re.fullmatch(r"[0-9]+", length) or len(length) > 20:
                raise MediaDownloadError("Invalid image download size.")
            if int(length) > MAX_IMAGE_BYTES:
                raise MediaDownloadError("Image exceeds the download size limit.", 413)
        transfer_encoding = response.getheader("Transfer-Encoding")
        if transfer_encoding and (transfer_encoding.lower() != "chunked" or length is not None):
            raise MediaDownloadError("Invalid image response framing.")

        body = bytearray()
        while True:
            _remaining(deadline)
            chunk = response.read1(min(CHUNK_SIZE, MAX_IMAGE_BYTES + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_IMAGE_BYTES:
                raise MediaDownloadError("Image exceeds the download size limit.", 413)
        if length is not None and len(body) != int(length):
            raise MediaDownloadError("Incomplete image download.")
        return bytes(body)
    except MediaDownloadError:
        raise
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        # Never include exceptions, request URLs, response headers or Location:
        # these can contain userinfo or a signed URL's credentials.
        raise MediaDownloadError("Failed to download image.") from None
    finally:
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()
