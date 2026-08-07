idf_component_register(SRCS "vital_signs_monitoring_sys.c" "wifi_prov.c"
                    INCLUDE_DIRS "."
                    REQUIRES esp_event esp_wifi esp_netif esp_system nvs_flash log freertos esp_http_server esp_timer esp_driver_gpio)