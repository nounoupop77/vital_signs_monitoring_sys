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
#include "cJSON.h"

/* ═══ WiFi Configuration ═══ */
/* defaults used when NVS has no saved credentials */
#define WIFI_DEFAULT_SSID "4223PSTS"
#define WIFI_DEFAULT_PASS "1234567890"
#define WIFI_MAX_RETRY   5
#define WIFI_NVS_NS      "wifi_cred"
#define WIFI_NVS_KEY_SSID "ssid"
#define WIFI_NVS_KEY_PASS "pass"

/* ═══ MQTT Configuration ═══ */
#define MQTT_BROKER  "mqtt://192.168.110.68:1883"
#define MQTT_CLIENT  "esp32-csi-001"
#define MQTT_TOPIC   "me41004/csi"
/* ESP32 subscribes here to receive WiFi config from GUI */
#define MQTT_CONFIG_TOPIC "me41004/config"

/* ═══ CSI Configuration ═══ */
#define CSI_CHANNEL 0
#define CSI_SECOND  WIFI_SECOND_CHAN_NONE
    
static esp_mqtt_client_handle_t mqtt_client = NULL;
#define CSI_QUEUE_LEN   64   /* hold ~1.3s of 50Hz samples while MQTT is busy */
#define CSI_SEND_STACK  6144   /* was 6144; batch+json need more */
#define MAX_CSI_BYTES   512
#define CSI_BATCH_SIZE  1    /* local network: no batching needed, send each sample immediately */

static QueueHandle_t csi_queue = NULL;
static volatile bool mqtt_connected_flag = false;

typedef struct {
    int64_t  ts_us;   /* capture timestamp, microseconds (esp_timer_get_time) */
    uint8_t  mac[6];
    int8_t   rssi;
    uint16_t len;
    uint8_t  buf[MAX_CSI_BYTES];
} csi_sample_t;

/* BSSID of the gateway/AP; only CSI packets from this source are kept.
   Filled in GOT_IP before CSI is enabled. */
static uint8_t gateway_bssid[6] = {0};

static int s_retry_count = 0;
static bool mqtt_started = false;

static wifi_csi_config_t csi_cfg = {
    .lltf_en = 1,
    .htltf_en = 1,
    .stbc_htltf2_en = 0,
    .ltf_merge_en = 1,
    .channel_filter_en = 1,   /* reject adjacent-channel interference */
    .manu_scale = false,
    .shift = 0,
};

/* ──────────────────────────────────────────────
 * NVS WiFi credential storage
 * ──────────────────────────────────────────────*/
static void load_wifi_from_nvs(char *ssid, size_t ssid_len,
                                char *pass, size_t pass_len)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(WIFI_NVS_NS, NVS_READONLY, &h);
    if (err != ESP_OK) {
        strncpy(ssid, WIFI_DEFAULT_SSID, ssid_len);
        strncpy(pass, WIFI_DEFAULT_PASS, pass_len);
        ESP_LOGW("WIFI", "NVS open failed, using defaults");
        return;
    }

    size_t required = ssid_len;
    if (nvs_get_str(h, WIFI_NVS_KEY_SSID, ssid, &required) != ESP_OK)
        strncpy(ssid, WIFI_DEFAULT_SSID, ssid_len);

    required = pass_len;
    if (nvs_get_str(h, WIFI_NVS_KEY_PASS, pass, &required) != ESP_OK)
        strncpy(pass, WIFI_DEFAULT_PASS, pass_len);

    nvs_close(h);
    ESP_LOGI("WIFI", "Loaded credentials from NVS: ssid=%s", ssid);
}

static void save_wifi_to_nvs(const char *ssid, const char *pass)
{
    nvs_handle_t h;
    if (nvs_open(WIFI_NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGE("WIFI", "Failed to open NVS for writing");
        return;
    }
    nvs_set_str(h, WIFI_NVS_KEY_SSID, ssid);
    nvs_set_str(h, WIFI_NVS_KEY_PASS, pass);
    nvs_commit(h);
    nvs_close(h);
    ESP_LOGI("WIFI", "Saved new WiFi credentials to NVS: ssid=%s", ssid);
}

/* ──────────────────────────────────────────────
 * Reconnect WiFi with new credentials
 * ──────────────────────────────────────────────*/
static void reconnect_wifi(const char *ssid, const char *pass)
{
    ESP_LOGI("WIFI", "Reconnecting to SSID=%s", ssid);

    wifi_config_t wifi_cfg = {0};
    strncpy((char *)wifi_cfg.sta.ssid, ssid, sizeof(wifi_cfg.sta.ssid) - 1);
    strncpy((char *)wifi_cfg.sta.password, pass, sizeof(wifi_cfg.sta.password) - 1);
    wifi_cfg.sta.channel = CSI_CHANNEL;

    s_retry_count = 0;
    esp_wifi_disconnect();
    esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg);
    esp_wifi_connect();
}

/* ──────────────────────────────────────────────
 * Parse incoming WiFi config JSON from MQTT
 *   expected: {"ssid":"...","password":"..."}
 * ──────────────────────────────────────────────*/
static void handle_wifi_config_msg(const char *json_str, int len)
{
    /* cJSON needs null-terminated string */
    char *buf = malloc(len + 1);
    if (!buf) return;
    memcpy(buf, json_str, len);
    buf[len] = '\0';

    cJSON *root = cJSON_Parse(buf);
    free(buf);
    if (!root) {
        ESP_LOGE("WIFI", "Failed to parse WiFi config JSON");
        return;
    }

    cJSON *j_ssid = cJSON_GetObjectItem(root, "ssid");
    cJSON *j_pass = cJSON_GetObjectItem(root, "password");

    if (cJSON_IsString(j_ssid) && j_ssid->valuestring[0] != '\0') {
        const char *ssid = j_ssid->valuestring;
        const char *pass = cJSON_IsString(j_pass) ? j_pass->valuestring : "";

        save_wifi_to_nvs(ssid, pass);
        reconnect_wifi(ssid, pass);
    } else {
        ESP_LOGW("WIFI", "WiFi config JSON missing ssid");
    }

    cJSON_Delete(root);
}

/* ──────────────────────────────────────────────
 * MQTT event handler
 * ──────────────────────────────────────────────*/
static void mqtt_event_handler(void *arg, esp_event_base_t base,
                               int32_t event_id, void *event_data)
{
    esp_mqtt_event_handle_t ev = event_data;
    switch (event_id) {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI("MQTT", "CONNECTED to broker");
        mqtt_connected_flag = true;
        /* subscribe to WiFi config topic */
        esp_mqtt_client_subscribe(mqtt_client, MQTT_CONFIG_TOPIC, 1);
        break;
    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW("MQTT", "DISCONNECTED from broker");
        mqtt_connected_flag = false;
        /* drop stale queued samples so reconnect starts fresh;
           old timestamps are useless to the Python resampler anyway */
        xQueueReset(csi_queue);
        break;
    case MQTT_EVENT_DATA:
        ESP_LOGI("MQTT", "DATA topic=%.*s", ev->topic_len, ev->topic);
        if (ev->topic_len == strlen(MQTT_CONFIG_TOPIC) &&
            strncmp(ev->topic, MQTT_CONFIG_TOPIC, ev->topic_len) == 0)
        {
            handle_wifi_config_msg(ev->data, ev->data_len);
        }
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
    ping_cfg.interval_ms  = 50;    /* 20 Hz: sustainable rate for ESP32 WiFi+CSI+MQTT pipeline */
    ping_cfg.count        = 0;
    ping_cfg.timeout_ms   = 1000;

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
        /* capture gateway BSSID so we can filter CSI to a single source */
        wifi_ap_record_t ap;
        if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
            memcpy(gateway_bssid, ap.bssid, 6);
            ESP_LOGI("CSI", "filtering CSI to gateway BSSID " MACSTR, MAC2STR(gateway_bssid));
        } else {
            ESP_LOGW("CSI", "AP info unavailable, MAC filter inactive");
        }
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
    }
}

static void wifi_csi_cb(void *ctx, wifi_csi_info_t *data) {
    if (data == NULL || data->buf == NULL || csi_queue == NULL) return;
    if (data->len > MAX_CSI_BYTES) return;
    /* keep only packets from the gateway; drops all other stations */
    if (memcmp(data->mac, gateway_bssid, 6) != 0) return;

    csi_sample_t s;
    s.ts_us = esp_timer_get_time();   /* stamp at capture time */
    memcpy(s.mac, data->mac, 6);
    s.rssi = data->rx_ctrl.rssi;
    s.len  = data->len;
    memcpy(s.buf, data->buf, data->len);

    if (xQueueSendToBack(csi_queue, &s, 0) != pdPASS) {
        csi_sample_t drop;
        xQueueReceive(csi_queue, &drop, 0);
        xQueueSendToBack(csi_queue, &s, 0);
    }
}

static const char b64tab[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
static int b64_encode(const uint8_t *src, int len, char *dst, int dst_max)
{
    int o = 0;
    for (int i = 0; i < len; i += 3) {
        uint32_t v = ((uint32_t)src[i]) << 16;
        if (i + 1 < len) v |= ((uint32_t)src[i + 1]) << 8;
        if (i + 2 < len) v |= src[i + 2];
        if (o + 4 >= dst_max) return -1;
        dst[o++] = b64tab[(v >> 18) & 0x3F];
        dst[o++] = b64tab[(v >> 12) & 0x3F];
        dst[o++] = (i + 1 < len) ? b64tab[(v >> 6) & 0x3F] : (char)61;
        dst[o++] = (i + 2 < len) ? b64tab[v & 0x3F] : (char)61;
    }
    dst[o] = '\0';
    return o;
}

static void csi_sender_task(void *arg)
{
    csi_sample_t batch[CSI_BATCH_SIZE];
    uint32_t published = 0;

    while (1) {
        /* collect CSI_BATCH_SIZE samples from the queue */
        int n = 0;
        while (n < CSI_BATCH_SIZE) {
            if (xQueueReceive(csi_queue, &batch[n], portMAX_DELAY) != pdPASS)
                continue;
            n++;
        }

        /* build one JSON with all N samples.
           Format: {"mac":"..","samples":[{"ts":..,"rssi":..,"len":..,"subcarriers":[[..]..]},..]} */
        /* heap-allocate the JSON buffer: 6KB on the stack would overflow */
        int jsonsz = 1200; char *json = malloc(jsonsz);
        if (!json) {
            ESP_LOGE("CSI", "json malloc failed, drop batch");
            continue;
        }
        int off = snprintf(json, jsonsz,
            "{\"mac\":\"" MACSTR "\",\"samples\":[",
            MAC2STR(batch[0].mac));

        /* base64-encode subcarrier data: 256 bytes -> 344 chars (vs ~1200 as JSON) */
char b64buf[400];
for (int b = 0; b < n; b++) {
    csi_sample_t *s = &batch[b];
    int blen = b64_encode(s->buf, s->len, b64buf, sizeof(b64buf));
    if (blen < 0) continue;
    off += snprintf(json + off, jsonsz - off,
        "%s{\"ts\":%lld,\"rssi\":%d,\"len\":%d,\"sc\":\"%s\"}",
        (b > 0) ? "," : "",
        (long long)(s->ts_us / 1000), s->rssi, s->len, b64buf);
    if ((size_t)off >= (size_t)jsonsz - 16) break;
}
        off += snprintf(json + off, jsonsz - off, "]}");

        if (mqtt_client && mqtt_connected_flag) {
            int r = esp_mqtt_client_publish(mqtt_client, MQTT_TOPIC, json, 0, 0, 0);
            published++;
            if ((published % 20) == 0)
                ESP_LOGI("CSI", "batches=%lu (%lu samps) ret=%d q=%u",
                         (unsigned long)published,
                         (unsigned long)(published * CSI_BATCH_SIZE), r,
                         (unsigned)uxQueueMessagesWaiting(csi_queue));
        }
        free(json);
    }
}

void app_main(void) {
    nvs_flash_init();
    esp_netif_init();
    esp_event_loop_create_default();

    /* Init WiFi in station mode */
    esp_netif_create_default_wifi_sta();
    esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                        &wifi_event_handler, NULL, NULL);
    esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                        &wifi_event_handler, NULL, NULL);
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    esp_wifi_init(&cfg);
    esp_wifi_set_mode(WIFI_MODE_STA);

    /* Load WiFi credentials from NVS (falls back to defaults) */
    char wifi_ssid[33] = {0};
    char wifi_pass[65] = {0};
    load_wifi_from_nvs(wifi_ssid, sizeof(wifi_ssid),
                       wifi_pass, sizeof(wifi_pass));

    wifi_config_t wifi_cfg = {0};
    strncpy((char *)wifi_cfg.sta.ssid, wifi_ssid, sizeof(wifi_cfg.sta.ssid) - 1);
    strncpy((char *)wifi_cfg.sta.password, wifi_pass, sizeof(wifi_cfg.sta.password) - 1);
    wifi_cfg.sta.channel = CSI_CHANNEL;
    esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg);

    /* Enable CSI capture */
    esp_wifi_set_csi_rx_cb(&wifi_csi_cb, NULL);
    csi_queue = xQueueCreate(CSI_QUEUE_LEN, sizeof(csi_sample_t));
    xTaskCreate(csi_sender_task, "csi_sender", CSI_SEND_STACK, NULL, 5, NULL);

    esp_wifi_set_ps(WIFI_PS_NONE);
    esp_wifi_start();

    /* Init MQTT */
    esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.uri = MQTT_BROKER,
        .credentials.client_id = MQTT_CLIENT,
        /* FRP tunnels need generous timeouts: slow TCP handshake + NAT churn */
        .network.timeout_ms = 15000,
        .network.reconnect_timeout_ms = 5000,
        /* short keepalive beats FRP/NAT idle timeouts (~60s) */
        .session.keepalive = 30,
    };
    mqtt_client = esp_mqtt_client_init(&mqtt_cfg);
    esp_mqtt_client_register_event(mqtt_client, ESP_EVENT_ANY_ID,
                                   mqtt_event_handler, NULL);
}
