/*
 * wifi_prov.h -- SoftAP captive-portal WiFi provisioning
 *
 * When called, the ESP32 switches to APSTA mode, broadcasts an open
 * hotspot, and serves a configuration web page.  Phones / laptops
 * that join the hotspot get a captive-portal popup automatically.
 */
#pragma once
#include <stdbool.h>

/* Defined in vital_signs_monitoring_sys.c; declared here so the
 * provisioning module can persist new credentials before rebooting. */
void save_wifi_to_nvs(const char *ssid, const char *pass, const char *broker);

/* Returns true if the BOOT button (GPIO0) is held low right now. */
bool prov_boot_button_held(void);

/* Enters provisioning mode.  Never returns (blocks on the HTTP task).
 * The device reboots automatically once the user submits WiFi creds. */
void start_provisioning(void);