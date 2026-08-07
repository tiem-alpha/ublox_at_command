#include "uart_bridge.h"

#include "uart0_hmi.h"
#include "uart_async_port.h"
#include "ublox_transport.h"

#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/ring_buffer.h>

#define UART0_DEVICE_NODE DT_NODELABEL(uart0)
#define UART1_DEVICE_NODE DT_NODELABEL(uart1)

#define RX_RING_SIZE 512
#define RX_BUF_SIZE 64

static const struct device *const uart0_dev = DEVICE_DT_GET(UART0_DEVICE_NODE);
static const struct device *const uart1_dev = DEVICE_DT_GET(UART1_DEVICE_NODE);

RING_BUF_DECLARE(uart0_rx_ring, RX_RING_SIZE);
RING_BUF_DECLARE(uart1_rx_ring, RX_RING_SIZE);

static struct k_spinlock uart0_rx_lock;
static struct k_spinlock uart1_rx_lock;
static K_SEM_DEFINE(uart0_rx_sem, 0, 1);
static K_SEM_DEFINE(uart1_rx_sem, 0, 1);

static uint8_t uart0_rx_buffer[RX_BUF_SIZE];
static uint8_t uart1_rx_buffer[RX_BUF_SIZE];

static struct uart_async_port uart0_port = {
	.dev = uart0_dev,
	.rx_ring = &uart0_rx_ring,
	.rx_lock = &uart0_rx_lock,
	.rx_sem = &uart0_rx_sem,
	.rx_buf = uart0_rx_buffer,
	.rx_buf_size = sizeof(uart0_rx_buffer),
};

static struct uart_async_port uart1_port = {
	.dev = uart1_dev,
	.rx_ring = &uart1_rx_ring,
	.rx_lock = &uart1_rx_lock,
	.rx_sem = &uart1_rx_sem,
	.rx_buf = uart1_rx_buffer,
	.rx_buf_size = sizeof(uart1_rx_buffer),
};

static const struct ublox_transport_config ublox_config = {
	.port = &uart1_port,
	.bridge_mode_enabled = uart0_hmi_bridge_mode_enabled,
	.hmi_write_bytes = uart0_hmi_write_bytes,
	.hmi_write_response_line = uart0_hmi_write_response_line,
};

static const struct uart0_hmi_config hmi_config = {
	.port = &uart0_port,
	.send_at_command = ublox_transport_send_at_command,
	.send_bytes_to_ublox = ublox_transport_send_bytes,
};

int uart_bridge_start(void)
{
	int err;

	err = uart_async_port_start(&uart0_port);
	if (err != 0) {
		return err;
	}

	err = uart_async_port_start(&uart1_port);
	if (err != 0) {
		return err;
	}

	err = uart0_hmi_start(&hmi_config);
	if (err != 0) {
		return err;
	}

	err = ublox_transport_start(&ublox_config);
	if (err != 0) {
		return err;
	}

	printk("UART0 HMI and UART1 u-blox tasks ready\n");
	return 0;
}
