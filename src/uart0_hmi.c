#include "uart0_hmi.h"

#include "uart_app_limits.h"

#include <ctype.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/printk.h>

#define UART0_LINE_SIZE 192
#define UART0_TASK_STACK_SIZE 1536
#define UART0_TASK_PRIORITY 5

enum uart0_mode {
	UART0_MODE_CONTROL = 0,
	UART0_MODE_BRIDGE,
};

struct line_reader {
	char buf[UART0_LINE_SIZE];
	size_t len;
};

static const struct uart0_hmi_config *hmi_config;
static atomic_t active_mode = UART0_MODE_CONTROL;
static K_MUTEX_DEFINE(uart0_tx_mutex);
static K_THREAD_STACK_DEFINE(uart0_task_stack, UART0_TASK_STACK_SIZE);
static struct k_thread uart0_task_data;

static enum uart0_mode get_uart0_mode(void)
{
	return (enum uart0_mode)atomic_get(&active_mode);
}

static void set_uart0_mode(enum uart0_mode mode)
{
	atomic_set(&active_mode, mode);
}

bool uart0_hmi_bridge_mode_enabled(void)
{
	return get_uart0_mode() == UART0_MODE_BRIDGE;
}

void uart0_hmi_write_bytes(const uint8_t *buf, size_t len)
{
	k_mutex_lock(&uart0_tx_mutex, K_FOREVER);
	uart_async_port_write(hmi_config->port, buf, len);
	k_mutex_unlock(&uart0_tx_mutex);
}

void uart0_hmi_write_str(const char *str)
{
	uart0_hmi_write_bytes((const uint8_t *)str, strlen(str));
}

void uart0_hmi_write_response_line(const char *line)
{
	uart0_hmi_write_str(line);
	uart0_hmi_write_str("\r\n");
}

static void uart0_prompt(void)
{
	if (get_uart0_mode() == UART0_MODE_CONTROL) {
		uart0_hmi_write_str("\r\nctrl> ");
	}
}

static bool has_unsafe_at_string_char(const char *str)
{
	while (*str != '\0') {
		if (*str == '"') {
			return true;
		}
		str++;
	}

	return false;
}

static void echo_uart0_char(uint8_t ch)
{
	if (ch == '\r' || ch == '\n') {
		uart0_hmi_write_str("\r\n");
	} else if (ch == '\b' || ch == 0x7f) {
		uart0_hmi_write_str("\b \b");
	} else if (isprint(ch)) {
		uart0_hmi_write_bytes(&ch, 1);
	}
}

static void uart0_report_send_result(int err)
{
	if (err == 0) {
		return;
	}

	if (err == -EMSGSIZE) {
		uart0_hmi_write_str("Command is too long\r\n");
	} else if (err == -ENOMSG) {
		uart0_hmi_write_str("UART1 command queue is full\r\n");
	} else {
		char msg[48];

		snprintk(msg, sizeof(msg), "UART1 queue error: %d\r\n", err);
		uart0_hmi_write_str(msg);
	}
}

static char *skip_spaces(char *str)
{
	while (*str != '\0' && isspace((unsigned char)*str)) {
		str++;
	}

	return str;
}

static char *next_token(char **cursor)
{
	char *token = skip_spaces(*cursor);
	char *end;

	if (*token == '\0') {
		*cursor = token;
		return NULL;
	}

	end = token;
	while (*end != '\0' && !isspace((unsigned char)*end)) {
		end++;
	}

	if (*end != '\0') {
		*end = '\0';
		end++;
	}

	*cursor = end;
	return token;
}

static bool str_equal_ci(const char *left, const char *right)
{
	while (*left != '\0' && *right != '\0') {
		if (tolower((unsigned char)*left) != tolower((unsigned char)*right)) {
			return false;
		}

		left++;
		right++;
	}

	return *left == '\0' && *right == '\0';
}

static void control_help(void)
{
	uart0_hmi_write_str(
		"Commands:\r\n"
		"  help                     Show this list\r\n"
		"  bridge                   Enter AT bridge mode\r\n"
		"  at <AT command>          Send raw AT command to u-blox\r\n"
		"  scan                     AT+UWSCAN\r\n"
		"  status                   AT+UWSSTAT\r\n"
		"  connect <ssid> <pass>    Configure and activate station\r\n"
		"  disconnect               AT+UWSCA=0,4\r\n"
		"  ping <host>              AT+UPING=\"host\"\r\n"
		"  tcp <host> <port>        AT+UDCP=tcp://host:port\r\n"
		"  udp <host> <port>        AT+UDCP=udp://host:port\r\n"
		"  peers                    AT+UDLP?\r\n"
		"  close <handle>           AT+UDCPC=handle\r\n"
		"  server <port>            AT+UDSC=0,\"tcp://0.0.0.0:port\"\r\n"
		"  stop_server [id]         AT+UDSC=id,0\r\n");
}

static void send_formatted_at(char *buf, size_t buf_size, int len)
{
	if (len < 0 || (size_t)len >= buf_size) {
		uart0_hmi_write_str("Generated AT command is too long\r\n");
		return;
	}

	uart0_report_send_result(hmi_config->send_at_command(buf));
}

static void dispatch_control_command(char *line)
{
	char *cursor = line;
	char *cmd = next_token(&cursor);
	char at_command[UART_APP_MAX_AT_COMMAND_LEN + 1U];
	int len;

	if (cmd == NULL) {
		return;
	}

	if (str_equal_ci(cmd, "help") || str_equal_ci(cmd, "?")) {
		control_help();
	} else if (str_equal_ci(cmd, "bridge")) {
		set_uart0_mode(UART0_MODE_BRIDGE);
		uart0_hmi_write_str("Bridge mode. Type +++ and Enter to return to control mode.\r\n");
	} else if (str_equal_ci(cmd, "at")) {
		char *raw = skip_spaces(cursor);

		if (*raw == '\0') {
			uart0_hmi_write_str("Usage: at <AT command>\r\n");
			return;
		}

		uart0_report_send_result(hmi_config->send_at_command(raw));
	} else if (str_equal_ci(cmd, "scan")) {
		uart0_report_send_result(hmi_config->send_at_command("AT+UWSCAN"));
	} else if (str_equal_ci(cmd, "status")) {
		uart0_report_send_result(hmi_config->send_at_command("AT+UWSSTAT"));
	} else if (str_equal_ci(cmd, "connect")) {
		char *ssid = next_token(&cursor);
		char *pass = next_token(&cursor);

		if (ssid == NULL || pass == NULL) {
			uart0_hmi_write_str("Usage: connect <ssid> <password>\r\n");
			return;
		}

		if (has_unsafe_at_string_char(ssid) || has_unsafe_at_string_char(pass)) {
			uart0_hmi_write_str("SSID/password cannot contain double quotes\r\n");
			return;
		}

		len = snprintk(at_command, sizeof(at_command), "AT+UWSC=0,2,\"%s\"", ssid);
		send_formatted_at(at_command, sizeof(at_command), len);
		uart0_report_send_result(hmi_config->send_at_command("AT+UWSC=0,5,2"));
		len = snprintk(at_command, sizeof(at_command), "AT+UWSC=0,8,\"%s\"", pass);
		send_formatted_at(at_command, sizeof(at_command), len);
		uart0_report_send_result(hmi_config->send_at_command("AT+UWSCA=0,3"));
	} else if (str_equal_ci(cmd, "disconnect")) {
		uart0_report_send_result(hmi_config->send_at_command("AT+UWSCA=0,4"));
	} else if (str_equal_ci(cmd, "ping")) {
		char *host = next_token(&cursor);

		if (host == NULL) {
			uart0_hmi_write_str("Usage: ping <host>\r\n");
			return;
		}

		if (has_unsafe_at_string_char(host)) {
			uart0_hmi_write_str("Host cannot contain double quotes\r\n");
			return;
		}

		len = snprintk(at_command, sizeof(at_command), "AT+UPING=\"%s\"", host);
		send_formatted_at(at_command, sizeof(at_command), len);
	} else if (str_equal_ci(cmd, "tcp") || str_equal_ci(cmd, "udp")) {
		char *host = next_token(&cursor);
		char *port = next_token(&cursor);

		if (host == NULL || port == NULL) {
			uart0_hmi_write_str("Usage: tcp <host> <port> or udp <host> <port>\r\n");
			return;
		}

		len = snprintk(at_command, sizeof(at_command), "AT+UDCP=%s://%s:%s",
			       str_equal_ci(cmd, "tcp") ? "tcp" : "udp", host, port);
		send_formatted_at(at_command, sizeof(at_command), len);
	} else if (str_equal_ci(cmd, "peers")) {
		uart0_report_send_result(hmi_config->send_at_command("AT+UDLP?"));
	} else if (str_equal_ci(cmd, "close")) {
		char *handle = next_token(&cursor);

		if (handle == NULL) {
			uart0_hmi_write_str("Usage: close <handle>\r\n");
			return;
		}

		len = snprintk(at_command, sizeof(at_command), "AT+UDCPC=%s", handle);
		send_formatted_at(at_command, sizeof(at_command), len);
	} else if (str_equal_ci(cmd, "server")) {
		char *port = next_token(&cursor);

		if (port == NULL) {
			uart0_hmi_write_str("Usage: server <port>\r\n");
			return;
		}

		len = snprintk(at_command, sizeof(at_command), "AT+UDSC=0,\"tcp://0.0.0.0:%s\"",
			       port);
		send_formatted_at(at_command, sizeof(at_command), len);
	} else if (str_equal_ci(cmd, "stop_server")) {
		char *id = next_token(&cursor);

		if (id == NULL) {
			id = "0";
		}

		len = snprintk(at_command, sizeof(at_command), "AT+UDSC=%s,0", id);
		send_formatted_at(at_command, sizeof(at_command), len);
	} else {
		uart0_hmi_write_str("Unknown command. Type help.\r\n");
	}
}

static void finish_bridge_line(struct line_reader *reader)
{
	if (reader->len == 0U) {
		return;
	}

	reader->buf[reader->len] = '\0';

	if (strcmp(reader->buf, "+++") == 0 || str_equal_ci(reader->buf, "control")) {
		set_uart0_mode(UART0_MODE_CONTROL);
		uart0_hmi_write_str("Control mode.\r\n");
		reader->len = 0U;
		return;
	}

	uart0_report_send_result(
		hmi_config->send_bytes_to_ublox((const uint8_t *)reader->buf, reader->len));
	uart0_report_send_result(
		hmi_config->send_bytes_to_ublox((const uint8_t *)"\r\n", 2));
	reader->len = 0U;
}

static void line_reader_feed(struct line_reader *reader, uint8_t ch, bool bridge_mode)
{
	if (ch == '\r' || ch == '\n') {
		echo_uart0_char(ch);

		if (bridge_mode) {
			finish_bridge_line(reader);
		} else {
			reader->buf[reader->len] = '\0';
			dispatch_control_command(reader->buf);
			reader->len = 0U;
			uart0_prompt();
		}

		return;
	}

	if (ch == '\b' || ch == 0x7f) {
		if (reader->len > 0U) {
			reader->len--;
			echo_uart0_char(ch);
		}
		return;
	}

	if (reader->len >= (sizeof(reader->buf) - 1U)) {
		reader->len = 0U;
		uart0_hmi_write_str("\r\nInput line is too long\r\n");
		uart0_prompt();
		return;
	}

	reader->buf[reader->len++] = (char)ch;
	echo_uart0_char(ch);
}

static void uart0_task(void *p1, void *p2, void *p3)
{
	struct line_reader reader = { 0 };
	uint8_t data[64];
	uint32_t len;

	(void)p1;
	(void)p2;
	(void)p3;

	uart0_hmi_write_str("\r\nu-blox UART interface ready\r\n");
	control_help();
	uart0_prompt();

	while (1) {
		k_sem_take(hmi_config->port->rx_sem, K_FOREVER);

		do {
			len = uart_async_port_read(hmi_config->port, data, sizeof(data));

			for (uint32_t i = 0; i < len; i++) {
				line_reader_feed(&reader, data[i],
						 get_uart0_mode() == UART0_MODE_BRIDGE);
			}
		} while (len != 0U);
	}
}

int uart0_hmi_start(const struct uart0_hmi_config *config)
{
	k_tid_t tid;

	if (config == NULL || config->port == NULL || config->send_at_command == NULL ||
	    config->send_bytes_to_ublox == NULL) {
		return -EINVAL;
	}

	hmi_config = config;
	set_uart0_mode(UART0_MODE_CONTROL);

	tid = k_thread_create(&uart0_task_data, uart0_task_stack,
			      K_THREAD_STACK_SIZEOF(uart0_task_stack), uart0_task,
			      NULL, NULL, NULL, UART0_TASK_PRIORITY, 0, K_NO_WAIT);
	k_thread_name_set(tid, "uart0_hmi");

	return 0;
}
