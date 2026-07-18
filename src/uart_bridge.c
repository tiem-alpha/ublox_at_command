#include "uart_bridge.h"

#include <errno.h>
#include <string.h>
#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/ring_buffer.h>

LOG_MODULE_REGISTER(uart_bridge, LOG_LEVEL_INF);

#define RING_BUF_SIZE 512
#define RX_BUF_SIZE   64
#define RECEIVE_TIMEOUT_US 1000
#define BRIDGE_STACK_SIZE 1024
#define BRIDGE_THREAD_PRIORITY 5

#define UART0_DEVICE_NODE DT_NODELABEL(uart0)
#define UART1_DEVICE_NODE DT_NODELABEL(uart1)

struct uart_bridge_path {
	const struct device *rx_dev;
	const struct device *tx_dev;
	struct ring_buf *ring;
	struct k_spinlock *lock;
	uint8_t *rx_buf;
	size_t rx_buf_size;
	const char *tx_log_prefix;
};

static const struct device *const uart0_dev = DEVICE_DT_GET(UART0_DEVICE_NODE);
static const struct device *const uart1_dev = DEVICE_DT_GET(UART1_DEVICE_NODE);

RING_BUF_DECLARE(uart0_to_uart1_ring, RING_BUF_SIZE);
RING_BUF_DECLARE(uart1_to_uart0_ring, RING_BUF_SIZE);

static struct k_spinlock uart0_to_uart1_lock;
static struct k_spinlock uart1_to_uart0_lock;
static K_SEM_DEFINE(bridge_data_sem, 0, 1);

static uint8_t uart0_rx_buffer[RX_BUF_SIZE];
static uint8_t uart1_rx_buffer[RX_BUF_SIZE];

static K_THREAD_STACK_DEFINE(uart_bridge_stack, BRIDGE_STACK_SIZE);
static struct k_thread uart_bridge_thread_data;

static struct uart_bridge_path uart0_to_uart1 = {
	.rx_dev = uart0_dev,
	.tx_dev = uart1_dev,
	.ring = &uart0_to_uart1_ring,
	.lock = &uart0_to_uart1_lock,
	.rx_buf = uart0_rx_buffer,
	.rx_buf_size = sizeof(uart0_rx_buffer),
	.tx_log_prefix = "UART0 -> UART1 TX",
};

static struct uart_bridge_path uart1_to_uart0 = {
	.rx_dev = uart1_dev,
	.tx_dev = uart0_dev,
	.ring = &uart1_to_uart0_ring,
	.lock = &uart1_to_uart0_lock,
	.rx_buf = uart1_rx_buffer,
	.rx_buf_size = sizeof(uart1_rx_buffer),
	.tx_log_prefix = "UART1 -> UART0 TX",
};

static void forward_to_uart(const struct device *uart_dev, const uint8_t *buf, size_t len)
{
	for (size_t i = 0; i < len; i++) {
		uart_poll_out(uart_dev, buf[i]);
	}
}

static void drain_bridge_path(struct uart_bridge_path *path)
{
	uint8_t tx_data[RX_BUF_SIZE];
	char log_data[RX_BUF_SIZE + 1];
	uint32_t len;
	k_spinlock_key_t key;

	do {
		key = k_spin_lock(path->lock);
		len = ring_buf_get(path->ring, tx_data, sizeof(tx_data));
		k_spin_unlock(path->lock, key);

		if (len != 0) {
			forward_to_uart(path->tx_dev, tx_data, len);

			memcpy(log_data, tx_data, len);
			log_data[len] = '\0';
			LOG_INF("%s: %s", path->tx_log_prefix, log_data);
		}
	} while (len != 0);
}

static void uart_bridge_thread(void *p1, void *p2, void *p3)
{
	(void)p1;
	(void)p2;
	(void)p3;

	while (1) {
		k_sem_take(&bridge_data_sem, K_FOREVER);
		drain_bridge_path(&uart0_to_uart1);
		drain_bridge_path(&uart1_to_uart0);
	}
}

static void uart_cb(const struct device *dev, struct uart_event *evt, void *user_data)
{
	struct uart_bridge_path *path = user_data;
	k_spinlock_key_t key;

	(void)dev;

	switch (evt->type) {
	case UART_RX_RDY:
		key = k_spin_lock(path->lock);
		(void)ring_buf_put(path->ring, evt->data.rx.buf + evt->data.rx.offset,
				   evt->data.rx.len);
		k_spin_unlock(path->lock, key);
		k_sem_give(&bridge_data_sem);
		break;

	case UART_RX_DISABLED:
		(void)uart_rx_enable(path->rx_dev, path->rx_buf, path->rx_buf_size,
				     RECEIVE_TIMEOUT_US);
		break;

	default:
		break;
	}
}

static int setup_uart(const struct device *uart_dev, struct uart_bridge_path *path)
{
	int err;

	if (!device_is_ready(uart_dev)) {
		printk("%s is not ready\n", uart_dev->name);
		return -ENODEV;
	}

	err = uart_callback_set(uart_dev, uart_cb, path);
	if (err != 0) {
		printk("Failed to set callback for %s: %d\n", uart_dev->name, err);
		return err;
	}

	err = uart_rx_enable(uart_dev, path->rx_buf, path->rx_buf_size, RECEIVE_TIMEOUT_US);
	if (err != 0) {
		printk("Failed to enable RX for %s: %d\n", uart_dev->name, err);
	}

	return err;
}

int uart_bridge_start(void)
{
	int err;
	k_tid_t tid;

	err = setup_uart(uart0_dev, &uart0_to_uart1);
	if (err != 0) {
		return err;
	}

	err = setup_uart(uart1_dev, &uart1_to_uart0);
	if (err != 0) {
		return err;
	}

	tid = k_thread_create(&uart_bridge_thread_data, uart_bridge_stack,
			      K_THREAD_STACK_SIZEOF(uart_bridge_stack), uart_bridge_thread,
			      NULL, NULL, NULL, BRIDGE_THREAD_PRIORITY, 0, K_NO_WAIT);
	k_thread_name_set(tid, "uart_bridge");

	printk("UART bridge ready: UART0 <-> UART1\n");
	return 0;
}
