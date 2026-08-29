FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY services/static_departures_api.py /app/static_departures_api.py
COPY services/apple_store_notifications.py /app/apple_store_notifications.py
COPY services/apple_store_business_events.py /app/apple_store_business_events.py
COPY services/apple_store_notification_store.py /app/apple_store_notification_store.py
COPY services/telegram_sales_notifier.py /app/telegram_sales_notifier.py
COPY services/tfl_gateway.py /app/tfl_gateway.py
COPY services/translink_gateway.py /app/translink_gateway.py
COPY services/gtfsrt_gateway.py /app/gtfsrt_gateway.py
COPY services/ttc_gateway.py /app/ttc_gateway.py
COPY services/bay_area_gateway.py /app/bay_area_gateway.py
COPY services/king_county_gateway.py /app/king_county_gateway.py
COPY services/mta_ny_gateway.py /app/mta_ny_gateway.py
COPY services/mbta_gateway.py /app/mbta_gateway.py
COPY services/wmata_gateway.py /app/wmata_gateway.py
COPY services/geofox_gateway.py /app/geofox_gateway.py
COPY services/kyiv_gateway.py /app/kyiv_gateway.py
COPY services/kyiv_radar_inference.py /app/kyiv_radar_inference.py
COPY services/stm_gateway.py /app/stm_gateway.py
COPY services/fintraffic_gateway.py /app/fintraffic_gateway.py
COPY services/poland_gateway.py /app/poland_gateway.py
COPY scripts/dynamic_resource_resolver.py /app/dynamic_resource_resolver.py
COPY config/apple /app/config/apple
COPY config/finland-cities.json /app/config/finland-cities.json
COPY config/poland-cities.json /app/config/poland-cities.json
COPY config/poland-sources.json /app/config/poland-sources.json
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/static-departures/health', timeout=2)"
CMD ["python3", "/app/static_departures_api.py"]
