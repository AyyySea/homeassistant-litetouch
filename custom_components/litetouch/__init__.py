"""LiteTouch 5000LC load-based integration."""
import logging
import socket
import threading
import time
import voluptuous as vol
from homeassistant.const import CONF_HOST, CONF_PORT, EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.discovery import load_platform
from .const import (
    DOMAIN, CONF_LOADS, CONF_SCENES, CONF_MODULE, CONF_CHANNEL,
    CONF_NAME, CONF_LOADID, CONF_DRIVE_SCENE, DEFAULT_PORT
)

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAY_INITIAL = 1.0   # seconds
RECONNECT_DELAY_MAX = 60.0      # seconds

LOAD_SCHEMA = vol.Schema({
    vol.Required(CONF_MODULE): cv.string,
    vol.Required(CONF_CHANNEL): vol.All(vol.Coerce(int), vol.Range(min=0, max=7)),
    vol.Required(CONF_NAME): cv.string,
    vol.Optional(CONF_DRIVE_SCENE, default=None): vol.Any(
        None, vol.All(vol.Coerce(int), vol.Range(min=1, max=256))
    ),
})

SCENE_SCHEMA = vol.Schema({
    vol.Required(CONF_LOADID): vol.All(vol.Coerce(int), vol.Range(min=1, max=256)),
    vol.Required(CONF_NAME): cv.string,
})

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Required(CONF_LOADS): vol.All(cv.ensure_list, [LOAD_SCHEMA]),
        vol.Optional(CONF_SCENES, default=[]): vol.All(cv.ensure_list, [SCENE_SCHEMA]),
    })
}, extra=vol.ALLOW_EXTRA)


class LiteTouchController:
    """Manages the TCP connection to the controller.

    Drives loads via CINLL/CSLOF on scenes; tracks state by listening for
    RMODU broadcasts. A background thread owns the connection: it connects,
    reads, and on any failure tears the socket down and reconnects with
    exponential backoff. Commands sent while disconnected are dropped with a
    warning rather than queued -- a stale light command replayed minutes
    later is worse than a lost one.
    """

    def __init__(self, host, port, hass):
        self.host = host
        self.port = port
        self.hass = hass
        self._sock = None
        self._send_lock = threading.Lock()
        self._thread = None
        self._stop_event = threading.Event()
        self._connected = False

    def start(self):
        """Start the connection-manager thread. Returns immediately."""
        self._thread = threading.Thread(
            target=self._run, name="litetouch-reader", daemon=True
        )
        self._thread.start()

    def stop(self):
        """Shut down: stop reconnect attempts and close the socket."""
        self._stop_event.set()
        self._close_socket()

    @property
    def connected(self):
        return self._connected

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _run(self):
        """Connect / read / reconnect loop. Lives for the process lifetime."""
        delay = RECONNECT_DELAY_INITIAL
        while not self._stop_event.is_set():
            try:
                self._connect()
            except OSError as err:
                _LOGGER.warning(
                    "LiteTouch connect to %s:%s failed (%s); retrying in %.0fs",
                    self.host, self.port, err, delay,
                )
                if self._stop_event.wait(delay):
                    return
                delay = min(delay * 2, RECONNECT_DELAY_MAX)
                continue

            delay = RECONNECT_DELAY_INITIAL
            _LOGGER.info("LiteTouch connected to %s:%s", self.host, self.port)
            self._read_until_failure()
            self._mark_disconnected()
            if not self._stop_event.is_set():
                _LOGGER.warning("LiteTouch connection lost; reconnecting")

    def _connect(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Detect a silently-dead peer (e.g. an XPort power cycle) via TCP
            # keepalive, with tighter timing where the platform supports it
            # (~minutes instead of the kernel default of ~2 hours).
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt, val in (
                ("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)
            ):
                if hasattr(socket, opt):
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
            sock.settimeout(10)
            sock.connect((self.host, self.port))
            # Enable event broadcasts (RMODU + RLEDU + REVNT; SIEVN,7 = all).
            sock.sendall(b"R,SIEVN,7\r")
            # Drain the ack so the reader starts on a clean line boundary.
            sock.settimeout(0.3)
            try:
                sock.recv(2048)
            except socket.timeout:
                pass
            sock.settimeout(0.5)
        except OSError:
            try:
                sock.close()
            except OSError:
                pass
            raise
        self._sock = sock
        self._connected = True

    def _read_until_failure(self):
        """Read and parse lines until the socket dies or we're stopped."""
        buf = bytearray()
        while not self._stop_event.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:  # peer closed the connection
                return
            for b in chunk:
                if b == 0x0D:
                    line = buf.decode("ascii", errors="replace")
                    buf = bytearray()
                    self._process_line(line)
                else:
                    buf.append(b)

    def _mark_disconnected(self):
        """Tear down the socket. Safe to call from any thread, repeatedly."""
        self._connected = False
        self._close_socket()

    def _close_socket(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                # shutdown() wakes a reader blocked in recv() immediately;
                # close() alone can leave it waiting out its timeout.
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    def _process_line(self, line):
        """Parse an incoming line. We only care about RMODU broadcasts."""
        if "RMODU" not in line:
            return
        parts = line.strip().split(',')
        # Format: R, RMODU, MMMM, FF, lvl0, lvl1, ..., lvl7
        if len(parts) < 12:
            return
        try:
            module = parts[2].upper().zfill(4)
            levels = []
            for lvl_str in parts[4:12]:
                try:
                    levels.append(int(lvl_str))
                except ValueError:
                    levels.append(-1)
            signal = f"litetouch_module_{module}"
            self.hass.loop.call_soon_threadsafe(
                async_dispatcher_send, self.hass, signal, levels
            )
        except Exception as e:
            _LOGGER.warning("Failed to parse RMODU line %r: %s", line, e)

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    def _send(self, cmd_bytes):
        sock = self._sock
        if not self._connected or sock is None:
            _LOGGER.warning("LiteTouch not connected; dropping command %r", cmd_bytes)
            return
        try:
            with self._send_lock:
                sock.sendall(cmd_bytes)
        except OSError as err:
            # Closing the socket also unblocks the reader thread, which then
            # drives the reconnect loop.
            _LOGGER.error("LiteTouch send failed (%s); reconnecting", err)
            self._mark_disconnected()

    def set_scene_level(self, loadid, level):
        """CINLL: fire scene at the given level 0-100. loadid is 1-indexed.

        The protocol uses 0-indexed scene numbers on the wire. Any hardware
        CGMAX cap programmed on the controller clamps the effective level.
        """
        level = max(0, min(100, int(level)))
        cmd = f"R,CINLL,{loadid - 1},{level}\r".encode('ascii')
        self._send(cmd)

    def fire_scene_on(self, loadid):
        """CSLON: turn a scene on at its programmed levels."""
        self._send(f"R,CSLON,{loadid - 1}\r".encode('ascii'))

    def fire_scene_off(self, loadid):
        """CSLOF: turn a scene off (all loads in scene -> 0)."""
        self._send(f"R,CSLOF,{loadid - 1}\r".encode('ascii'))


def setup(hass, config):
    conf = config[DOMAIN]
    controller = LiteTouchController(
        conf[CONF_HOST], conf[CONF_PORT], hass
    )
    # Never block (or fail) HA startup on the controller being reachable:
    # the background thread keeps retrying with backoff until it connects.
    controller.start()

    hass.data[DOMAIN] = {
        'controller': controller,
        'loads': conf[CONF_LOADS],
        'scenes': conf.get(CONF_SCENES, []),
    }
    hass.bus.listen_once(
        EVENT_HOMEASSISTANT_STOP, lambda event: controller.stop()
    )
    load_platform(hass, 'light', DOMAIN, {}, config)
    return True
