/* ============================================================
 * wifi_prov.c -- SoftAP captive-portal WiFi provisioning
 *
 * Flow:
 *   1. Switch to APSTA, broadcast open hotspot "VitalSigns-Setup"
 *   2. DNS hijack: every A-record query resolves to 192.168.4.1
 *      This makes iOS / Android / Windows pop up a captive-portal
 *      page automatically.
 *   3. HTTP server on port 80:
 *        GET  (wildcard) -> configuration web page (HTML + JS)
 *        GET  /scan     -> JSON list of nearby networks
 *        POST /save     -> parse SSID+password, save to NVS, reboot
 * ============================================================ */

#include <string.h>
#include <stdlib.h>
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_http_server.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "esp_netif.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "lwip/sockets.h"
#include "cJSON.h"
#include "wifi_prov.h"

static const char *TAG = "PROV";

/* ---- AP configuration ------------------------------------- */
#define PROV_SSID        "VitalSigns-Setup"
#define PROV_MAX_CONN    4
#define PROV_AP_CHANNEL  1

/* ---- DNS hijack ------------------------------------------- */
static const uint8_t AP_IP[4] = {192, 168, 4, 1};

/* ============================================================
 * DNS hijack task
 *
 * Responds to every A-record query with 192.168.4.1 so that any
 * phone or laptop on the hotspot is redirected to our web page.
 * ============================================================ */
static void dns_hijack_task(void *arg)
{
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (sock < 0) {
        ESP_LOGE(TAG, "DNS socket create failed");
        vTaskDelete(NULL);
        return;
    }

    struct sockaddr_in srv = {0};
    srv.sin_family      = AF_INET;
    srv.sin_port        = htons(53);
    srv.sin_addr.s_addr = htonl(INADDR_ANY);

    if (bind(sock, (struct sockaddr *)&srv, sizeof(srv)) < 0) {
        ESP_LOGE(TAG, "DNS bind failed");
        close(sock);
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG, "DNS hijack listening on :53");

    uint8_t rx[512], tx[512];
    struct sockaddr_in cli;
    socklen_t cli_len;

    for (;;) {
        cli_len = sizeof(cli);
        int n = recvfrom(sock, rx, sizeof(rx), 0,
                         (struct sockaddr *)&cli, &cli_len);
        if (n < 17) continue;               /* too short: header(12)+1-label+type+class */

        memcpy(tx, rx, n);

        /* Header flags: QR=1, RD=1 (copy), RA=1, RCODE=0 */
        tx[2] = 0x81;
        tx[3] = 0x80;
        /* Answer count = 1 */
        tx[6] = 0x00; tx[7] = 0x01;

        int off = n;
        /* Name pointer back to the question at offset 12 */
        tx[off++] = 0xC0; tx[off++] = 0x0C;
        /* Type A */
        tx[off++] = 0x00; tx[off++] = 0x01;
        /* Class IN */
        tx[off++] = 0x00; tx[off++] = 0x01;
        /* TTL 60s */
        tx[off++] = 0x00; tx[off++] = 0x00;
        tx[off++] = 0x00; tx[off++] = 0x3C;
        /* RDLENGTH */
        tx[off++] = 0x00; tx[off++] = 0x04;
        /* RDATA = AP IP */
        tx[off++] = AP_IP[0];
        tx[off++] = AP_IP[1];
        tx[off++] = AP_IP[2];
        tx[off++] = AP_IP[3];

        sendto(sock, tx, off, 0,
               (struct sockaddr *)&cli, cli_len);
    }
}


/* ============================================================
 * URL-decode helper (form fields use application/x-www-form-urlencoded)
 * ============================================================ */
static void url_decode(char *dst, size_t dst_max, const char *src, size_t src_len)
{
    size_t d = 0;
    for (size_t i = 0; i < src_len && d + 1 < dst_max; i++) {
        if (src[i] == '%' && i + 2 < src_len) {
            char hex[3] = { src[i+1], src[i+2], 0 };
            dst[d++] = (char)strtol(hex, NULL, 16);
            i += 2;
        } else if (src[i] == '+') {
            dst[d++] = ' ';
        } else {
            dst[d++] = src[i];
        }
    }
    dst[d] = '\0';
}

/* Extract a named field from "key=value&key=value" body. */
static bool form_field(char *dst, size_t dst_max,
                       const char *body, size_t body_len,
                       const char *key)
{
    size_t klen = strlen(key);
    const char *p = body;
    const char *end = body + body_len;

    while (p < end) {
        const char *amp = memchr(p, '&', end - p);
        size_t pair_len = amp ? (size_t)(amp - p) : (size_t)(end - p);

        if (pair_len > klen && strncmp(p, key, klen) == 0 && p[klen] == '=') {
            url_decode(dst, dst_max, p + klen + 1, pair_len - klen - 1);
            return true;
        }
        p = amp ? amp + 1 : end;
    }
    dst[0] = '\0';
    return false;
}


/* ============================================================
 * HTTP handlers
 * ============================================================ */

/* Compact configuration page.  JavaScript fetches /scan on load to
 * populate the dropdown.  The <form> POSTs to /save. */
static const char PROV_HTML[] =
"<!DOCTYPE html><html><head><meta charset='utf-8'>"
"<meta name='viewport' content='width=device-width,initial-scale=1'>"
"<title>Vital Signs Monitor</title><style>"
"body{font-family:system-ui,sans-serif;max-width:420px;margin:40px auto;padding:0 20px;color:#333}"
"h2{color:#0EA5E9;margin-bottom:4px}label{display:block;margin:12px 0 4px;font-size:14px;color:#555}"
"select,input{width:100%;padding:11px;margin:4px 0;box-sizing:border-box;font-size:15px;border:1px solid #ccc;border-radius:6px}"
"button{width:100%;padding:14px;margin-top:16px;background:#0EA5E9;color:#fff;border:none;border-radius:6px;font-size:16px}"
".hint{font-size:12px;color:#999;margin-top:4px}</style></head><body>"
"<h2>Vital Signs Monitor</h2><p>WiFi Setup</p>"
"<form action='/save' method='POST'>"
"<label>Network</label>"
"<select name='ssid' id='ssid'><option value=''>Scanning...</option></select>"
"<div class='hint'>Can't find it? Type the SSID below.</div>"
"<input name='ssid_manual' placeholder='Manual SSID (optional)'>"
"<label>Password</label>"
"<input name='pass' type='password' placeholder='WiFi password'>"
"<label>MQTT Broker IP</label>"
"<input name='broker' placeholder='192.168.1.100'>"
"<button type='submit'>Save &amp; Connect</button></form>"
"<script>"
"fetch('/scan').then(r=>r.json()).then(a=>{"
"var s=document.getElementById('ssid');"
"s.innerHTML='<option value=\"\">Select a network...</option>';"
"a.sort((x,y)=>y.r-x.r).forEach(n=>{"
"var o=document.createElement('option');o.value=n.s;"
"o.textContent=n.s+' ('+n.r+' dBm)';s.appendChild(o)});}).catch(()=>{"
"document.getElementById('ssid').innerHTML='<option value=\"\">Scan failed</option>'});"
"</script></body></html>";


static esp_err_t root_get_handler(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html");
    httpd_resp_sendstr(req, PROV_HTML);
    return ESP_OK;
}


static esp_err_t scan_get_handler(httpd_req_t *req)
{
    /* Blocking scan across all channels. */
    wifi_scan_config_t cfg = {0};
    esp_wifi_scan_start(&cfg, true);

    uint16_t count = 0;
    esp_wifi_scan_get_ap_num(&count);
    if (count > 24) count = 24;

    wifi_ap_record_t *aps = NULL;
    if (count > 0) {
        aps = calloc(count, sizeof(wifi_ap_record_t));
        if (aps) esp_wifi_scan_get_ap_records(&count, aps);
    }

    cJSON *arr = cJSON_CreateArray();
    for (uint16_t i = 0; i < count; i++) {
        if (aps[i].ssid[0] == '\0') continue;  /* skip hidden APs */
        cJSON *item = cJSON_CreateObject();
        cJSON_AddStringToObject(item, "s", (const char *)aps[i].ssid);
        cJSON_AddNumberToObject(item, "r", aps[i].rssi);
        cJSON_AddItemToArray(arr, item);
    }

    char *json = cJSON_PrintUnformatted(arr);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, json ? json : "[]");

    free(json);
    cJSON_Delete(arr);
    free(aps);
    return ESP_OK;
}


static esp_err_t save_post_handler(httpd_req_t *req)
{
    /* Read the POST body (URL-encoded form data). */
    int total = req->content_len;
    if (total <= 0 || total > 1024) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Bad request");
        return ESP_FAIL;
    }

    char *body = malloc(total + 1);
    if (!body) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "OOM");
        return ESP_FAIL;
    }

    int received = httpd_req_recv(req, body, total);
    if (received <= 0) {
        free(body);
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "No body");
        return ESP_FAIL;
    }
    body[received] = '\0';

    char ssid[33] = {0};
    char ssid_man[33] = {0};
    char pass[65] = {0};
    char broker[128] = {0};

    form_field(ssid_man, sizeof(ssid_man), body, received, "ssid_manual");
    form_field(ssid, sizeof(ssid), body, received, "ssid");
    form_field(pass, sizeof(pass), body, received, "pass");
    form_field(broker, sizeof(broker), body, received, "broker");
    free(body);

    /* Wrap bare IP into a full mqtt:// URI.
     * User can type just "192.168.1.100" and we produce "mqtt://192.168.1.100:1883".
     * If the input already starts with mqtt:// we leave it as-is. */
    if (broker[0] != '\0' && strncmp(broker, "mqtt://", 7) != 0) {
        char wrapped[140];
        snprintf(wrapped, sizeof(wrapped), "mqtt://%s:1883", broker);
        strncpy(broker, wrapped, sizeof(broker) - 1);
        broker[sizeof(broker) - 1] = '\0';
    }

    /* Prefer manually typed SSID if the dropdown was left empty. */
    if (ssid_man[0] != '\0')
        memcpy(ssid, ssid_man, sizeof(ssid));

    if (ssid[0] == '\0') {
        httpd_resp_set_type(req, "text/html");
        httpd_resp_sendstr(req,
            "<html><body><h2>Missing SSID</h2>"
            "<p>Please go back and select or type a network name.</p>"
            "<p><a href='/'>Retry</a></p></body></html>");
        return ESP_OK;
    }

    /* Persist and acknowledge. */
    save_wifi_to_nvs(ssid, pass, broker);

    ESP_LOGI(TAG, "Credentials saved (ssid=%s, broker=%s), restarting in 2 s...", ssid, broker[0] ? broker : "(default)");

    httpd_resp_set_type(req, "text/html");
    httpd_resp_sendstr(req,
        "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
        "<h2 style='color:#0EA5E9'>Saved!</h2>"
        "<p>Connecting to WiFi and restarting...</p>"
        "<p style='color:#888;font-size:13px'>You can close this page.</p>"
        "</body></html>");

    /* Give the browser time to render, then reboot into normal mode. */
    vTaskDelay(pdMS_TO_TICKS(2000));
    esp_restart();

    return ESP_OK;  /* unreachable */
}


/* ============================================================
 * BOOT button check (GPIO0 on most ESP32 dev boards)
 * ============================================================ */
bool prov_boot_button_held(void)
{
    /* GPIO0 = BOOT button on most ESP32 dev boards.
     *
     * GPIO0 is also a strapping pin: if held LOW during reset the chip
     * enters flash download mode, so we CANNOT detect it during boot.
     *
     * Instead we give a 3-second window after normal boot for the user
     * to press the button and trigger provisioning. */
    gpio_config_t io = {
        .pin_bit_mask  = (1ULL << 0),
        .mode          = GPIO_MODE_INPUT,
        .pull_up_en    = GPIO_PULLUP_ENABLE,
        .pull_down_en  = GPIO_PULLDOWN_DISABLE,
        .intr_type     = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);

    ESP_LOGI(TAG, "Press BOOT (GPIO0) within 3s to enter WiFi setup...");

    for (int i = 3; i > 0; i--) {
        ESP_LOGI(TAG, "  BOOT check: %ds remaining", i);
        if (gpio_get_level(0) == 0) {
            ESP_LOGI(TAG, "BOOT pressed -> entering provisioning");
            return true;
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }

    ESP_LOGI(TAG, "BOOT not pressed, continuing normal boot");
    return false;
}


/* ============================================================
 * start_provisioning -- switch to APSTA, start DNS + HTTP server
 * ============================================================ */
void start_provisioning(void)
{
    ESP_LOGI(TAG, "=== Entering provisioning mode ===");
    ESP_LOGI(TAG, "Connect to hotspot \"%s\" and follow the popup.", PROV_SSID);

    /* APSTA so we can run the AP while also scanning on STA. */
    esp_wifi_set_mode(WIFI_MODE_APSTA);

    wifi_config_t ap_cfg = {0};
    strncpy((char *)ap_cfg.ap.ssid, PROV_SSID, sizeof(ap_cfg.ap.ssid) - 1);
    ap_cfg.ap.ssid_len       = strlen(PROV_SSID);
    ap_cfg.ap.channel        = PROV_AP_CHANNEL;
    ap_cfg.ap.max_connection = PROV_MAX_CONN;
    ap_cfg.ap.authmode       = WIFI_AUTH_OPEN;
    esp_wifi_set_config(WIFI_IF_AP, &ap_cfg);

    esp_wifi_start();

    /* DNS hijack in its own task. */
    xTaskCreate(dns_hijack_task, "dns_hijack", 3072, NULL, 5, NULL);

    /* HTTP server with wildcard URI matching. */
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.uri_match_fn     = httpd_uri_match_wildcard;
    config.max_uri_handlers = 8;
    config.max_req_hdr_len  = 1024;   /* iOS/Android captive probes send long headers */
    config.stack_size       = 8192;   /* extra stack for the page + JSON generation */

    httpd_handle_t server = NULL;
    if (httpd_start(&server, &config) != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start HTTP server");
        return;
    }

    /* Exact-match handlers first so they win over the wildcard. */
    httpd_uri_t scan_uri = {
        .uri = "/scan", .method = HTTP_GET, .handler = scan_get_handler
    };
    httpd_register_uri_handler(server, &scan_uri);

    httpd_uri_t save_uri = {
        .uri = "/save", .method = HTTP_POST, .handler = save_post_handler
    };
    httpd_register_uri_handler(server, &save_uri);

    /* Wildcard catch-all for the HTML page (also intercepts
     * captive-portal probe URLs from every OS). */
    httpd_uri_t root_uri = {
        .uri = "/*", .method = HTTP_GET, .handler = root_get_handler
    };
    httpd_register_uri_handler(server, &root_uri);

    ESP_LOGI(TAG, "Captive portal ready on http://192.168.4.1/");

    /* app_main can now return; the HTTP + DNS tasks keep running. */
}
