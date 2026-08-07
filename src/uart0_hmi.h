#ifndef UART0_HMI_H
#define UART0_HMI_H

#include "uart_async_port.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

struct uart0_hmi_config {
	struct uart_async_port *port;
	int (*send_at_command)(const char *command);
	int (*send_bytes_to_ublox)(const uint8_t *buf, size_t len);
};

int uart0_hmi_start(const struct uart0_hmi_config *config);
bool uart0_hmi_bridge_mode_enabled(void);
void uart0_hmi_write_bytes(const uint8_t *buf, size_t len);
void uart0_hmi_write_str(const char *str);
void uart0_hmi_write_response_line(const char *line);

#endif /* UART0_HMI_H */
