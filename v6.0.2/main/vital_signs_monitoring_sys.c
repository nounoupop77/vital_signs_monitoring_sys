#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_mac.h"
#include "esp_system.h"
#include "esp_event.h"
#include "esp_wifi.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "mqtt_client.h"
#include "freertos/queue.h"
#include "esp_timer.h"
#include "ping/ping_sock.h"
#include "lwip/inet.h"
#include "wifi_prov.h"

/* ===== Debug switch =====
 * Set to 1 to enable diagnostic counters and per-packet logs.
 * Keep 0 in production: the stats log does a large snprintf every 50
 * packets and the MQTT DATA log fires on every received message. */
#define CSI_DEBUG 0

    /* ===== WiFi Configuration ===== */
/* defaults used when NVS has no saved credentials */
#define WIFI_DEFAULT_SSID "5G-tkshb_dhh"
#define WIFI_DEFAULT_PASS "66668888"
#define WIFI_MAX_RETRY   5
#define WIFI_NVS_NS      "wifi_cred"
#define WIFI_NVS_KEY_SSID "ssid"
#define WIFI_NVS_KEY_PASS "pass"
#define MQTT_NVS_KEY_BROKER "mqtt_broker"

/* ===== MQTT Configuration ===== */
#define MQTT_BROKER  "mqtt://172.20.10.5:1883"
#define MQTT_CLIENT  "esp32-csi-001"
#define MQTT_TOPIC   "me41004/csi"
/* ESP32 publishes its current WiFi connection info here for the GUI */
#define MQTT_STATUS_TOPIC "me41004/status"

/* ===== CSI Configuration ===== */
#define CSI_CHANNEL 0
#define CSI_SECOND  WIFI_SECOND_CHAN_NONE

static esp_mqtt_client_handle_t mqtt_client = NULL;
#define CSI_QUEUE_LEN   32
#define CSI_SEND_STACK  6144
#define MAX_CSI_BYTES   512

/* ===== Fixed-rate CSI experiment =====
 * Set to 0 to restore the adaptive 20-80 ms publish behaviour. */
#define CSI_FIXED_RATE_HZ 32
#define CSI_FIXED_MODE    1

/* ===== Adaptive publish throttle (rate-adaptive core) =====
 * Instead of a fixed MIN_PUBLISH_GAP_MS, the gap floats between MIN and MAX
 * based on how fast the network can drain the queue (qdepth).
 *   - qdepth low  -> network is fast -> shrink gap toward MIN (full speed)
 *   - qdepth high -> network is slow -> grow gap toward MAX (drop more)
 *   - publish error or near-full queue -> back off fast (big step up)
 * This keeps the connection alive on ANY network without manual tuning. */
#if CSI_FIXED_MODE
#define MIN_PUBLISH_GAP_MS  (1000 / CSI_FIXED_RATE_HZ)
#define MAX_PUBLISH_GAP_MS  MIN_PUBLISH_GAP_MS
#else
#define MIN_PUBLISH_GAP_MS  20     /* fastest  ~50 Hz (good network ceiling) */
#define MAX_PUBLISH_GAP_MS  80     /* slowest  ~12 Hz (bad network floor)    */
#endif
#define GAP_BACKOFF_STEP    5      /* ms added per back-off tick (fast)      */
#define GAP_RECOVERY_STEP   1      /* ms removed per recovery tick (slow)    */
#define QD_HIGH_WATER      12      /* queue depth that triggers back-off     */
#define QD_LOW_WATER        4      /* queue depth that allows recovery       */

static QueueHandle_t csi_queue = NULL;
static volatile bool mqtt_connected_flag = false;

/* CSI timing diagnostics. Updated from different tasks, but aligned 32-bit
 * reads are sufficient for the periodic operational log. */
#if CSI_DEBUG
static volatile uint32_t s_cb_total = 0;
static volatile uint32_t s_cb_too_soon = 0;
static volatile uint32_t s_cb_oversize = 0;
static volatile uint32_t s_cb_accepted = 0;
static volatile uint32_t s_q_replaced = 0;
static volatile uint32_t s_q_failed = 0;
static volatile uint32_t s_sender_dequeued = 0;
static volatile uint32_t s_send_no_mqtt = 0;
static volatile uint32_t s_publish_fail = 0;
#endif /* CSI_DEBUG */

typedef struct {
    uint8_t  mac[6];
    int8_t   rssi;
    uint16_t len;
    int64_t  ts_us;
    uint8_t  buf[MAX_CSI_BYTES];
} csi_sample_t;

static int s_retry_count = 0;
static bool mqtt_started = false;

static wifi_csi_config_t csi_cfg = {
    .lltf_en = 1,
    .htltf_en = 1,
    .stbc_htltf2_en = 1,
    .ltf_merge_en = 1,
    .channel_filter_en = 0,
    .manu_scale = false,
    .shift = 0,
};

/* =====================================================================
 * NVS WiFi credential storage
 * ====================================================================*/
static bool load_wifi_from_nvs(char *ssid, size_t ssid_len,
                                char *pass, size_t pass_len)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(WIFI_NVS_NS, NVS_READONLY, &h);
    if (err != ESP_OK) {
        strncpy(ssid, WIFI_DEFAULT_SSID, ssid_len);
        strncpy(pass, WIFI_DEFAULT_PASS, pass_len);
        ESP_LOGW("WIFI", "NVS open failed, using defaults");
        return false;
    }

    bool have_creds = true;

    size_t required = ssid_len;
    if (nvs_get_str(h, WIFI_NVS_KEY_SSID, ssid, &required) != ESP_OK) {
        strncpy(ssid, WIFI_DEFAULT_SSID, ssid_len);
        have_creds = false;
    }

    required = pass_len;
    if (nvs_get_str(h, WIFI_NVS_KEY_PASS, pass, &required) != ESP_OK)
        strncpy(pass, WIFI_DEFAULT_PASS, pass_len);

    nvs_close(h);
    ESP_LOGI("WIFI", "Loaded from NVS: ssid=%s have_creds=%d", ssid, have_creds);
    return have_creds;
}


/* Load MQTT broker URI from NVS; falls back to compiled default. */
static void load_broker_from_nvs(char *broker, size_t broker_len)
{
    strncpy(broker, MQTT_BROKER, broker_len - 1);
    broker[broker_len - 1] = '\0';

    nvs_handle_t h;
    if (nvs_open(WIFI_NVS_NS, NVS_READONLY, &h) != ESP_OK) return;

    size_t required = broker_len;
    if (nvs_get_str(h, MQTT_NVS_KEY_BROKER, broker, &required) != ESP_OK)
        ESP_LOGW("MQTT", "No broker in NVS, using default %s", broker);
    else
        ESP_LOGI("MQTT", "Broker from NVS: %s", broker);

    nvs_close(h);
}
void save_wifi_to_nvs(const char *ssid, const char *pass, const char *broker)
{
    nvs_handle_t h;
    if (nvs_open(WIFI_NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGE("WIFI", "Failed to open NVS for writing");
        return;
    }
    nvs_set_str(h, WIFI_NVS_KEY_SSID, ssid);
    nvs_set_str(h, WIFI_NVS_KEY_PASS, pass);
    if (broker && broker[0])
        nvs_set_str(h, MQTT_NVS_KEY_BROKER, broker);
    nvs_commit(h);
    nvs_close(h);
    ESP_LOGI("WIFI", "Saved new WiFi credentials to NVS: ssid=%s", ssid);
}

/* =====================================================================
 * Publish current WiFi connection info for the GUI.
 * Retained, so a GUI that connects later immediately sees the latest state.
 * ====================================================================*/
static void publish_wifi_status(void)
{
    if (mqtt_client == NULL || !mqtt_connected_flag) return;

    char ssid[33] = "";
    int rssi = 0;
    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
        strncpy(ssid, (const char *)ap.ssid, sizeof(ssid) - 1);
        ssid[sizeof(ssid) - 1] = '\0';
        rssi = ap.rssi;
    }

    char ip[16] = "";
    esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    if (netif != NULL) {
        esp_netif_ip_info_t ip_info;
        if (esp_netif_get_ip_info(netif, &ip_info) == ESP_OK)
            snprintf(ip, sizeof(ip), IPSTR, IP2STR(&ip_info.ip));
    }

    char json[128];
    snprintf(json, sizeof(json),
             "{\"ssid\":\"%s\",\"ip\":\"%s\",\"rssi\":%d}", ssid, ip, rssi);
    esp_mqtt_client_publish(mqtt_client, MQTT_STATUS_TOPIC, json, 0, 0, 1);
    ESP_LOGI("WIFI", "Published WiFi status: %s", json);
}

/* =====================================================================
 * MQTT event handler
 * ====================================================================*/
static void mqtt_event_handler(void *arg, esp_event_base_t base,
                               int32_t event_id, void *event_data)
{
    esp_mqtt_event_handle_t ev = event_data;
    switch (event_id) {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI("MQTT", "CONNECTED to broker");
        mqtt_connected_flag = true;
        publish_wifi_status();
        break;
    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW("MQTT", "DISCONNECTED from broker");
        mqtt_connected_flag = false;
        break;
    case MQTT_EVENT_ERROR:
        ESP_LOGE("MQTT", "ERROR type=%d", ev->error_handle->error_type);
        break;
    default:
        break;
    }
}

static void start_ping_to_gateway(esp_netif_ip_info_t *ip_info)
{
    esp_ping_config_t ping_cfg = ESP_PING_DEFAULT_CONFIG();
    ping_cfg.target_addr.u_addr.ip4.addr = ip_info->gw.addr;
    ping_cfg.target_addr.type = ESP_IPADDR_TYPE_V4;
    ping_cfg.interval_ms  = 10;
    ping_cfg.count        = 0;
    ping_cfg.timeout_ms   = 25;//1000/40=25ms for 40Hz ping

    esp_ping_handle_t ping;
    esp_ping_new_session(&ping_cfg, NULL, &ping);
    esp_ping_start(ping);
}

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        ESP_LOGI("PROBE", "EVENT: STA_START");
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT &&
               event_id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW("PROBE", "EVENT: STA_DISCONNECTED retry=%d", s_retry_count);
        if (s_retry_count < WIFI_MAX_RETRY) {
            esp_wifi_connect();
            s_retry_count++;
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ESP_LOGI("PROBE", "EVENT: GOT_IP");
        ip_event_got_ip_t* event = (ip_event_got_ip_t*) event_data;
        ESP_LOGI("WIFI", "got ip:" IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_count = 0;
        esp_err_t ret;
        ret = esp_wifi_set_csi_config(&csi_cfg);
        ESP_LOGI("PROBE", "set_csi_config => %s", esp_err_to_name(ret));
        ret = esp_wifi_set_csi(true);
        ESP_LOGI("PROBE", "set_csi(true) => %s", esp_err_to_name(ret));
        start_ping_to_gateway(&event->ip_info);

        if (!mqtt_started && mqtt_client != NULL) {
            esp_mqtt_client_start(mqtt_client);
            mqtt_started = true;
            ESP_LOGI("MQTT", "MQTT client started (after IP)");
        }

        /* Refresh the GUI's WiFi view (covers a reconnect where MQTT stayed up) */
        publish_wifi_status();
    }
}

/* No-op promiscuous callback - required to enable promiscuous mode.
 * CSI callback fires in parallel; we don't need packet content here. */
static void wifi_promiscuous_cb(void *buf, wifi_promiscuous_pkt_type_t type)
{
    (void)buf;
    (void)type;
}

/* CSI callback rate-limit: protects Core 0 from callback bursts. */
#if CSI_FIXED_MODE
#define CSI_MIN_CB_INTERVAL_US  ((1000000 + CSI_FIXED_RATE_HZ - 1) / CSI_FIXED_RATE_HZ)
#else
#define CSI_MIN_CB_INTERVAL_US  (20 * 1000)
#endif
static int64_t s_last_cb_us = 0;

static void wifi_csi_cb(void *ctx, wifi_csi_info_t *data) {
    if (data == NULL || data->buf == NULL || csi_queue == NULL) return;
#if CSI_DEBUG
    s_cb_total++;
#endif

    /* Drop excess callbacks beyond ~50 Hz before any processing */
    int64_t now_us = esp_timer_get_time();
    if ((now_us - s_last_cb_us) < CSI_MIN_CB_INTERVAL_US) {
#if CSI_DEBUG
        s_cb_too_soon++;
#endif
        return;
    }
    s_last_cb_us = now_us;

    if (data->len > MAX_CSI_BYTES) {
#if CSI_DEBUG
        s_cb_oversize++;
#endif
        return;
    }

    csi_sample_t s;
    s.ts_us = now_us;
    memcpy(s.mac, data->mac, 6);
    s.rssi = data->rx_ctrl.rssi;
    s.len  = data->len;
    memcpy(s.buf, data->buf, data->len);

    if (xQueueSendToBack(csi_queue, &s, 0) != pdPASS) {
        csi_sample_t drop;
        xQueueReceive(csi_queue, &drop, 0);
        if (xQueueSendToBack(csi_queue, &s, 0) == pdPASS) {
#if CSI_DEBUG
            s_q_replaced++;
#endif
            return;
        }
#if CSI_DEBUG
        s_q_failed++;
#endif
        return;
    }
#if CSI_DEBUG
    s_cb_accepted++;
#endif
}

/* =====================================================================
 * Adaptive CSI sender task
 *
 * The publish gap (gap_ms) self-tunes to whatever the current network can
 * sustain, using the outgoing queue depth as a congestion signal:
 *   - Queue draining well (qd <= LOW)  -> slowly speed up (shrink gap)
 *   - Queue backing up   (qd >= HIGH)  -> quickly slow down (grow gap)
 *   - Publish failed     (r < 0)       -> back off immediately
 * The asymmetry (fast back-off, slow recovery) is deliberate and is the
 * standard AIMD-style approach used by TCP: react aggressively to trouble,
 * probe optimistically when healthy. This prevents the select() timeout
 * disconnects seen on slow networks while still pushing full speed on fast
 * networks. Excess CSI samples that arrive during a back-off are dropped
 * locally (the `continue`), so they never stress the MQTT stack at all.
 * ====================================================================*/
static void csi_sender_task(void *arg)
{
    csi_sample_t s;
#if CSI_DEBUG
    uint32_t published = 0, skipped = 0;
#endif
    bool have_last_ts = false;
    int64_t last_sample_ms = 0;
    uint32_t gap_ms = MIN_PUBLISH_GAP_MS;   /* dynamic, adapts to network */

    while (1) {
        if (xQueueReceive(csi_queue, &s, portMAX_DELAY) != pdPASS) continue;
#if CSI_DEBUG
        s_sender_dequeued++;
#endif

        /* Throttle on capture time. Using wall-clock send time here mixes MQTT
         * latency jitter into the effective CSI sampling grid. */
        int64_t sample_ms = s.ts_us / 1000;

        /* Local throttle: drop samples that arrive faster than gap_ms.
         * Dropping here (before MQTT) is what keeps the TCP buffer from
         * saturating and triggering select() timeout disconnects. */
        if (have_last_ts && (sample_ms - last_sample_ms) < (int64_t)gap_ms) {
#if CSI_DEBUG
            skipped++;
#endif
            continue;
        }
        have_last_ts = true;
        last_sample_ms = sample_ms;

        char json[2048];
        int off = snprintf(json, sizeof(json),
            "{\"ts_us\":%lld,\"mac\":\"" MACSTR "\",\"rssi\":%d,\"len\":%d,\"subcarriers\":[",
            (long long)s.ts_us, MAC2STR(s.mac), s.rssi, s.len);
       for (int i = 0; i + 1 < s.len; i += 2) {
           int n = snprintf(json + off, sizeof(json) - off, "[%d,%d]%s",
                             (int16_t)(int8_t)s.buf[i],
                             (int16_t)(int8_t)s.buf[i + 1],
                            (i + 2 < s.len) ? "," : "");
            if (n < 0 || (size_t)n >= sizeof(json) - off) break;
            off += n;
        }
        if ((size_t)off < sizeof(json) - 2) {
            json[off++] = ']';
            json[off++] = '}';
            json[off]   = '\0';
        }

        if (mqtt_client && mqtt_connected_flag) {
            int r = esp_mqtt_client_publish(mqtt_client, MQTT_TOPIC, json, 0, 0, 0);
            if (r < 0) {
#if CSI_DEBUG
                s_publish_fail++;
#endif
            }
#if CSI_DEBUG
            published++;
#endif

            /* Adaptive feedback: steer gap_ms toward the network's sweet spot */
            UBaseType_t qd = uxQueueMessagesWaiting(csi_queue);
            if (r < 0 || qd >= QD_HIGH_WATER) {
                /* Network can't keep up -> back off quickly */
                gap_ms += GAP_BACKOFF_STEP;
                if (gap_ms > MAX_PUBLISH_GAP_MS) gap_ms = MAX_PUBLISH_GAP_MS;
            } else if (qd <= QD_LOW_WATER) {
                /* Network has headroom -> recover speed slowly */
                if (gap_ms > MIN_PUBLISH_GAP_MS) gap_ms -= GAP_RECOVERY_STEP;
            }
            /* Middle zone: hold steady, we've found this network's rate */

#if CSI_DEBUG
            if ((published % 50) == 0)
                ESP_LOGI("CSI",
                         "mode=%s rate=%dHz pub=%lu fail=%lu sskip=%lu "
                         "deq=%lu cb=%lu cbok=%lu cbskip=%lu big=%lu qrep=%lu qfail=%lu "
                         "nomqtt=%lu gap=%ums qd=%u",
                         CSI_FIXED_MODE ? "fixed" : "adaptive", CSI_FIXED_RATE_HZ,
                         (unsigned long)published, (unsigned long)s_publish_fail,
                         (unsigned long)skipped, (unsigned long)s_sender_dequeued,
                         (unsigned long)s_cb_total, (unsigned long)s_cb_accepted,
                         (unsigned long)s_cb_too_soon,
                         (unsigned long)s_cb_oversize, (unsigned long)s_q_replaced,
                         (unsigned long)s_q_failed, (unsigned long)s_send_no_mqtt,
                         (unsigned)gap_ms, (unsigned)qd);
#endif
        }
        else {
#if CSI_DEBUG
            s_send_no_mqtt++;
#endif
        }
    }
}

void app_main(void) {
    nvs_flash_init();
    esp_netif_init();
    esp_event_loop_create_default();

    /* Check BOOT button BEFORE WiFi init (GPIO0 is a strapping pin,
     * must be checked post-boot with a detection window) */
    bool force_prov = prov_boot_button_held();

    /* Init WiFi in station mode */
    esp_netif_create_default_wifi_sta();
    esp_netif_create_default_wifi_ap();   /* needed for provisioning SoftAP */
    esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                        &wifi_event_handler, NULL, NULL);
    esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                        &wifi_event_handler, NULL, NULL);
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    esp_wifi_init(&cfg);

    /* Load WiFi credentials from NVS (falls back to defaults) */
    char wifi_ssid[33] = {0};
    char wifi_pass[65] = {0};
    bool have_creds = load_wifi_from_nvs(wifi_ssid, sizeof(wifi_ssid),
                                            wifi_pass, sizeof(wifi_pass));


    /* Load broker URI from NVS (falls back to compiled default) */
    char mqtt_broker[128] = {0};
    load_broker_from_nvs(mqtt_broker, sizeof(mqtt_broker));
    /* No saved WiFi or BOOT button held -> start SoftAP provisioning portal */
    if (!have_creds || force_prov) {
        start_provisioning();
        return;   /* provisioning tasks keep running after app_main exits */
    }

    /* ---- Normal station mode ---- */
    esp_wifi_set_mode(WIFI_MODE_STA);

    wifi_config_t wifi_cfg = {0};
    strncpy((char *)wifi_cfg.sta.ssid, wifi_ssid, sizeof(wifi_cfg.sta.ssid) - 1);
    strncpy((char *)wifi_cfg.sta.password, wifi_pass, sizeof(wifi_cfg.sta.password) - 1);
    wifi_cfg.sta.channel = CSI_CHANNEL;
    wifi_cfg.sta.threshold.authmode = WIFI_AUTH_WPA_PSK;
    esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg);

    /* Enable CSI capture */
    esp_wifi_set_csi_rx_cb(&wifi_csi_cb, NULL);
    csi_queue = xQueueCreate(CSI_QUEUE_LEN, sizeof(csi_sample_t));
    xTaskCreate(csi_sender_task, "csi_sender", CSI_SEND_STACK, NULL, 5, NULL);

    esp_wifi_set_ps(WIFI_PS_NONE);
    esp_wifi_start();

    /* Promiscuous mode DISABLED. With it on, CSI fired for ALL OFDM frames
    * on channel 6 (beacons, other stations' data from ~16 nearby devices),
    * which mixed unrelated transmitters into the amplitude buffer and
    * produced the multi-peak FFT noise. Now CSI fires only on frames
    * addressed to this device, i.e. the ping/reply traffic with the
    * gateway (start_ping_to_gateway at ~50 Hz), which is a single clean
    * link for vital-signs detection. */

    /* Init MQTT */
    esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.uri = mqtt_broker,
        .credentials.client_id = MQTT_CLIENT,
        .network.timeout_ms = 5000,
        .network.reconnect_timeout_ms = 3000,
    };
    mqtt_client = esp_mqtt_client_init(&mqtt_cfg);
    esp_mqtt_client_register_event(mqtt_client, ESP_EVENT_ANY_ID,
                                   mqtt_event_handler, NULL);
}
