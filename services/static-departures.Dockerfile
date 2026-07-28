FROM python:3.12-slim
WORKDIR /app
COPY services/static_departures_api.py /app/static_departures_api.py
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/static-departures/health', timeout=2)"
CMD ["python3", "/app/static_departures_api.py"]
