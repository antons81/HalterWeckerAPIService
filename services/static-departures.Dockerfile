FROM python:3.12-slim
WORKDIR /app
COPY services/static_departures_api.py /app/static_departures_api.py
COPY services/tfl_gateway.py /app/tfl_gateway.py
COPY services/translink_gateway.py /app/translink_gateway.py
COPY services/gtfsrt_gateway.py /app/gtfsrt_gateway.py
COPY services/ttc_gateway.py /app/ttc_gateway.py
COPY services/bay_area_gateway.py /app/bay_area_gateway.py
COPY services/king_county_gateway.py /app/king_county_gateway.py
COPY services/mta_ny_gateway.py /app/mta_ny_gateway.py
COPY services/mbta_gateway.py /app/mbta_gateway.py
COPY services/wmata_gateway.py /app/wmata_gateway.py
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/static-departures/health', timeout=2)"
CMD ["python3", "/app/static_departures_api.py"]
