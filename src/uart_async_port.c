#include "uart_async_port.h"

#include <errno.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/sys/printk.h>

#define RECEIVE_TIMEOUT_US 1000

static void uart_cb(const struct device *dev, struct uart_event *evt, void *user_data)
{
	struct uart_async_port *port = user_data;
	k_spinlock_key_t key;

	(void)dev;

	switch (evt->type) {
	case UART_RX_RDY:
		key = k_spin_lock(port->rx_lock);
		(void)ring_buf_put(port->rx_ring, evt->data.rx.buf + evt->data.rx.offset,
				   evt->data.rx.len);
		k_spin_unlock(port->rx_lock, key);
		k_sem_give(port->rx_sem);
		break;

	case UART_RX_DISABLED:
	case UART_RX_STOPPED:
		(void)uart_rx_enable(port->dev, port->rx_buf, port->rx_buf_size,
				     RECEIVE_TIMEOUT_US);
		break;

	default:
		break;
	}
}

int uart_async_port_start(struct uart_async_port *port)
{
	int err;

	if (!device_is_ready(port->dev)) {
		printk("%s is not ready\n", port->dev->name);
		return -ENODEV;
	}

	err = uart_callback_set(port->dev, uart_cb, port);
	if (err != 0) {
		printk("Failed to set callback for %s: %d\n", port->dev->name, err);
		return err;
	}

	err = uart_rx_enable(port->dev, port->rx_buf, port->rx_buf_size, RECEIVE_TIMEOUT_US);
	if (err != 0) {
		printk("Failed to enable RX for %s: %d\n", port->dev->name, err);
	}

	return err;
}

uint32_t uart_async_port_read(struct uart_async_port *port, uint8_t *buf, size_t len)
{
	uint32_t read_len;
	k_spinlock_key_t key;

	key = k_spin_lock(port->rx_lock);
	read_len = ring_buf_get(port->rx_ring, buf, len);
	k_spin_unlock(port->rx_lock, key);

	return read_len;
}

void uart_async_port_write(const struct uart_async_port *port, const uint8_t *buf, size_t len)
{
	for (size_t i = 0; i < len; i++) {
		uart_poll_out(port->dev, buf[i]);
	}
}
