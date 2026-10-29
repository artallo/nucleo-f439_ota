/*
 * sber-ota: UDP/TCP echo-сервер на NUCLEO-F439ZI (Zephyr).
 * Шаг 1: каркас приложения с сетью и логированием.
 * Шаг 2 (следующий): запись во второй банк flash при живой сети.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/net/socket.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_mgmt.h>
#include <zephyr/net/net_event.h>
#include <zephyr/net/conn_mgr_monitor.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/dfu/mcuboot.h>
#include <errno.h>
#include <zephyr/app_version.h>
#include <cmsis_core.h>

/* Строки лога — ASCII: консоли/CI/парсеры не всегда понимают UTF-8 */
LOG_MODULE_REGISTER(sber_ota, LOG_LEVEL_INF);

#define ECHO_PORT      4242
#define RX_BUF_SIZE    1500
#define THREAD_STACK   2048
#define THREAD_PRIO    K_PRIO_PREEMPT(8)

static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);

/* Серверы стартуют после события L4_CONNECTED от connection manager */
static K_SEM_DEFINE(net_ready, 0, 1);

static void l4_event_handler(uint64_t event, struct net_if *iface, void *info,
			     size_t info_length, void *user_data)
{
	ARG_UNUSED(iface);
	ARG_UNUSED(info);
	ARG_UNUSED(info_length);
	ARG_UNUSED(user_data);

	if (event == NET_EVENT_L4_CONNECTED) {
		LOG_INF("network up");
		k_sem_give(&net_ready);
	} else if (event == NET_EVENT_L4_DISCONNECTED) {
		LOG_WRN("network down");
	}
}

NET_MGMT_REGISTER_EVENT_HANDLER(l4_handler,
				NET_EVENT_L4_CONNECTED | NET_EVENT_L4_DISCONNECTED,
				l4_event_handler, NULL);

/*
 * Подтверждение образа после обновления. MCUboot запускает новый образ как
 * "testing"; без подтверждения следующий сброс откатит предыдущий. Критерий
 * "образ исправен" здесь — принят и отвечен первый UDP-запрос: значит, стек,
 * драйвер Ethernet и приложение живы и доступны для скрипта обновления.
 */
static void confirm_image_after_first_reply(void)
{
	static bool done;
	int rc;

	if (done) {
		return;
	}
	done = true;

	if (!IS_ENABLED(CONFIG_SBER_OTA_AUTO_CONFIRM)) {
		LOG_WRN("auto-confirm disabled: image stays in testing state");
		return;
	}
	if (boot_is_img_confirmed()) {
		return;
	}
	rc = boot_write_img_confirmed();
	if (rc) {
		LOG_ERR("boot_write_img_confirmed: %d", rc);
	} else {
		LOG_INF("image confirmed after first network reply");
	}
}

static void wait_net_ready(void)
{
	k_sem_take(&net_ready, K_FOREVER);
	k_sem_give(&net_ready); /* передать эстафету второму потоку */
}

/* ---------- UDP echo ---------- */
static void udp_echo_thread(void *a, void *b, void *c)
{
	static uint8_t buf[RX_BUF_SIZE];
	struct sockaddr_in addr = {
		.sin_family = AF_INET,
		.sin_port = htons(ECHO_PORT),
		.sin_addr.s_addr = htonl(INADDR_ANY),
	};
	int sock;

	wait_net_ready();

	sock = zsock_socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
	if (sock < 0) {
		LOG_ERR("UDP socket: %d", errno);
		return;
	}
	if (zsock_bind(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		LOG_ERR("UDP bind: %d", errno);
		zsock_close(sock);
		return;
	}
	LOG_INF("UDP echo listening on port %d", ECHO_PORT);

	while (1) {
		struct sockaddr_in peer;
		socklen_t peer_len = sizeof(peer);
		ssize_t n = zsock_recvfrom(sock, buf, sizeof(buf), 0,
					   (struct sockaddr *)&peer, &peer_len);

		if (n < 0) {
			LOG_ERR("UDP recvfrom: %d", errno);
			continue;
		}
		if (zsock_sendto(sock, buf, n, 0, (struct sockaddr *)&peer, peer_len) < 0) {
			LOG_ERR("UDP sendto: %d", errno);
			continue;
		}
		confirm_image_after_first_reply();
	}
}

/* ---------- TCP echo (один клиент за раз) ---------- */
static void tcp_echo_thread(void *a, void *b, void *c)
{
	static uint8_t buf[RX_BUF_SIZE];
	struct sockaddr_in addr = {
		.sin_family = AF_INET,
		.sin_port = htons(ECHO_PORT),
		.sin_addr.s_addr = htonl(INADDR_ANY),
	};
	int sock;

	wait_net_ready();

	sock = zsock_socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
	if (sock < 0) {
		LOG_ERR("TCP socket: %d", errno);
		return;
	}
	if (zsock_bind(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0 ||
	    zsock_listen(sock, 2) < 0) {
		LOG_ERR("TCP bind/listen: %d", errno);
		zsock_close(sock);
		return;
	}
	LOG_INF("TCP echo listening on port %d", ECHO_PORT);

	while (1) {
		struct sockaddr_in peer;
		socklen_t peer_len = sizeof(peer);
		char ip[NET_IPV4_ADDR_LEN];
		int client = zsock_accept(sock, (struct sockaddr *)&peer, &peer_len);

		if (client < 0) {
			LOG_ERR("TCP accept: %d", errno);
			continue;
		}
		zsock_inet_ntop(AF_INET, &peer.sin_addr, ip, sizeof(ip));
		LOG_INF("TCP client %s:%u", ip, ntohs(peer.sin_port));

		while (1) {
			ssize_t n = zsock_recv(client, buf, sizeof(buf), 0);
			ssize_t off = 0;

			if (n <= 0) {
				break;
			}
			while (off < n) {
				ssize_t s = zsock_send(client, buf + off, n - off, 0);

				if (s < 0) {
					n = -1;
					break;
				}
				off += s;
			}
			if (n < 0) {
				break;
			}
		}
		LOG_INF("TCP client disconnected");
		zsock_close(client);
	}
}

K_THREAD_DEFINE(udp_tid, THREAD_STACK, udp_echo_thread, NULL, NULL, NULL, THREAD_PRIO, 0, 0);
K_THREAD_DEFINE(tcp_tid, THREAD_STACK, tcp_echo_thread, NULL, NULL, NULL, THREAD_PRIO, 0, 0);

/* Распечатать partitions из DTS: проверка, что overlay подхватился */
static void log_flash_layout(void)
{
	const struct flash_area *fa;
	const uint8_t ids[] = {
		PARTITION_ID(boot_partition),
		PARTITION_ID(boot_state_partition),
		PARTITION_ID(reserved_partition),
		PARTITION_ID(slot0_partition),
		PARTITION_ID(slot1_partition),
	};

	for (size_t i = 0; i < ARRAY_SIZE(ids); i++) {
		if (flash_area_open(ids[i], &fa) == 0) {
			LOG_INF("partition %u: off 0x%08lx size %4u KB dev %s", fa->fa_id,
				(unsigned long)fa->fa_off, (unsigned int)(fa->fa_size / 1024),
				fa->fa_dev->name);
			flash_area_close(fa);
		}
	}
}

int main(void)
{
	/* VTOR показывает, откуда реально исполняемся: 0x08000000 без загрузчика,
	 * 0x08020000 (+0x200 заголовка MCUboot) — если нас запустил MCUboot из slot0 */
	LOG_INF("sber-ota %s start, board=%s, VTOR=0x%08x", APP_VERSION_EXTENDED_STRING,
		CONFIG_BOARD, (unsigned int)SCB->VTOR);

	if (gpio_is_ready_dt(&led)) {
		gpio_pin_configure_dt(&led, GPIO_OUTPUT_INACTIVE);
	}

	LOG_INF("image state: %s", boot_is_img_confirmed() ? "confirmed"
		: "testing (reverts on next reset unless confirmed)");
	log_flash_layout();

	/* conn_mgr: если интерфейс уже поднят, разослать текущее состояние */
	conn_mgr_mon_resend_status();

	while (1) {
		gpio_pin_toggle_dt(&led);
		k_sleep(K_MSEC(500));
	}
	return 0;
}
