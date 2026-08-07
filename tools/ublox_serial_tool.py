#!/usr/bin/env python3
"""Small Tkinter serial console for u-blox AT/EDM testing."""

from __future__ import annotations

import csv
import queue
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk
from typing import Callable

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - handled in the UI at runtime.
    serial = None
    list_ports = None


EDM_HEAD = 0xAA
EDM_TAIL = 0x55
EDM_AT_REQUEST = 0x44
EDM_AT_RESPONSE = 0x45
EDM_AT_EVENT = 0x41
EDM_DATA_COMMAND = 0x36
EDM_CONNECT_EVENT = 0x11
EDM_DISCONNECT_EVENT = 0x21
EDM_DATA_EVENT = 0x31
EDM_START_EVENT = 0x71

EDM_CONNECTION_BT = 0x01
EDM_CONNECTION_IPV4 = 0x02
EDM_CONNECTION_IPV6 = 0x03
AT_RESPONSE_TIMEOUT_MS = 3000

EDM_FRAME_TYPE_OPTIONS = (
    ("command DATA_COMMAND (0x36)", EDM_DATA_COMMAND),
    ("command AT_REQUEST (0x44)", EDM_AT_REQUEST),
    ("response AT_RESPONSE (0x45)", EDM_AT_RESPONSE),
    ("event CONNECT_EVENT (0x11)", EDM_CONNECT_EVENT),
    ("event DISCONNECT_EVENT (0x21)", EDM_DISCONNECT_EVENT),
    ("event DATA_EVENT (0x31)", EDM_DATA_EVENT),
    ("event AT_EVENT (0x41)", EDM_AT_EVENT),
    ("event START_EVENT (0x71)", EDM_START_EVENT),
)
EDM_FRAME_TYPE_BY_LABEL = dict(EDM_FRAME_TYPE_OPTIONS)
EDM_DATA_FRAME_TYPES = (EDM_DATA_COMMAND, EDM_DATA_EVENT)

SECURITY_OPTIONS = (
    "Open (0)",
    "WPA2 (2)",
)
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
UDCP_RE = re.compile(r"\+UDCP:\s*(\d+)", re.IGNORECASE)
UDLP_RE = re.compile(r"\+UDLP:\s*(\d+)(?:,(.*))?", re.IGNORECASE)
AT_OK_RE = re.compile(r"(^|[\r\n])OK([\r\n]|$)", re.IGNORECASE)
AT_ERROR_RE = re.compile(r"(^|[\r\n])(?:ERROR|\+CME ERROR:.*|\+CMS ERROR:.*)([\r\n]|$)", re.IGNORECASE)


def bytes_to_hex(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def bytes_to_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def parse_hex(text: str) -> bytes:
    cleaned = (
        text.replace("[", " ")
        .replace("]", " ")
        .replace("{", " ")
        .replace("}", " ")
        .replace(",", " ")
        .replace(";", " ")
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("\t", " ")
    )
    parts = [part for part in cleaned.split(" ") if part]
    if len(parts) == 1:
        token = parts[0].removeprefix("0x").removeprefix("0X")
        if len(token) > 2 and len(token) % 2 == 0:
            return bytes.fromhex(token)

    values: list[int] = []
    for part in parts:
        token = part.removeprefix("0x").removeprefix("0X")
        if len(token) > 2:
            raise ValueError(f"Hex byte too long: {part}")
        values.append(int(token, 16))
    return bytes(values)


def append_line_ending(data: bytes, ending: str) -> bytes:
    if ending == "CR":
        return data + b"\r"
    if ending == "LF":
        return data + b"\n"
    if ending == "CRLF":
        return data + b"\r\n"
    return data


def quote_at_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_security_value(text: str) -> int:
    match = re.search(r"\((\d+)\)", text)
    token = match.group(1) if match else text.strip()
    value = int(token, 10)
    if value < 0:
        raise ValueError("Security value must be zero or greater")
    return value


def is_valid_ipv4(address: str) -> bool:
    parts = address.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(part) <= 255 for part in parts)
    except ValueError:
        return False


def split_at_csv_fields(text: str) -> list[str]:
    try:
        return [field.strip().strip('"') for field in next(csv.reader([text], skipinitialspace=True))]
    except csv.Error:
        return [field.strip().strip('"') for field in text.split(",")]


def first_valid_ipv4(text: str) -> str | None:
    for match in IPV4_RE.finditer(text):
        address = match.group(0)
        if address in ("0.0.0.0", "255.255.255.255"):
            continue
        if address.startswith("127."):
            continue
        if is_valid_ipv4(address):
            return address
    return None


def normalize_socket_detail(detail: str) -> str:
    normalized = detail.strip()
    if normalized.upper().startswith("AT+UDCP"):
        _prefix, _separator, normalized = normalized.partition("=")
    return normalized.strip().strip('"').lower()


def parse_unstat_ip_101(text: str) -> str | None:
    for line in text.splitlines():
        if "+UNSTAT" not in line.upper() or ":" not in line:
            continue

        fields = split_at_csv_fields(line.split(":", 1)[1].strip())
        for index, field in enumerate(fields):
            if field != "101":
                continue
            for candidate in fields[index + 1 :]:
                address = first_valid_ipv4(candidate)
                if address:
                    return address

        if "101" in fields:
            address = first_valid_ipv4(line)
            if address:
                return address
    return None


def make_edm_head(frame_type: int, payload_length: int) -> bytes:
    length = payload_length + 2
    return bytes([EDM_HEAD, (length >> 8) & 0xFF, length & 0xFF, 0x00, frame_type])


def make_edm_tail() -> bytes:
    return bytes([EDM_TAIL])


def make_edm_frame(frame_type: int, payload: bytes) -> bytes:
    return make_edm_head(frame_type, len(payload)) + payload + make_edm_tail()


def make_edm_data_head(frame_type: int, channel: int, payload_length: int) -> bytes:
    length = payload_length + 3
    return bytes([EDM_HEAD, (length >> 8) & 0xFF, length & 0xFF, 0x00, frame_type, channel])


def make_edm_data_frame(frame_type: int, channel: int, payload: bytes) -> bytes:
    return make_edm_data_head(frame_type, channel, len(payload)) + payload + make_edm_tail()


def make_edm_at_request(command: bytes) -> bytes:
    return make_edm_frame(EDM_AT_REQUEST, command)


def make_edm_data_command(channel: int, payload: bytes) -> bytes:
    return make_edm_data_frame(EDM_DATA_COMMAND, channel, payload)


@dataclass
class EdmFrame:
    frame_type: int
    payload: bytes
    raw: bytes


@dataclass
class SocketEntry:
    key: str
    kind: str
    handle: str
    close_command: str
    detail: str
    status: str = "open"


class EdmParser:
    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[EdmFrame]:
        self._buffer.extend(data)
        frames: list[EdmFrame] = []

        while True:
            try:
                start = self._buffer.index(EDM_HEAD)
            except ValueError:
                self._buffer.clear()
                return frames

            if start:
                del self._buffer[:start]

            if len(self._buffer) < 6:
                return frames

            length = (self._buffer[1] << 8) | self._buffer[2]
            total = 3 + length + 1
            if length < 2:
                del self._buffer[0]
                continue
            if len(self._buffer) < total:
                return frames
            if self._buffer[total - 1] != EDM_TAIL:
                del self._buffer[0]
                continue

            raw = bytes(self._buffer[:total])
            body = raw[3:-1]
            frame_type = body[1]
            payload = body[2:]
            frames.append(EdmFrame(frame_type=frame_type, payload=payload, raw=raw))
            del self._buffer[:total]


class SerialWorker:
    def __init__(self, inbox: queue.Queue[tuple[str, bytes | str]]) -> None:
        self._serial = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._inbox = inbox

    @property
    def is_open(self) -> bool:
        return bool(self._serial and self._serial.is_open)

    def open(self, port: str, baudrate: int) -> None:
        if serial is None:
            raise RuntimeError("pyserial is not installed. Run: pip install pyserial")
        if self.is_open:
            self.close()

        self._stop.clear()
        self._serial = serial.Serial(port=port, baudrate=baudrate, timeout=0.05)
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self._thread = None
        if self._serial:
            self._serial.close()
        self._serial = None

    def write(self, data: bytes) -> None:
        if not self.is_open:
            raise RuntimeError("Serial port is not open")
        self._serial.write(data)

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._serial is None:
                    return
                data = self._serial.read(4096)
                if data:
                    self._inbox.put(("rx", data))
                else:
                    time.sleep(0.01)
            except Exception as exc:  # pragma: no cover - depends on serial hardware.
                self._inbox.put(("error", str(exc)))
                return


class UbloxSerialTool(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("u-blox AT/EDM Serial Tool")
        self.geometry("1120x820")
        self.minsize(900, 640)

        self._inbox: queue.Queue[tuple[str, bytes | str]] = queue.Queue()
        self._serial = SerialWorker(self._inbox)
        self._edm_parser = EdmParser()
        self._syncing = False
        self._at_sequence_commands: list[str] = []
        self._at_sequence_index = 0
        self._at_sequence_tag = ""
        self._at_sequence_is_edm = False
        self._at_sequence_id = 0
        self._at_response_buffer = ""
        self._at_timeout_after_id: str | None = None
        self._at_sequence_on_complete: Callable[[], None] | None = None
        self._socket_entries: dict[str, SocketEntry] = {}
        self._pending_udcp_detail = ""

        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="115200")
        self.mode_var = tk.StringVar(value="Command")
        self.edm_send_var = tk.StringVar(value="command AT_REQUEST (0x44)")
        self.channel_var = tk.StringVar(value="02")
        self.udcp_var = tk.StringVar(value="tcp://192.168.1.7:8888")
        self.wifi_ssid_var = tk.StringVar(value="NguyenTiem ")
        self.wifi_password_var = tk.StringVar(value="111111111")
        self.wifi_security_var = tk.StringVar(value="WPA2 (2)")
        self.current_ip_var = tk.StringVar(value="-")
        self.server_id_var = tk.StringVar(value="0")
        self.server_protocol_var = tk.StringVar(value="tcp")
        self.server_ip_var = tk.StringVar(value="0.0.0.0")
        self.server_port_var = tk.StringVar(value="8888")
        self.line_ending_var = tk.StringVar(value="CRLF")
        self.status_var = tk.StringVar(value="Disconnected")
        self.mode_var.trace_add("write", self._update_edm_controls)
        self.edm_send_var.trace_add("write", self._update_edm_controls)

        self._build_ui()
        self.refresh_ports()
        self.after(50, self._poll_inbox)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if serial is None:
            messagebox.showerror("Missing dependency", "pyserial is required.\nRun: pip install pyserial")

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)

        connection = ttk.LabelFrame(self, text="Connection")
        connection.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        connection.columnconfigure(1, weight=1)

        ttk.Label(connection, text="COM").grid(row=0, column=0, padx=(8, 4), pady=8)
        self.port_combo = ttk.Combobox(connection, textvariable=self.port_var, width=28, state="readonly")
        self.port_combo.grid(row=0, column=1, sticky="ew", padx=4, pady=8)
        ttk.Button(connection, text="Refresh", command=self.refresh_ports).grid(row=0, column=2, padx=4, pady=8)

        ttk.Label(connection, text="Baud").grid(row=0, column=3, padx=(16, 4), pady=8)
        self.baud_combo = ttk.Combobox(
            connection,
            textvariable=self.baud_var,
            width=12,
            values=("9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"),
        )
        self.baud_combo.grid(row=0, column=4, padx=4, pady=8)
        ttk.Button(connection, text="Open", command=self.open_port).grid(row=0, column=5, padx=4, pady=8)
        ttk.Button(connection, text="Close", command=self.close_port).grid(row=0, column=6, padx=(4, 8), pady=8)

        mode = ttk.LabelFrame(self, text="Mode")
        mode.grid(row=1, column=0, sticky="ew", padx=10, pady=6)

        ttk.Radiobutton(mode, text="Command mode", value="Command", variable=self.mode_var).grid(row=0, column=0, padx=8, pady=8)
        ttk.Radiobutton(mode, text="EDM mode", value="EDM", variable=self.mode_var).grid(row=0, column=1, padx=8, pady=8)
        ttk.Button(mode, text="Apply mode", command=self.apply_mode).grid(row=0, column=2, padx=(16, 8), pady=8)
        ttk.Button(mode, text="Reset modem", command=self.reset_modem).grid(row=0, column=3, padx=8, pady=8)
        ttk.Label(mode, text="Enter EDM: raw ATO2. Exit EDM: EDM AT_REQUEST ATO0. Reset: AT+CPWROFF.").grid(row=0, column=4, sticky="w", padx=8, pady=8)

        quick = ttk.Frame(self)
        quick.grid(row=2, column=0, sticky="ew", padx=10, pady=6)
        quick.columnconfigure(0, weight=1)
        quick.columnconfigure(1, weight=1)

        wifi = ttk.LabelFrame(quick, text="Wi-Fi station")
        wifi.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        wifi.columnconfigure(1, weight=1)
        wifi.columnconfigure(3, weight=1)

        ttk.Label(wifi, text="SSID").grid(row=0, column=0, padx=(8, 4), pady=(8, 4), sticky="e")
        ttk.Entry(wifi, textvariable=self.wifi_ssid_var, width=24).grid(row=0, column=1, padx=4, pady=(8, 4), sticky="ew")
        ttk.Label(wifi, text="Security").grid(row=0, column=2, padx=(8, 4), pady=(8, 4), sticky="e")
        ttk.Combobox(
            wifi,
            textvariable=self.wifi_security_var,
            values=SECURITY_OPTIONS,
            width=14,
        ).grid(row=0, column=3, padx=(4, 8), pady=(8, 4), sticky="ew")
        ttk.Label(wifi, text="Password").grid(row=1, column=0, padx=(8, 4), pady=(4, 8), sticky="e")
        ttk.Entry(wifi, textvariable=self.wifi_password_var, width=24, show="*").grid(row=1, column=1, padx=4, pady=(4, 8), sticky="ew")
        ttk.Button(wifi, text="Connect Wi-Fi", command=self.connect_wifi).grid(row=1, column=2, columnspan=2, padx=(8, 8), pady=(4, 4), sticky="ew")
        ttk.Label(wifi, text="IP").grid(row=2, column=0, padx=(8, 4), pady=(4, 8), sticky="e")
        ttk.Label(wifi, textvariable=self.current_ip_var).grid(row=2, column=1, padx=4, pady=(4, 8), sticky="w")
        ttk.Button(wifi, text="Disconnect Wi-Fi", command=self.disconnect_wifi).grid(row=2, column=2, padx=(8, 4), pady=(4, 8), sticky="ew")
        ttk.Button(wifi, text="Get IP", command=self.get_wifi_status).grid(row=2, column=3, padx=(4, 8), pady=(4, 8), sticky="ew")

        server = ttk.LabelFrame(quick, text="Server socket")
        server.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        server.columnconfigure(1, weight=1)
        server.columnconfigure(3, weight=1)

        ttk.Label(server, text="ID").grid(row=0, column=0, padx=(8, 4), pady=(8, 4), sticky="e")
        ttk.Combobox(
            server,
            textvariable=self.server_id_var,
            values=tuple(str(index) for index in range(7)),
            width=4,
            state="readonly",
        ).grid(row=0, column=1, padx=4, pady=(8, 4), sticky="w")
        ttk.Label(server, text="Protocol").grid(row=0, column=2, padx=(8, 4), pady=(8, 4), sticky="e")
        ttk.Combobox(
            server,
            textvariable=self.server_protocol_var,
            values=("tcp", "udp"),
            width=6,
            state="readonly",
        ).grid(row=0, column=3, padx=(4, 8), pady=(8, 4), sticky="ew")
        ttk.Label(server, text="IP").grid(row=1, column=0, padx=(8, 4), pady=(4, 4), sticky="e")
        ttk.Entry(server, textvariable=self.server_ip_var, width=18).grid(row=1, column=1, padx=4, pady=(4, 4), sticky="ew")
        ttk.Label(server, text="Port").grid(row=1, column=2, padx=(8, 4), pady=(4, 8), sticky="e")
        ttk.Entry(server, textvariable=self.server_port_var, width=8).grid(row=1, column=3, padx=(4, 8), pady=(4, 4), sticky="ew")
        ttk.Button(server, text="Create server", command=self.create_server_socket).grid(row=2, column=0, columnspan=4, padx=(8, 8), pady=(4, 8), sticky="ew")

        sockets = ttk.LabelFrame(self, text="Open sockets")
        sockets.grid(row=3, column=0, sticky="ew", padx=10, pady=6)
        sockets.columnconfigure(0, weight=1)
        self.socket_tree = ttk.Treeview(
            sockets,
            columns=("kind", "handle", "detail", "status"),
            show="headings",
            height=4,
            selectmode="browse",
        )
        self.socket_tree.heading("kind", text="Type")
        self.socket_tree.heading("handle", text="Handle/ID")
        self.socket_tree.heading("detail", text="Detail")
        self.socket_tree.heading("status", text="Status")
        self.socket_tree.column("kind", width=90, anchor="w")
        self.socket_tree.column("handle", width=90, anchor="w")
        self.socket_tree.column("detail", width=650, anchor="w")
        self.socket_tree.column("status", width=80, anchor="w")
        self.socket_tree.grid(row=0, column=0, rowspan=2, sticky="ew", padx=(8, 4), pady=8)
        ttk.Button(sockets, text="Refresh sockets", command=self.refresh_socket_list).grid(row=0, column=1, sticky="ew", padx=(4, 8), pady=(8, 4))
        ttk.Button(sockets, text="Close selected", command=self.close_selected_socket).grid(row=1, column=1, sticky="ew", padx=(4, 8), pady=(4, 8))

        send = ttk.LabelFrame(self, text="Send")
        send.grid(row=4, column=0, sticky="nsew", padx=10, pady=6)
        send.columnconfigure(1, weight=1)
        send.columnconfigure(3, weight=1)

        ttk.Label(send, text="String").grid(row=0, column=0, sticky="nw", padx=(8, 4), pady=(8, 4))
        self.tx_text = tk.Text(send, height=5, wrap="word", undo=True)
        self.tx_text.grid(row=0, column=1, sticky="nsew", padx=4, pady=(8, 4))
        self.tx_text.bind("<KeyRelease>", self._sync_tx_from_text)

        ttk.Label(send, text="Hex arr").grid(row=0, column=2, sticky="nw", padx=(8, 4), pady=(8, 4))
        self.tx_hex = tk.Text(send, height=5, wrap="word", undo=True)
        self.tx_hex.grid(row=0, column=3, sticky="nsew", padx=(4, 8), pady=(8, 4))
        self.tx_hex.bind("<KeyRelease>", self._sync_tx_from_hex)

        options = ttk.Frame(send)
        options.grid(row=1, column=0, columnspan=4, sticky="ew", padx=8, pady=(4, 8))

        ttk.Label(options, text="Client").pack(side="left")
        ttk.Entry(options, textvariable=self.udcp_var, width=28).pack(side="left", padx=(4, 8))
        ttk.Button(options, text="Open client", command=self.open_udcp_channel).pack(side="left", padx=(0, 16))

        ttk.Label(options, text="Line ending").pack(side="left")
        ttk.Combobox(
            options,
            textvariable=self.line_ending_var,
            width=8,
            values=("None", "CR", "LF", "CRLF"),
            state="readonly",
        ).pack(side="left", padx=(4, 16))

        self.edm_type_label = ttk.Label(options, text="EDM frame")
        self.edm_type_label.pack(side="left")
        self.edm_type_combo = ttk.Combobox(
            options,
            textvariable=self.edm_send_var,
            width=30,
            values=tuple(label for label, _frame_type in EDM_FRAME_TYPE_OPTIONS),
            state="readonly",
        )
        self.edm_type_combo.pack(side="left", padx=(4, 16))

        self.channel_label = ttk.Label(options, text="Channel hex")
        self.channel_label.pack(side="left")
        self.channel_entry = ttk.Entry(options, textvariable=self.channel_var, width=5)
        self.channel_entry.pack(side="left", padx=(4, 16))

        ttk.Button(options, text="Send", command=self.send_payload).pack(side="left")
        ttk.Button(options, text="Clear TX", command=self.clear_tx).pack(side="left", padx=(8, 0))

        receive = ttk.LabelFrame(self, text="Receive")
        receive.grid(row=5, column=0, sticky="nsew", padx=10, pady=6)
        receive.columnconfigure(0, weight=1)
        receive.columnconfigure(1, weight=1)
        receive.rowconfigure(1, weight=1)

        ttk.Label(receive, text="String").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))
        ttk.Label(receive, text="Hex arr / EDM parse").grid(row=0, column=1, sticky="w", padx=8, pady=(8, 4))

        self.rx_text = tk.Text(receive, wrap="word", state="disabled")
        self.rx_text.grid(row=1, column=0, sticky="nsew", padx=(8, 4), pady=(0, 8))
        self.rx_hex = tk.Text(receive, wrap="word", state="disabled")
        self.rx_hex.grid(row=1, column=1, sticky="nsew", padx=(4, 8), pady=(0, 8))

        bottom = ttk.Frame(self)
        bottom.grid(row=6, column=0, sticky="ew", padx=10, pady=(0, 10))
        bottom.columnconfigure(0, weight=1)
        ttk.Label(bottom, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        ttk.Button(bottom, text="Clear RX", command=self.clear_rx).grid(row=0, column=1, sticky="e")
        self._update_edm_controls()

    def refresh_ports(self) -> None:
        if list_ports is None:
            self.port_combo["values"] = ()
            return
        ports = [port.device for port in list_ports.comports()]
        self.port_combo["values"] = ports
        if ports and self.port_var.get() not in ports:
            self.port_var.set(ports[0])

    def open_port(self) -> None:
        try:
            port = self.port_var.get().strip()
            if not port:
                raise RuntimeError("Select a COM port")
            self._serial.open(port, int(self.baud_var.get()))
            self.status_var.set(f"Connected: {port} @ {self.baud_var.get()}")
        except Exception as exc:
            messagebox.showerror("Open failed", str(exc))

    def close_port(self) -> None:
        self._clear_at_sequence()
        self._serial.close()
        self.status_var.set("Disconnected")

    def apply_mode(self) -> None:
        if self.mode_var.get() == "EDM":
            data = b"\r\nATO2\r\n"
            tag = "enter EDM raw ATO2"
        else:
            data = make_edm_at_request(b"ATO0\r")
            tag = "exit EDM AT_REQUEST ATO0"
        self._write_serial(data, tag=tag)

    def reset_modem(self) -> None:
        try:
            self._send_at_commands(["AT+CPWROFF"], tag="reset modem")
        except Exception as exc:
            messagebox.showerror("Reset modem failed", str(exc))

    def connect_wifi(self) -> None:
        try:
            ssid = self.wifi_ssid_var.get()
            password = self.wifi_password_var.get()
            if ssid == "":
                raise ValueError("Enter Wi-Fi SSID")
            security = parse_security_value(self.wifi_security_var.get())
            commands = [
                "AT+UWSCA=0,1",
                f"AT+UWSC=0,2,{quote_at_string(ssid)}",
                f"AT+UWSC=0,5,{security}",
                f"AT+UWSC=0,8,{quote_at_string(password)}",
                "AT+UWSCA=0,3",
            ]
            self.current_ip_var.set("-")
            self._send_at_commands(
                commands,
                tag="connect Wi-Fi",
                on_complete=lambda: self.after(1000, lambda: self.get_wifi_status(retry_attempts=4, show_errors=False)),
            )
        except Exception as exc:
            messagebox.showerror("Connect Wi-Fi failed", str(exc))

    def disconnect_wifi(self) -> None:
        try:
            self._send_at_commands(
                ["AT+UWSCA=0,4"],
                tag="disconnect Wi-Fi",
                on_complete=lambda: self.current_ip_var.set("-"),
            )
        except Exception as exc:
            messagebox.showerror("Disconnect Wi-Fi failed", str(exc))

    def get_wifi_status(self, retry_attempts: int = 0, show_errors: bool = True) -> None:
        try:
            self._send_at_commands(
                ["AT+UNSTAT=0,101"],
                tag="get Wi-Fi IP",
                on_complete=lambda: self._retry_wifi_status_if_needed(retry_attempts),
            )
        except Exception as exc:
            if retry_attempts > 0:
                self.after(1000, lambda: self.get_wifi_status(retry_attempts=retry_attempts - 1, show_errors=False))
            elif show_errors:
                messagebox.showerror("Get Wi-Fi status failed", str(exc))

    def _retry_wifi_status_if_needed(self, retry_attempts: int) -> None:
        if self.current_ip_var.get() != "-" or retry_attempts <= 0 or not self._serial.is_open:
            return
        self.after(1000, lambda: self.get_wifi_status(retry_attempts=retry_attempts - 1, show_errors=False))

    def create_server_socket(self) -> None:
        try:
            server_id = int(self.server_id_var.get().strip(), 10)
            if not 0 <= server_id <= 6:
                raise ValueError("Server ID must be between 0 and 6")
            if f"server:{server_id}" in self._socket_entries:
                raise ValueError(f"Server ID {server_id} is already open")

            protocol = self.server_protocol_var.get().strip().lower()
            if protocol not in ("tcp", "udp"):
                raise ValueError("Protocol must be tcp or udp")

            address = self.server_ip_var.get().strip() or "0.0.0.0"
            if not is_valid_ipv4(address):
                raise ValueError("Enter a valid IPv4 address")

            port = int(self.server_port_var.get().strip(), 10)
            if not 1 <= port <= 65535:
                raise ValueError("Port must be between 1 and 65535")

            detail = f"{protocol}://{address}:{port}"
            duplicate = self._find_open_socket_by_detail("server", detail)
            if duplicate is not None:
                raise ValueError(f"Server socket is already open as ID {duplicate.handle}: {duplicate.detail}")

            command = f"AT+UDSC={server_id},{quote_at_string(detail)}"
            self._send_at_commands(
                [command],
                tag="create server",
                on_complete=lambda: self._upsert_socket(
                    SocketEntry(
                        key=f"server:{server_id}",
                        kind="server",
                        handle=str(server_id),
                        close_command=f"AT+UDSC={server_id},0",
                        detail=detail,
                    )
                ),
            )
        except Exception as exc:
            messagebox.showerror("Create server failed", str(exc))

    def open_udcp_channel(self) -> None:
        try:
            target = self.udcp_var.get().strip()
            if not target:
                raise ValueError("Enter a client socket target or AT+UDCP command")
            if target.upper().startswith("AT+UDCP"):
                command = target
                detail = target
            else:
                command = f"AT+UDCP={target}"
                detail = target

            duplicate = self._find_open_socket_by_detail("client", detail)
            if duplicate is not None:
                raise ValueError(f"Client socket is already open as handle {duplicate.handle}: {duplicate.detail}")

            self._pending_udcp_detail = detail
            self._send_at_commands([command], tag="open client", on_complete=lambda: setattr(self, "_pending_udcp_detail", ""))
        except Exception as exc:
            self._pending_udcp_detail = ""
            messagebox.showerror("Open client failed", str(exc))

    def refresh_socket_list(self) -> None:
        try:
            self._send_at_commands(["AT+UDLP?"], tag="refresh sockets")
        except Exception as exc:
            messagebox.showerror("Refresh sockets failed", str(exc))

    def close_selected_socket(self) -> None:
        selection = self.socket_tree.selection()
        if not selection:
            messagebox.showinfo("Close socket", "Select a socket first")
            return

        key = selection[0]
        entry = self._socket_entries.get(key)
        if entry is None:
            return

        try:
            self._send_at_commands(
                [entry.close_command],
                tag=f"close {entry.kind} {entry.handle}",
                on_complete=lambda: self._remove_socket(key),
            )
        except Exception as exc:
            messagebox.showerror("Close socket failed", str(exc))

    def _send_at_commands(self, commands: list[str], tag: str, on_complete: Callable[[], None] | None = None) -> None:
        if not self._serial.is_open:
            raise RuntimeError("Serial port is not open")
        if self._at_sequence_commands:
            raise RuntimeError("Another AT command sequence is waiting for response")

        cleaned_commands = [command.strip() for command in commands if command.strip()]
        if not cleaned_commands:
            raise ValueError("No AT commands to send")

        self._at_sequence_commands = cleaned_commands
        self._at_sequence_index = 0
        self._at_sequence_tag = tag
        self._at_sequence_is_edm = self.mode_var.get() == "EDM"
        self._at_sequence_id += 1
        self._at_sequence_on_complete = on_complete
        self._send_next_at_command()

    def _send_next_at_command(self) -> None:
        if self._at_sequence_index >= len(self._at_sequence_commands):
            tag = self._at_sequence_tag
            count = len(self._at_sequence_commands)
            on_complete = self._at_sequence_on_complete
            self._clear_at_sequence()
            self.status_var.set(f"{tag}: completed {count} AT command(s)")
            if on_complete:
                on_complete()
            return

        command = self._at_sequence_commands[self._at_sequence_index]
        payload = (command.rstrip("\r\n") + "\r").encode("utf-8")
        if self._at_sequence_is_edm:
            data = make_edm_at_request(payload)
            tx_tag = f"{self._at_sequence_tag} EDM AT_REQUEST {command}"
        else:
            data = payload
            tx_tag = f"{self._at_sequence_tag} raw AT {command}"

        self._at_response_buffer = ""
        if not self._write_serial(data, tag=tx_tag):
            self._clear_at_sequence()
            return

        step = self._at_sequence_index + 1
        total = len(self._at_sequence_commands)
        self.status_var.set(f"{self._at_sequence_tag}: waiting response {step}/{total}")
        sequence_id = self._at_sequence_id
        self._at_timeout_after_id = self.after(AT_RESPONSE_TIMEOUT_MS, self._handle_at_command_timeout, sequence_id)

    def _handle_at_command_timeout(self, sequence_id: int) -> None:
        if sequence_id != self._at_sequence_id or not self._at_sequence_commands:
            return

        command = self._at_sequence_commands[self._at_sequence_index]
        tag = self._at_sequence_tag
        self._clear_at_sequence()
        self.status_var.set(f"{tag}: timeout waiting response for {command}")
        self._append_rx_hex(f"!! {tag}: timeout {AT_RESPONSE_TIMEOUT_MS} ms, stopped at {command}\n")

    def _handle_at_response_text(self, text: str) -> None:
        if not self._at_sequence_commands:
            return

        self._at_response_buffer += text
        if AT_ERROR_RE.search(self._at_response_buffer):
            command = self._at_sequence_commands[self._at_sequence_index]
            tag = self._at_sequence_tag
            self._cancel_at_timeout()
            self._clear_at_sequence()
            self.status_var.set(f"{tag}: ERROR for {command}, stopped")
            self._append_rx_hex(f"!! {tag}: ERROR, stopped at {command}\n")
            return

        if AT_OK_RE.search(self._at_response_buffer):
            self._cancel_at_timeout()
            self._at_sequence_index += 1
            self._send_next_at_command()

    def _cancel_at_timeout(self) -> None:
        if self._at_timeout_after_id is not None:
            self.after_cancel(self._at_timeout_after_id)
            self._at_timeout_after_id = None

    def _clear_at_sequence(self) -> None:
        self._cancel_at_timeout()
        self._at_sequence_commands = []
        self._at_sequence_index = 0
        self._at_sequence_tag = ""
        self._at_sequence_is_edm = False
        self._at_response_buffer = ""
        self._at_sequence_on_complete = None

    def send_payload(self) -> None:
        try:
            payload = self.tx_text.get("1.0", "end-1c").encode("utf-8")
            payload = append_line_ending(payload, self.line_ending_var.get())

            if self.mode_var.get() == "EDM":
                frame_type = self._selected_edm_frame_type()
                if frame_type in EDM_DATA_FRAME_TYPES:
                    channel = parse_hex(self.channel_var.get())
                    if len(channel) != 1:
                        raise ValueError("Channel must be exactly one hex byte")
                    data = make_edm_data_frame(frame_type, channel[0], payload)
                else:
                    data = make_edm_frame(frame_type, payload)
            else:
                data = payload

            self._write_serial(data, tag="tx")
        except Exception as exc:
            messagebox.showerror("Send failed", str(exc))

    def _selected_edm_frame_type(self) -> int:
        try:
            return EDM_FRAME_TYPE_BY_LABEL[self.edm_send_var.get()]
        except KeyError as exc:
            raise ValueError("Select a valid EDM frame type") from exc

    def _update_edm_controls(self, *_args: object) -> None:
        if not hasattr(self, "edm_type_combo"):
            return

        is_edm = self.mode_var.get() == "EDM"
        frame_type = self._selected_edm_frame_type()
        needs_channel = is_edm and frame_type in EDM_DATA_FRAME_TYPES

        self.edm_type_combo.configure(state="readonly" if is_edm else "disabled")
        self.channel_entry.configure(state="normal" if needs_channel else "disabled")
        self.edm_type_label.configure(state="normal" if is_edm else "disabled")
        self.channel_label.configure(state="normal" if needs_channel else "disabled")

    def _write_serial(self, data: bytes, tag: str) -> bool:
        try:
            self._serial.write(data)
            self._append_rx_hex(f">> {tag}: {bytes_to_hex(data)}\n")
            return True
        except Exception as exc:
            messagebox.showerror("Serial write failed", str(exc))
            return False

    def _sync_tx_from_text(self, _event: tk.Event | None = None) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            data = self.tx_text.get("1.0", "end-1c").encode("utf-8")
            self.tx_hex.delete("1.0", "end")
            self.tx_hex.insert("1.0", bytes_to_hex(data))
        finally:
            self._syncing = False

    def _sync_tx_from_hex(self, _event: tk.Event | None = None) -> None:
        if self._syncing:
            return
        try:
            data = parse_hex(self.tx_hex.get("1.0", "end-1c"))
        except ValueError:
            return

        self._syncing = True
        try:
            self.tx_text.delete("1.0", "end")
            self.tx_text.insert("1.0", bytes_to_text(data))
        finally:
            self._syncing = False

    def _poll_inbox(self) -> None:
        while True:
            try:
                kind, payload = self._inbox.get_nowait()
            except queue.Empty:
                break

            if kind == "rx":
                data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
                text = bytes_to_text(data)
                self._append_rx_text(text)
                self._append_rx_hex(f"<< rx: {bytes_to_hex(data)}\n")
                self._process_at_metadata(text)
                if not self._at_sequence_is_edm:
                    self._handle_at_response_text(text)
                for frame in self._edm_parser.feed(data):
                    self._handle_edm_frame(frame)
                    self._append_rx_hex(self._format_edm_frame(frame))
                    if self._at_sequence_is_edm and frame.frame_type == EDM_AT_RESPONSE:
                        self._handle_at_response_text(bytes_to_text(frame.payload))
            elif kind == "error":
                self.status_var.set(f"Serial error: {payload}")

        self.after(50, self._poll_inbox)

    def _handle_edm_frame(self, frame: EdmFrame) -> None:
        if frame.frame_type in (EDM_AT_RESPONSE, EDM_AT_EVENT):
            self._process_at_metadata(bytes_to_text(frame.payload))

        if frame.frame_type == EDM_CONNECT_EVENT and frame.payload:
            channel = frame.payload[0]
            self.channel_var.set(f"{channel:02X}")
            self.edm_send_var.set("command DATA_COMMAND (0x36)")
            self.status_var.set(f"EDM channel opened: 0x{channel:02X}")
        elif frame.frame_type == EDM_DISCONNECT_EVENT and frame.payload:
            channel = frame.payload[0]
            self.status_var.set(f"EDM channel closed: 0x{channel:02X}")

    def _process_at_metadata(self, text: str) -> None:
        self._update_wifi_ip_from_rx(text)
        self._update_sockets_from_rx(text)

    def _update_wifi_ip_from_rx(self, text: str) -> None:
        address = parse_unstat_ip_101(text)
        if address:
            self._set_current_ip(address)
            return

        current = self.server_ip_var.get().strip()
        if current and current != "0.0.0.0":
            return

        upper = text.upper()
        if "+UWS" not in upper and "IP" not in upper:
            return

        address = first_valid_ipv4(text)
        if address:
            self._set_current_ip(address)

    def _set_current_ip(self, address: str) -> None:
        self.current_ip_var.set(address)
        current_server_ip = self.server_ip_var.get().strip()
        if current_server_ip in ("", "0.0.0.0"):
            self.server_ip_var.set(address)
            self.status_var.set(f"Server IP auto-filled: {address}")
        else:
            self.status_var.set(f"Wi-Fi IP: {address}")

    def _update_sockets_from_rx(self, text: str) -> None:
        for match in UDCP_RE.finditer(text):
            handle = match.group(1)
            self._upsert_socket(
                SocketEntry(
                    key=f"client:{handle}",
                    kind="client",
                    handle=handle,
                    close_command=f"AT+UDCPC={handle}",
                    detail=self._pending_udcp_detail or "client socket",
                )
            )

        for match in UDLP_RE.finditer(text):
            handle = match.group(1)
            detail = (match.group(2) or "client socket").strip()
            self._upsert_socket(
                SocketEntry(
                    key=f"client:{handle}",
                    kind="client",
                    handle=handle,
                    close_command=f"AT+UDCPC={handle}",
                    detail=detail,
                )
            )

    def _upsert_socket(self, entry: SocketEntry) -> None:
        self._socket_entries[entry.key] = entry
        self._refresh_socket_tree()

    def _remove_socket(self, key: str) -> None:
        self._socket_entries.pop(key, None)
        self._refresh_socket_tree()

    def _find_open_socket_by_detail(self, kind: str, detail: str) -> SocketEntry | None:
        normalized = normalize_socket_detail(detail)
        for entry in self._socket_entries.values():
            if entry.kind == kind and entry.status == "open" and normalize_socket_detail(entry.detail) == normalized:
                return entry
        return None

    def _refresh_socket_tree(self) -> None:
        existing_selection = self.socket_tree.selection()
        selected_key = existing_selection[0] if existing_selection else ""
        for item in self.socket_tree.get_children():
            self.socket_tree.delete(item)

        for key, entry in sorted(self._socket_entries.items(), key=lambda item: (item[1].kind, item[1].handle)):
            self.socket_tree.insert(
                "",
                "end",
                iid=key,
                values=(entry.kind, entry.handle, entry.detail, entry.status),
            )

        if selected_key in self._socket_entries:
            self.socket_tree.selection_set(selected_key)

    def _format_edm_frame(self, frame: EdmFrame) -> str:
        names = {
            EDM_CONNECT_EVENT: "Connect Event",
            EDM_DISCONNECT_EVENT: "Disconnect Event",
            EDM_DATA_EVENT: "Data Event",
            EDM_AT_REQUEST: "AT Request",
            EDM_AT_RESPONSE: "AT Response",
            EDM_AT_EVENT: "AT Event/URC",
            EDM_DATA_COMMAND: "Data Command",
            EDM_START_EVENT: "Start Event",
        }
        name = names.get(frame.frame_type, f"0x{frame.frame_type:02X}")
        if frame.frame_type in (EDM_DATA_COMMAND, EDM_DATA_EVENT) and frame.payload:
            channel = frame.payload[0]
            body = frame.payload[1:]
            return (
                f"   EDM {name}: channel={channel:02X} payload_hex={bytes_to_hex(body)} "
                f'payload_text="{bytes_to_text(body)}"\n'
            )
        if frame.frame_type in (EDM_CONNECT_EVENT, EDM_DISCONNECT_EVENT):
            return self._format_edm_connection_event(name, frame.payload)
        return (
            f"   EDM {name}: payload_hex={bytes_to_hex(frame.payload)} "
            f'payload_text="{bytes_to_text(frame.payload)}"\n'
        )

    def _format_edm_connection_event(self, name: str, payload: bytes) -> str:
        connection_types = {
            EDM_CONNECTION_BT: "BT",
            EDM_CONNECTION_IPV4: "IPv4",
            EDM_CONNECTION_IPV6: "IPv6",
        }
        if len(payload) >= 2:
            channel = payload[0]
            connection_type = payload[1]
            connection_name = connection_types.get(connection_type, f"0x{connection_type:02X}")
            rest = payload[2:]
            return (
                f"   EDM {name}: channel={channel:02X} connection={connection_name} "
                f"payload_hex={bytes_to_hex(rest)} payload_text=\"{bytes_to_text(rest)}\"\n"
            )
        return (
            f"   EDM {name}: payload_hex={bytes_to_hex(payload)} "
            f'payload_text="{bytes_to_text(payload)}"\n'
        )

    def _append_rx_text(self, text: str) -> None:
        self.rx_text.configure(state="normal")
        self.rx_text.insert("end", text)
        self.rx_text.see("end")
        self.rx_text.configure(state="disabled")

    def _append_rx_hex(self, text: str) -> None:
        self.rx_hex.configure(state="normal")
        self.rx_hex.insert("end", text)
        self.rx_hex.see("end")
        self.rx_hex.configure(state="disabled")

    def clear_tx(self) -> None:
        self.tx_text.delete("1.0", "end")
        self.tx_hex.delete("1.0", "end")

    def clear_rx(self) -> None:
        self.rx_text.configure(state="normal")
        self.rx_text.delete("1.0", "end")
        self.rx_text.configure(state="disabled")
        self.rx_hex.configure(state="normal")
        self.rx_hex.delete("1.0", "end")
        self.rx_hex.configure(state="disabled")

    def _on_close(self) -> None:
        self.close_port()
        self.destroy()


def main() -> None:
    app = UbloxSerialTool()
    app.mainloop()


if __name__ == "__main__":
    main()
