#ifndef UBLOX_TRANSPORT_H
#define UBLOX_TRANSPORT_H

#include "uart_async_port.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

struct ublox_transport_config {
	struct uart_async_port *port;
	bool (*bridge_mode_enabled)(void);
	void (*hmi_write_bytes)(const uint8_t *buf, size_t len);
	void (*hmi_write_response_line)(const char *line);
};

int ublox_transport_start(const struct ublox_transport_config *config);
int ublox_transport_send_at_command(const char *command);
int ublox_transport_send_bytes(const uint8_t *buf, size_t len);

#endif /* UBLOX_TRANSPORT_H */
