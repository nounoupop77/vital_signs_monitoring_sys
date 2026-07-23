idf_component_register(SRCS "vital_signs_monitoring_sys.c"
                    INCLUDE_DIRS "."
                    REQUIRES esp_event esp_wifi esp_netif esp_system nvs_flash mqtt json log freertos)