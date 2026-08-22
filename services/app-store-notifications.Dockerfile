FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/app_store_notifications_api.py /app/app_store_notifications_api.py
COPY services/apple_store_notifications.py /app/apple_store_notifications.py
COPY services/apple_store_business_events.py /app/apple_store_business_events.py
COPY services/apple_store_notification_store.py /app/apple_store_notification_store.py
COPY services/telegram_sales_notifier.py /app/telegram_sales_notifier.py
COPY config/apple /app/config/apple

ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"

CMD ["python3", "/app/app_store_notifications_api.py"]
