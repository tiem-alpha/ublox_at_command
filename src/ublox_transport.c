#include "ublox_transport.h"

#include "uart_app_limits.h"

#include <errno.h>
#include <stdint.h>
#include <string.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(ublox_transport, LOG_LEVEL_INF);

#define UBLOX_TX_MSG_SIZE (UART_APP_MAX_AT_COMMAND_LEN + 2U)
#define UBLOX_TX_QUEUE_LEN 8
#define UBLOX_RESPONSE_LINE_SIZE 256
#define UBLOX_TASK_STACK_SIZE 1536
#define UBLOX_TASK_PRIORITY 5

struct ublox_tx_msg {
	uint16_t len;
	uint8_t data[UBLOX_TX_MSG_SIZE];
};

struct response_parser {
	char line[UBLOX_RESPONSE_LINE_SIZE];
	size_t len;
};

static const struct ublox_transport_config *transport_config;
K_MSGQ_DEFINE(ublox_tx_msgq, sizeof(struct ublox_tx_msg), UBLOX_TX_QUEUE_LEN, 4);
static K_SEM_DEFINE(ublox_work_sem, 0, 1);
static K_THREAD_STACK_DEFINE(ublox_task_stack, UBLOX_TASK_STACK_SIZE);
static struct k_thread ublox_task_data;

static int queue_tx_msg(const uint8_t *buf, size_t len)
{
	struct ublox_tx_msg msg = { 0 };
	int err;

	if (len > sizeof(msg.data)) {
		return -EMSGSIZE;
	}

	if (len != 0U) {
		memcpy(msg.data, buf, len);
	}
	msg.len = len;

	err = k_msgq_put(&ublox_tx_msgq, &msg, K_NO_WAIT);
	if (err == 0) {
		k_sem_give(&ublox_work_sem);
	}

	return err;
}

int ublox_transport_send_bytes(const uint8_t *buf, size_t len)
{
	if (buf == NULL && len != 0U) {
		return -EINVAL;
	}

	return queue_tx_msg(buf, len);
}

int ublox_transport_send_at_command(const char *command)
{
	struct ublox_tx_msg msg = { 0 };
	size_t len;
	int err;

	if (command == NULL) {
		return -EINVAL;
	}

	len = strlen(command);
	if ((len + 2U) > sizeof(msg.data)) {
		return -EMSGSIZE;
	}

	memcpy(msg.data, command, len);
	msg.data[len] = '\r';
	msg.data[len + 1U] = '\n';
	msg.len = len + 2U;

	err = k_msgq_put(&ublox_tx_msgq, &msg, K_NO_WAIT);
	if (err == 0) {
		k_sem_give(&ublox_work_sem);
	}

	return err;
}

static void process_response_line(const char *line)
{
	if (line[0] == '\0') {
		return;
	}

	if (strcmp(line, "OK") == 0) {
		LOG_DBG("u-blox response OK");
	} else if (strcmp(line, "ERROR") == 0 || strncmp(line, "+CME ERROR", 10) == 0) {
		LOG_WRN("u-blox error: %s", line);
	} else if (line[0] == '+') {
		LOG_INF("u-blox URC/response: %s", line);
	} else {
		LOG_DBG("u-blox response: %s", line);
	}

	transport_config->hmi_write_response_line(line);
}

static void response_parser_feed(struct response_parser *parser, const uint8_t *buf, size_t len)
{
	for (size_t i = 0; i < len; i++) {
		uint8_t ch = buf[i];

		if (ch == '\r') {
			continue;
		}

		if (ch == '\n') {
			parser->line[parser->len] = '\0';
			process_response_line(parser->line);
			parser->len = 0U;
			continue;
		}

		if (parser->len >= (sizeof(parser->line) - 1U)) {
			parser->line[parser->len] = '\0';
			process_response_line(parser->line);
			parser->len = 0U;
		}

		parser->line[parser->len++] = (char)ch;
	}
}

static void drain_tx(void)
{
	struct ublox_tx_msg msg;

	while (k_msgq_get(&ublox_tx_msgq, &msg, K_NO_WAIT) == 0) {
		uart_async_port_write(transport_config->port, msg.data, msg.len);
	}
}

static void drain_rx(struct response_parser *parser)
{
	uint8_t data[64];
	uint32_t len;

	do {
		len = uart_async_port_read(transport_config->port, data, sizeof(data));

		if (len == 0U) {
			continue;
		}

		if (transport_config->bridge_mode_enabled()) {
			transport_config->hmi_write_bytes(data, len);
		} else {
			response_parser_feed(parser, data, len);
		}
	} while (len != 0U);
}

static void ublox_task(void *p1, void *p2, void *p3)
{
	struct response_parser parser = { 0 };

	(void)p1;
	(void)p2;
	(void)p3;

	while (1) {
		k_sem_take(&ublox_work_sem, K_FOREVER);
		drain_tx();
		drain_rx(&parser);
	}
}

static void ublox_rx_wakeup_task(void *p1, void *p2, void *p3)
{
	(void)p1;
	(void)p2;
	(void)p3;

	while (1) {
		k_sem_take(transport_config->port->rx_sem, K_FOREVER);
		k_sem_give(&ublox_work_sem);
	}
}

static K_THREAD_STACK_DEFINE(ublox_rx_wakeup_stack, 512);
static struct k_thread ublox_rx_wakeup_task_data;

int ublox_transport_start(const struct ublox_transport_config *config)
{
	k_tid_t tid;

	if (config == NULL || config->port == NULL || config->bridge_mode_enabled == NULL ||
	    config->hmi_write_bytes == NULL || config->hmi_write_response_line == NULL) {
		return -EINVAL;
	}

	transport_config = config;

	tid = k_thread_create(&ublox_task_data, ublox_task_stack,
			      K_THREAD_STACK_SIZEOF(ublox_task_stack), ublox_task,
			      NULL, NULL, NULL, UBLOX_TASK_PRIORITY, 0, K_NO_WAIT);
	k_thread_name_set(tid, "uart1_ublox");

	tid = k_thread_create(&ublox_rx_wakeup_task_data, ublox_rx_wakeup_stack,
			      K_THREAD_STACK_SIZEOF(ublox_rx_wakeup_stack), ublox_rx_wakeup_task,
			      NULL, NULL, NULL, UBLOX_TASK_PRIORITY, 0, K_NO_WAIT);
	k_thread_name_set(tid, "uart1_rx_wakeup");

	return 0;
}
