#ifndef UART_ASYNC_PORT_H
#define UART_ASYNC_PORT_H

#include <stddef.h>
#include <stdint.h>
#include <zephyr/device.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/ring_buffer.h>

struct uart_async_port {
	const struct device *dev;
	struct ring_buf *rx_ring;
	struct k_spinlock *rx_lock;
	struct k_sem *rx_sem;
	uint8_t *rx_buf;
	size_t rx_buf_size;
};

int uart_async_port_start(struct uart_async_port *port);
uint32_t uart_async_port_read(struct uart_async_port *port, uint8_t *buf, size_t len);
void uart_async_port_write(const struct uart_async_port *port, const uint8_t *buf, size_t len);

#endif /* UART_ASYNC_PORT_H */
