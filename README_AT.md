# u-connectXpress — Wi‑Fi, TCP/UDP, MQTT qua Extended Data Mode (EDM)

Tài liệu này mô tả luồng giao tiếp thực tế của project với module u-blox u-connectXpress, đặc biệt là NINA-W132.

> **Quyết định thiết kế của project**
>
> - Không sử dụng `AT+UDATW` / `AT+UDATR`.
> - kiểm tra tra modem ready bằng AT 
> - tắt echo mode 
> - Không gửi chuỗi raw `AT...` trực tiếp trên UART sau khi đã vào EDM.
> - Module được giữ trong **Extended Data Mode**.
> - AT command được đóng gói bằng **EDM AT Request `0x44`**.
> - Payload socket/MQTT được gửi bằng **EDM Data Command `0x36`**.
> - Response, URC và dữ liệu nhận về được phân loại theo loại frame EDM.

---

## 1. Luồng tổng thể

```text
Power on
   |
kiểm tra modem ready bằng URCE hoặc 'AT' command
    |
vào EDM mode
    |
Command mode
   |
   | Cấu hình/kết nối Wi‑Fi
   v
Extended Data Mode
   |
   +--> EDM AT Request 0x44  --> AT+UDCP, AT+UDCPC, status...
   |
   +--> EDM Data Command 0x36 --> payload TCP/UDP/MQTT
   |
   +<-- EDM AT Response 0x45
   |
   +<-- EDM AT Event 0x41     --> URC
   |
   +<-- EDM Data Event        --> payload nhận từ remote peer
```

Trong project dùng ubxlib, AT client TX được ubxlib intercept và đóng gói thành EDM frame. Vì vậy application vẫn có thể gọi AT client API, nhưng byte thực tế trên UART không còn là chuỗi `AT...` thuần.

---

## 2. Vào Extended Data Mode

Từ command mode, gửi:

```text
ATO2
```

Response thành công:

```text
OK
```

Sau đó UART chuyển sang giao thức EDM. Không tiếp tục gửi trực tiếp:

```text
AT+...
```

dưới dạng raw text trên UART.

Mọi AT command tiếp theo phải được bọc trong EDM AT Request frame.

---

## 3. Cấu trúc frame EDM cơ bản

### 3.1 Khuôn dạng chung

```text
AA <len_hi> <len_lo> <payload bytes> 55
```

| Trường | Ý nghĩa |
|---|---|
| `AA` | EDM frame head |
| `len_hi len_lo` | Độ dài phần EDM payload, big-endian |
| `payload` | Type, channel và data tùy loại frame |
| `55` | EDM frame tail |

Parser UART phải xử lý dữ liệu theo byte và không phụ thuộc ranh giới của một lần UART receive/DMA callback.

---

## 4. Gửi AT command trong EDM

### 4.1 EDM AT Request

Frame UART:

```text
AA <len_hi> <len_lo> 00 44 <AT command bytes> 55
```

Ý nghĩa:

```text
AA  = EDM head
len = độ dài AT command + 2
00  = reserved
44  = EDM AT Request
55  = EDM tail
```

AT command bytes là nội dung command dạng ASCII, ví dụ:

```text
AT+UDCP=tcp://192.168.1.7:8888
```

Không thêm một frame raw UART riêng chứa command này.

### 4.2 Ví dụ đóng gói

Giả sử command:

```text
AT
```

Command dài 2 byte, do đó:

```text
len = 2 + 2 = 4
```

Frame:

```text
AA 00 04 00 44 41 54 55
```

Trong đó:

```text
41 54 = "AT"
```

### 4.3 Response và URC

Module trả về các loại frame:

```text
45 = EDM AT Response
41 = EDM AT Event / URC
```

Host phải dispatch theo type:

```text
0x45 -> chuyển dữ liệu response cho AT client
0x41 -> chuyển nội dung tới URC/event handler
```

AT response và URC có thể đến xen kẽ, vì vậy không được giả định mọi text nhận được đều thuộc command đang chờ.

---

## 5. Wi‑Fi Station

Các AT command dưới đây được gửi trực tiếp khi còn command mode, hoặc được bọc bằng EDM AT Request `0x44` sau khi đã vào EDM.

### 5.1 Cấu hình SSID

```text
AT+UWSC=0,2,"MySSID"
```

### 5.2 Cấu hình authentication

```text
AT+UWSC=0,5,2
```

Giá trị thường dùng:

```text
1 = Open
2 = WPA/WPA2/WPA3
6 = WPA2/WPA3
7 = WPA3 only
```

### 5.3 Cấu hình password

```text
AT+UWSC=0,8,"MyPassword123"
```

### 5.4 DHCP

```text
AT+UWSC=0,100,2
```

### 5.5 Activate Wi‑Fi profile

```text
AT+UWSCA=0,3
```

### 5.6 Trạng thái Wi‑Fi

```text
AT+UWSSTAT
```

Response có dạng:

```text
+UWSSTAT:<status_id>,<status_value>
```

Các ID thường dùng:

| ID | Ý nghĩa |
|---:|---|
| `0` | SSID |
| `1` | BSSID |
| `2` | Channel |
| `3` | `0` disabled, `1` disconnected, `2` connected |
| `6` | RSSI |

### 5.7 Trạng thái IP

```text
AT+UNSTAT
```

Ví dụ:

```text
+UNSTAT:0,101,192.168.1.10
+UNSTAT:0,102,255.255.255.0
+UNSTAT:0,103,192.168.1.1
+UNSTAT:0,104,192.168.1.1
```

Các ID quan trọng:

| ID | Ý nghĩa |
|---:|---|
| `101` | IPv4 address |
| `102` | Subnet mask |
| `103` | Default gateway |
| `104` | Primary DNS |
| `105` | Secondary DNS |

### 5.8 Wi‑Fi và network URC

```text
+UUWLE   = Wi‑Fi link connected
+UUWLD   = Wi‑Fi link disconnected
+UUNU    = Network up
+UUND    = Network down
+UUNERR  = Network error
```

Khi đang EDM, các URC này được chuyển trong **EDM AT Event frame `0x41`**.

---

## 6. Mở TCP/UDP/MQTT channel

### 6.1 TCP client

```text
AT+UDCP=tcp://192.168.1.7:8888
```

### 6.2 UDP peer

```text
AT+UDCP=udp://192.168.1.7:9999
```

### 6.3 MQTT peer

```text
AT+UDCP=mqtt://<broker>:<port>/?client=<client_id>&pt=<publish_topic>&st=<subscribe_topic>
```

Ví dụ:

```text
AT+UDCP=mqtt://192.168.1.7:1883/?client=nina_w132&pt=device/data&st=device/cmd/#
```

Trong EDM, các command trên được gửi bằng frame:

```text
AA <len_hi> <len_lo> 00 44 <AT+UDCP command bytes> 55
```

### 6.4 Response khi tạo peer

AT response:

```text
+UDCP:<peer_handle>
OK
```

Khi kết nối hoàn tất, module phát URC:

```text
+UUDPC:<peer_handle>,<type>,<protocol>,
       <local_address>,<local_port>,
       <remote_address>,<remote_port>
```

Ví dụ:

```text
+UDCP:1
OK

+UUDPC:1,2,0,192.168.1.10,60594,192.168.1.7,8888
```

Các protocol thường gặp:

```text
0 = TCP
1 = UDP
6 = MQTT
```

> `peer_handle` của `+UDCP` và EDM data `channel` phải được quản lý rõ ràng trong software. Không nên mặc định mọi kết nối luôn có channel `1` hoặc `2`.

---

## 7. Gửi payload data

Payload thực không được gửi bằng AT command.

Không sử dụng:

```text
AT+UDATW
AT+UDATR
AT+USOWR
AT+UMQTTPUB
```

Trong project hiện tại, payload được gửi bằng **EDM Data Command `0x36`**.

### 7.1 Frame UART

```text
AA <len_hi> <len_lo> 00 36 <channel> <payload bytes> 55
```

Ý nghĩa:

```text
AA       = EDM head
len      = payload_len + 3
00       = reserved
36       = EDM Data Command
channel  = EDM channel tương ứng với peer đã mở
payload  = dữ liệu cần gửi
55       = EDM tail
```

### 7.2 Ví dụ gửi `"abc"` trên channel `0x02`

```text
AA 00 06 00 36 02 61 62 63 55
```

Trong đó:

```text
61 62 63 = "abc"
payload_len = 3
len = payload_len + 3 = 6
```

### 7.3 Pseudocode đóng gói

```c
bool edm_send_data(uint8_t channel,
                   const uint8_t *payload,
                   size_t payload_len)
{
    uint16_t edm_len;

    if ((payload == NULL && payload_len != 0U) ||
        payload_len > 65532U) {
        return false;
    }

    edm_len = (uint16_t)(payload_len + 3U);

    uart_write_byte(0xAA);
    uart_write_byte((uint8_t)(edm_len >> 8));
    uart_write_byte((uint8_t)(edm_len & 0xFF));
    uart_write_byte(0x00);
    uart_write_byte(0x36);
    uart_write_byte(channel);
    uart_write(payload, payload_len);
    uart_write_byte(0x55);

    return true;
}
```

Trong code production, toàn bộ frame nên được serialize qua một TX queue hoặc một cơ chế lock duy nhất để tránh hai task ghép byte của hai frame vào nhau.

---

## 8. Nhận payload data

Payload từ TCP, UDP hoặc MQTT được module gửi về dưới dạng EDM data event/frame tương ứng.

Parser cần:

1. Tìm `0xAA`.
2. Đọc `len_hi`, `len_lo`.
3. Chờ đủ chính xác `len` byte payload.
4. Kiểm tra tail `0x55`.
5. Đọc frame type.
6. Dispatch theo type và channel.
7. Chuyển payload tới socket/MQTT application handler.

Không parse payload nhận được như dòng AT command và không chờ `CRLF`, vì dữ liệu có thể là binary.

---

## 9. Đóng channel

Dùng:

```text
AT+UDCPC=<peer_handle>
```

Ví dụ:

```text
AT+UDCPC=1
```

Khi đang EDM, command được bọc bằng EDM AT Request:

```text
AA <len_hi> <len_lo> 00 44 AT+UDCPC=<handle> 55
```

Khi peer bị đóng hoặc mất kết nối, module phát:

```text
+UUDPD:<peer_handle>
```

URC này đến trong EDM AT Event `0x41`.

---

## 10. Giữ module ở EDM

Luồng bình thường của project là giữ module ở EDM để AT control và payload data chạy song song trên cùng UART:

```text
EDM AT Request  0x44 -> gửi AT command
EDM Data Command 0x36 -> gửi payload
EDM AT Response 0x45 <- nhận AT response
EDM AT Event    0x41 <- nhận URC
EDM Data Event       <- nhận payload
```

Project hiện tại không có API riêng để thoát EDM. Vì vậy application không nên thiết kế flow phụ thuộc vào việc liên tục chuyển:

```text
command mode <-> transparent data mode
```

Không sử dụng `ATO1` và escape sequence `+++` trong luồng truyền data bình thường của project.

---

## 11. State machine đề xuất

```text
MODULE_RESET
    |
    v
WAIT_STARTUP
    |
    v
CONFIGURE_WIFI
    |
    v
WAIT_WIFI_LINK
    |
    v
WAIT_NETWORK_UP
    |
    v
ENTER_EDM
    |
    v
EDM_READY
    |
    +--> OPEN_PEER
    |        |
    |        v
    |   WAIT_PEER_CONNECTED
    |        |
    |        v
    |   CHANNEL_ACTIVE
    |        |
    |        +--> SEND_EDM_DATA
    |        +--> RECEIVE_EDM_DATA
    |        +--> HANDLE_AT_EVENT
    |
    +--> RECONNECT_WIFI
    +--> REOPEN_PEER
```

Các sự kiện quan trọng:

```text
+UUWLD  -> Wi‑Fi disconnected
+UUND   -> Network down
+UUDPD  -> Peer disconnected
+UUDPC  -> Peer connected
```

Khi mất Wi‑Fi hoặc network, invalidate toàn bộ channel/peer mapping cũ và chỉ mở lại peer sau khi nhận network up.

---

## 12. Checklist implementation

- [ ] Chỉ gửi `ATO2` một lần khi chuyển sang EDM.
- [ ] Không gửi raw `AT...` trực tiếp sau khi vào EDM.
- [ ] AT client TX được đóng gói thành type `0x44`.
- [ ] AT response type `0x45` được trả đúng về AT client.
- [ ] AT event type `0x41` được đưa tới URC parser.
- [ ] Payload TX dùng type `0x36`.
- [ ] Payload RX được xử lý theo channel, không parse như text.
- [ ] Độ dài EDM là big-endian.
- [ ] TX frame được serialize, không interleave giữa các task.
- [ ] Parser hỗ trợ frame bị chia qua nhiều DMA/UART receive callback.
- [ ] Parser hỗ trợ nhiều frame nằm trong cùng một RX buffer.
- [ ] Không hard-code channel nếu chưa lấy được mapping thực tế.
- [ ] Khi nhận `+UUDPD`, giải phóng channel và peer state.
- [ ] Khi nhận `+UUWLD` hoặc `+UUND`, invalidate toàn bộ peer.
- [ ] Không sử dụng `AT+UDATW`, `AT+UDATR`, `ATO1` hoặc `+++` trong flow bình thường.

---

## 13. Kết luận

`AT+UDCP` chỉ tạo peer và mở connection/channel.

Payload thực tế được gửi bằng:

```text
EDM Data Command 0x36
```

AT command khi đang EDM được gửi bằng:

```text
EDM AT Request 0x44
```

Module trả AT response và URC bằng:

```text
EDM AT Response 0x45
EDM AT Event    0x41
```

Do đó UART driver phải được thiết kế như một **EDM multiplexer**, không phải một parser dòng AT thuần và cũng không phải một transparent raw socket stream.
